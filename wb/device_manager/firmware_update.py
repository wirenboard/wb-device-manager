#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional, cast

from jsonrpc.exceptions import JSONRPCDispatchException

from . import logger
from .bootloader_scan import is_in_bootloader_mode
from .bus_scan_state import Port
from .fw_downloader import (
    BinaryDownloader,
    NoReleasedFwError,
    ReleasedBinary,
    RemoteFileDownloadingError,
    WBRemoteStorageError,
    get_latest_bootloader,
    get_released_fw,
)
from .mqtt_rpc import MQTTRPCAlreadyProcessingException, MQTTRPCErrorCode
from .releases import parse_releases
from .serial_device import Device, create_device_from_json
from .serial_rpc import (
    WB_DEVICE_PARAMETERS,
    WB_DEVICE_STEP_PARAMETERS,
    ModbusExceptionCode,
    ModbusProtocol,
    SerialExceptionBase,
    SerialRPCTimeoutException,
    SerialRPCWrapper,
    SerialTimeoutException,
    WBModbusException,
    get_parameter_with_step,
)
from .state_error import (
    DeviceResponseTimeoutError,
    FileDownloadError,
    GenericStateError,
    RPCCallTimeoutStateError,
    StateError,
)
from .ttl_lru_cache import ttl_lru_cache
from .version_comparison import component_firmware_is_newer, firmware_is_newer

WBMAP_MARKER = re.compile(r"\S*MAP\d+\S*")  # *MAP%d* matches


class SoftwareType(Enum):
    FIRMWARE = "firmware"
    BOOTLOADER = "bootloader"
    COMPONENT = "component"


@dataclass
class DeviceUpdateInfo:  # pylint: disable=too-many-instance-attributes
    port: Port
    protocol: ModbusProtocol
    slave_id: int
    to_version: str
    progress: int = 0
    from_version: Optional[str] = None
    type: SoftwareType = SoftwareType.FIRMWARE
    error: Optional[StateError] = None
    component_number: Optional[int] = None
    component_model: Optional[str] = None

    def __eq__(self, o):
        return (
            self.slave_id == o.slave_id
            and self.port == o.port
            and self.type == o.type
            and self.protocol == o.protocol
        )


class UpdateState:
    def __init__(self, mqtt_connection, topic: str) -> None:
        self._devices = []
        self._mqtt_connection = mqtt_connection
        self._topic = topic

    def _to_json_string(self) -> str:
        dict_for_json = {
            "devices": [to_dict_for_json(d) for d in self._devices],
        }
        return json.dumps(dict_for_json, indent=None, separators=(",", ":"))

    def update(self, device_info: DeviceUpdateInfo) -> None:
        try:
            self._devices[self._devices.index(device_info)] = device_info
        except ValueError:
            self._devices.append(device_info)
        self._mqtt_connection.publish(self._topic, self._to_json_string(), retain=True)

    def remove(self, device_info: DeviceUpdateInfo, should_notify: bool) -> None:
        try:
            self._devices.remove(device_info)
        except ValueError:
            return
        if should_notify:
            self._mqtt_connection.publish(self._topic, self._to_json_string(), retain=True)

    def is_updating(self, slave_id: int, port: Port) -> bool:
        return any(
            d.slave_id == slave_id and d.port == port and d.progress < 100 and d.error is None
            for d in self._devices
        )

    def clear_error(self, slave_id: int, port: Port, software_type: SoftwareType) -> None:
        for d in self._devices:
            if d.slave_id == slave_id and d.port == port and d.type == software_type and d.error is not None:
                self._devices.remove(d)
                self._mqtt_connection.publish(self._topic, self._to_json_string(), retain=True)
                return

    def reset(self) -> None:
        self._devices = []
        self._mqtt_connection.publish(self._topic, self._to_json_string(), retain=True)

    def publish_state(self):
        self._mqtt_connection.publish(self._topic, self._to_json_string(), retain=True)

    def clear_state(self):
        m_info = self._mqtt_connection.publish(self._topic, payload=None, retain=True, qos=1)
        m_info.wait_for_publish()


def to_dict_for_json(device_update_info: DeviceUpdateInfo) -> dict:
    d = asdict(device_update_info)
    d["type"] = device_update_info.type.value
    d["protocol"] = device_update_info.protocol.value
    return d


@dataclass
class FlashingOptions:
    reboot_to_bootloader: bool = True


@dataclass
class SoftwareComponent:
    type: SoftwareType = SoftwareType.FIRMWARE
    current_version: Optional[str] = None
    available: Optional[ReleasedBinary] = None
    flashing_options: FlashingOptions = field(
        default_factory=lambda: FlashingOptions(reboot_to_bootloader=True)
    )


@dataclass
class BootloaderInfo(SoftwareComponent):
    can_preserve_port_settings: bool = False
    type: SoftwareType = SoftwareType.BOOTLOADER


@dataclass
class FirmwareInfo(SoftwareComponent):
    type: SoftwareType = SoftwareType.FIRMWARE
    bootloader: BootloaderInfo = field(default_factory=BootloaderInfo)


@dataclass
class ComponentInfo(SoftwareComponent):
    """Information about inner hardware components firmware (sensors for example)"""

    type: SoftwareType = SoftwareType.COMPONENT
    model: Optional[str] = None
    flashing_options: FlashingOptions = field(
        default_factory=lambda: FlashingOptions(reboot_to_bootloader=False)
    )


@dataclass
class ParsedWBFW:
    info: bytes
    data: bytes


def parse_wbfw(data: bytes) -> ParsedWBFW:
    info_block_length = WB_DEVICE_PARAMETERS["fw_info_block"].register_count * 2

    bs = len(data)
    if bs % 2:
        raise ValueError(f"Fw file should be even-bytes long! Got {bs}b")

    res = ParsedWBFW(
        info=data[:info_block_length],
        data=data[info_block_length:],
    )
    if len(res.info) != info_block_length:
        raise ValueError(
            f"Info block size should be {info_block_length} bytes! Got {len(res.info)}\nRaw: {res.info}"
        )

    return res


@ttl_lru_cache(seconds_to_live=7200, maxsize=30)
def download_wbfw(binary_downloader: BinaryDownloader, url: str) -> ParsedWBFW:
    return parse_wbfw(binary_downloader.download_file(url))


class UpdateNotifier:  # pylint: disable=too-few-public-methods
    def __init__(self, notifications_count: int):
        self.step = -1
        self.notification_step = max(100 / notifications_count, 1)

    def should_notify(self, progress_percent: int) -> bool:
        if progress_percent >= 100:
            return True
        current_step = int(progress_percent / self.notification_step)
        if current_step > self.step:
            self.step = current_step
            return True
        return False


class FirmwareInfoReader:
    def __init__(self, downloader: BinaryDownloader) -> None:
        self._downloader = downloader
        self._release = parse_releases("/usr/lib/wb-release").get("SUITE", "")
        # When continuous read mode is enabled and a register is not available,
        # reading returns 0xFFFE for each register
        self._unavailable_component_signature = bytes(
            [0xFE for _ in range(WB_DEVICE_STEP_PARAMETERS["component_signature"].register_count)]
        ).decode("latin-1")

    async def read_bootloader_info(self, serial_device: Device, fw_signature: str) -> BootloaderInfo:
        res = BootloaderInfo()
        try:
            res.can_preserve_port_settings = (
                await serial_device.read(WB_DEVICE_PARAMETERS["reboot_to_bootloader_preserve_port_settings"])
                == 0
            )
        except SerialExceptionBase:
            pass
        try:
            res.current_version = cast(
                str, await serial_device.read(WB_DEVICE_PARAMETERS["bootloader_version"])
            )
            res.available = get_latest_bootloader(fw_signature, self._downloader)
        except (SerialExceptionBase, WBRemoteStorageError) as err:
            logger.debug("Can't get bootloader information for %s: %s", serial_device.description, err)
        return res

    async def read_fw_signature(self, serial_device: Device) -> str:
        return cast(str, await serial_device.read(WB_DEVICE_PARAMETERS["fw_signature"]))

    async def read_fw_version(self, serial_device: Device) -> str:
        return cast(str, await serial_device.read(WB_DEVICE_PARAMETERS["fw_version"]))

    async def read_component_info(
        self, serial_device: Device, component_number: int
    ) -> Optional[ComponentInfo]:

        signature_conf = get_parameter_with_step(
            WB_DEVICE_STEP_PARAMETERS["component_signature"], component_number
        )
        fw_version_conf = get_parameter_with_step(
            WB_DEVICE_STEP_PARAMETERS["component_fw_version"], component_number
        )
        model_conf = get_parameter_with_step(WB_DEVICE_STEP_PARAMETERS["component_model"], component_number)

        signature = await serial_device.read(signature_conf)
        if signature == self._unavailable_component_signature:
            return None
        current_version = await serial_device.read(fw_version_conf)
        available = get_released_fw(signature, self._release, self._downloader)
        model = await serial_device.read(model_conf)
        return ComponentInfo(current_version=current_version, available=available, model=model)

    async def read_components_presence(self, serial_device: Device) -> list[int]:
        timeout = 2  # Timeout is used because data may be not available immediately after switching on
        start = time.time()
        components_presence = []
        while time.time() - start < timeout and not components_presence:
            try:
                bytestring = await serial_device.read(WB_DEVICE_PARAMETERS["components_presence"])
                for byte in bytestring:
                    for bitposition in range(8):  # bits in byte
                        bitvalue = (byte & (1 << bitposition)) > 0
                        components_presence.append(int(bitvalue))
            except WBModbusException as e:
                if e.code != ModbusExceptionCode.SLAVE_DEVICE_BUSY:
                    return []
                await asyncio.sleep(0.1)

        res = []
        for component_number, presence_bit in enumerate(components_presence):
            if presence_bit == 1:
                res.append(component_number)
        return res

    async def read_components_info(self, serial_device: Device) -> dict[int, ComponentInfo]:
        res = {}
        for component_number in await self.read_components_presence(serial_device):
            component_info = await self.read_component_info(serial_device, component_number)
            if component_info is not None:
                res[component_number] = component_info
        return res

    def read_released_fw(self, signature: str) -> ReleasedBinary:
        return get_released_fw(signature, self._release, self._downloader)

    async def read(self, serial_device: Device, bootloader_mode: bool = False) -> FirmwareInfo:
        res = FirmwareInfo()
        signature = await self.read_fw_signature(serial_device)
        logger.debug("Get firmware info for: %s", signature)
        res.available = self.read_released_fw(signature)
        if not bootloader_mode:
            res.current_version = await self.read_fw_version(serial_device)
            res.bootloader = await self.read_bootloader_info(serial_device, signature)
        return res


@dataclass
class UpdateStateNotifier:
    _device_update_info: DeviceUpdateInfo
    _update_state: UpdateState
    _update_notifier: UpdateNotifier = field(default_factory=lambda: UpdateNotifier(30))

    def set_progress(self, progress: int) -> None:
        self._device_update_info.progress = progress
        if self._update_notifier.should_notify(progress):
            self._update_state.update(self._device_update_info)

    def set_error_from_exception(self, exception: Exception) -> None:
        if isinstance(exception, SerialRPCTimeoutException):
            self._device_update_info.error = RPCCallTimeoutStateError()
        elif isinstance(exception, SerialTimeoutException):
            self._device_update_info.error = DeviceResponseTimeoutError()
        elif isinstance(exception, RemoteFileDownloadingError):
            self._device_update_info.error = FileDownloadError()
        else:
            self._device_update_info.error = GenericStateError()
            self._device_update_info.error.metadata = {"exception": str(exception)}
        self._update_state.update(self._device_update_info)

    def delete(self, should_notify: bool = True) -> None:
        self._update_state.remove(self._device_update_info, should_notify)


async def write_fw_data_block(serial_device: Device, chunk: bytes) -> None:
    """
    Writes a firmware data block to the serial device.
    The device must be in bootloader mode.
    Retries writing the chunk up to 3 times if there is a failure.
    Slave device failure (0x04) Modbus exception is a successful write.
    It means that the chunk is already written.

    Args:
        serial_device (Device): The serial device to write the firmware chunk to.
        chunk (bytes): The firmware chunk to write.

    Raises:
        SerialExceptionBase derived exception: If there is a failure during flashing.
    """
    MAX_ERRORS = 3  # pylint: disable=invalid-name
    exception = None
    for _ in range(MAX_ERRORS):
        try:
            await serial_device.write(WB_DEVICE_PARAMETERS["fw_data_block"], chunk)
            return
        except WBModbusException as e:
            # The device sends slave device failure (0x04) if the chunk is already written
            if e.code == ModbusExceptionCode.SLAVE_DEVICE_FAILURE:
                return
            exception = e
        except SerialExceptionBase as e:
            # Could be an error during transmission, retry
            exception = e
    if exception is not None:
        raise exception


async def flash_fw(
    serial_device: Device, parsed_wbfw: ParsedWBFW, progress_notifier: UpdateStateNotifier
) -> None:
    """
    Flash firmware to a serial device. The device must be in bootloader mode.

    Args:
        serial_device (Device): The serial device to flash the firmware to.
        parsed_wbfw (ParsedWBFW): The parsed firmware data.
        progress_notifier (UpdateStateNotifier): The progress notifier for tracking the update state.

    Raises:
        SerialExceptionBase derived exception: If there is a failure during flashing.
    """

    # Bl needs some time to perform info-block magic
    info_block_timeout_s = 1.0
    await serial_device.write(WB_DEVICE_PARAMETERS["fw_info_block"], parsed_wbfw.info, info_block_timeout_s)

    data_chunk_length = WB_DEVICE_PARAMETERS["fw_data_block"].register_count * 2
    chunks = [
        parsed_wbfw.data[i : i + data_chunk_length]
        for i in range(0, len(parsed_wbfw.data), data_chunk_length)
    ]

    for index, chunk in enumerate(chunks):
        await write_fw_data_block(serial_device, chunk)
        progress_notifier.set_progress(int((index + 1) * 100 / len(chunks)))


async def reboot_to_bootloader(
    serial_device: Device, bootloader_can_preserve_port_settings: bool = False
) -> None:
    """
    Reboots the device to the bootloader. The device must be in firmware mode.

    Args:
        serial_device (Device): The serial device to communicate with.
        bootloader_can_preserve_port_settings (bool):
            Whether the bootloader can preserve port settings. Default is False.

    Raises:
        SerialExceptionBase derived exception: If there is a failure
    """

    reboot_timeout_s = 1
    if bootloader_can_preserve_port_settings:
        await serial_device.write(
            WB_DEVICE_PARAMETERS["reboot_to_bootloader_preserve_port_settings"], 1, reboot_timeout_s
        )
    else:
        try:
            await serial_device.write(WB_DEVICE_PARAMETERS["reboot_to_bootloader"], 1, reboot_timeout_s)
        except SerialTimeoutException:
            # Device has rebooted and doesn't send response (Fixed in latest FWs)
            logger.debug("Device doesn't send response to reboot command, probably it has rebooted")
        serial_device.set_default_port_settings()

    # Delay before going to bootloader
    await asyncio.sleep(0.5)


async def update_software(
    serial_device: Device,
    update_state_notifier: UpdateStateNotifier,
    software: SoftwareComponent,
    binary_downloader: BinaryDownloader,
    bootloader_can_preserve_port_settings: bool = False,
) -> bool:
    """
    Updates the software of a device. The device must be in firmware mode.

    Args:
        serial_device (Device): The serial device to update.
        update_state_notifier (UpdateStateNotifier): The notifier to update the state of the update process.
        software (SoftwareComponent): The software component to update.
        binary_downloader (BinaryDownloader): The downloader to download the binary file.
        bootloader_can_preserve_port_settings (bool, optional):
            Whether the bootloader can preserve port settings. Defaults to False.

    Returns:
        bool: True if the update was successful, False otherwise.
    """

    update_state_notifier.set_progress(0)
    device_model = ""
    sn = ""

    try:
        device_model = get_human_readable_device_model(await read_device_model(serial_device))
        sn = await read_sn(serial_device, device_model)
        if software.flashing_options.reboot_to_bootloader:
            await reboot_to_bootloader(serial_device, bootloader_can_preserve_port_settings)

        await flash_fw(
            serial_device,
            download_wbfw(binary_downloader, software.available.endpoint),
            update_state_notifier,
        )
    except (WBRemoteStorageError, SerialExceptionBase) as e:
        update_state_notifier.set_error_from_exception(e)
        logger.error(
            "%s (sn: %d, %s) %s update from %s to %s failed: %s",
            device_model,
            sn,
            serial_device.description,
            software.type.value,
            software.current_version,
            software.available.version,
            e,
        )
        return False
    logger.info(
        "%s (sn: %d, %s) %s update from %s to %s completed",
        device_model,
        sn,
        serial_device.description,
        software.type.value,
        software.current_version,
        software.available.version,
    )
    return True


async def restore_firmware(
    serial_device: Device,
    update_state_notifier: UpdateStateNotifier,
    firmware: ReleasedBinary,
    binary_downloader: BinaryDownloader,
) -> bool:
    """
    Restores the firmware of a serial device. The device must be in bootloader mode.

    Args:
        serial_device (Device): The serial device to restore the firmware for.
        update_state_notifier (UpdateStateNotifier): The notifier to update the state of the firmware update.
        firmware (ReleasedBinary): Information about the firmware to restore.
        binary_downloader (BinaryDownloader): The binary downloader to download the firmware.
    """

    update_state_notifier.set_progress(0)
    try:
        await flash_fw(
            serial_device,
            download_wbfw(binary_downloader, firmware.endpoint),
            update_state_notifier,
        )
    except (WBRemoteStorageError, SerialExceptionBase) as e:
        update_state_notifier.set_error_from_exception(e)
        logger.error("Firmware restore of %s failed: %s", serial_device.description, e)
        return False
    update_state_notifier.delete()
    logger.info("Firmware of device %s is restored to %s", serial_device.description, firmware.version)
    return True


async def update_components(
    serial_device: Device,
    state: UpdateState,
    binary_downloader: BinaryDownloader,
    components_info: dict[int, ComponentInfo],
) -> None:
    logger.debug("Start components update for %d %s", serial_device.slave_id, serial_device.get_port_config())
    for component_number, component_info in components_info.items():
        logger.debug("Update component %d: %s", component_number, component_info)

        update_notifier = UpdateStateNotifier(
            make_device_update_info(serial_device, component_info, component_number), state
        )
        if await update_software(
            serial_device,
            update_notifier,
            component_info,
            binary_downloader,
        ):
            update_notifier.delete()


def get_human_readable_device_model(device_model: str) -> str:
    """
    Returns the human-readable version of the device model read by read_device_model function.

    Args:
        device_model (str): The device model read by read_device_model function.

    Returns:
        str: The human-readable device model.
             Example: MAP12\x02E -> MAP12E
    """
    return device_model.replace("\x02", "")


async def read_device_model(serial_device: Device) -> str:
    """
    Reads the device model from the specified serial device.

    Args:
        serial_device (Device): The serial device to read from.

    Returns:
        str: The device model. An empty string is returned if the model can't be read.
             The string has not stripped 0x02 characters for devices with firmwares v2.
             Example: MAP12\x02E
    """
    try:
        return cast(str, await serial_device.read(WB_DEVICE_PARAMETERS["device_model_extended"]))
    except SerialExceptionBase as err:
        logger.debug("Can't read extended device model: %s", err)

    # Old devices have only standard model registers, try to read them
    try:
        return cast(str, await serial_device.read(WB_DEVICE_PARAMETERS["device_model"]))
    except SerialExceptionBase as err2:
        logger.debug("Can't read device model: %s", err2)
    return ""


async def read_sn(serial_device: Device, device_model: str) -> int:
    try:
        sn = int.from_bytes(
            cast(bytes, await serial_device.read(WB_DEVICE_PARAMETERS["sn"])), byteorder="big"
        )
        # WB-MAP* uses 25 bit for serial number
        if WBMAP_MARKER.match(device_model):
            return sn - 0xFE000000
        return sn
    except SerialExceptionBase as err:
        logger.debug("Can't read SN: %s", err)
    return 0


class PollingManager:
    def __init__(self, serial_device: Device) -> None:
        self._serial_device = serial_device

    async def __aenter__(self):
        try:
            await self._serial_device.set_poll(False)
        except SerialExceptionBase:
            # Failing to disable polling is acceptable: the firmware update will still proceed,
            # though it may take longer due to ongoing polling interference.
            pass

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            await self._serial_device.set_poll(True)
        except SerialExceptionBase:
            # not a problem too, wb-mqtt-serial has internal timeout to restart poll
            pass


def make_device_update_info(
    serial_device: Device, software_component: SoftwareComponent, component_number: int = None
) -> DeviceUpdateInfo:
    res = DeviceUpdateInfo(
        port=Port(serial_device.get_port_config()),
        protocol=serial_device.protocol,
        slave_id=serial_device.slave_id,
        to_version=software_component.available.version,
        from_version=software_component.current_version,
        type=software_component.type,
    )
    if component_number is not None:
        res.component_number = component_number
        res.component_model = software_component.model
    return res


class FirmwareUpdater:
    STATE_PUBLISH_TOPIC = "/wb-device-manager/firmware_update/state"

    def __init__(  # pylint: disable=too-many-arguments
        self,
        mqtt_connection,
        serial_rpc: SerialRPCWrapper,
        asyncio_loop,
        fw_info_reader: FirmwareInfoReader,
        binary_downloader: BinaryDownloader,
    ) -> None:
        self._serial_rpc = serial_rpc
        self._asyncio_loop = asyncio_loop
        self._state = UpdateState(mqtt_connection, self.STATE_PUBLISH_TOPIC)
        self._update_software_task = None
        self._fw_info_reader = fw_info_reader
        self._binary_downloader = binary_downloader

    async def _catch_all_exceptions(self, task, message):
        try:
            await task
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception("%s: %s", message, e)

    def _get_slave_id(self, **kwargs) -> int:
        slave_id = kwargs.get("slave_id")
        if not isinstance(slave_id, int):
            raise JSONRPCDispatchException(
                code=MQTTRPCErrorCode.REQUEST_HANDLING_ERROR.value, message="Invalid slave_id"
            )
        return slave_id

    async def get_firmware_info(self, **kwargs) -> dict:
        """
        MQTT RPC handler. Retrieves firmware information for a device.

        Args:
            **kwargs: Additional keyword arguments.
                slave_id: Modbus slave ID.
                port (dict): The port configuration.
                protocol (str): The Modbus protocol to use.

        Returns:
            dict: A dictionary containing the firmware information.
                fw (str): The current firmware version.
                available_fw (str): The available firmware version.
                can_update (bool): Indicates if the firmware can be updated.
                fw_has_update (bool): Indicates if available firmware is newer than current
                bootloader (str): The current bootloader version.
                available_bootloader (str): The available bootloader version.
                bootloader_has_update (bool): Indicates if available bootloader is newer than current
                components (dict[int, dict]): A dictionary containing the components info
                  firmware versions for the components.
                    number (int): The component order number.
                        model (str): The human-readable component model.
                        fw (str): The current firmware version.
                        available_fw (str): The available firmware version.
                        has_update (bool): Indicates if available firmware is newer than current
                model (str): The device model.
        """

        logger.debug("Request firmware info")
        serial_device = create_device_from_json(kwargs, self._serial_rpc)
        if self._state.is_updating(serial_device.slave_id, Port(serial_device.get_port_config())):
            raise MQTTRPCAlreadyProcessingException()
        res = {
            "fw": "",
            "available_fw": "",
            "can_update": False,
            "fw_has_update": False,
            "bootloader": "",
            "available_bootloader": "",
            "bootloader_has_update": False,
            "components": {},
            "model": "",
        }

        try:
            res["fw"] = await self._fw_info_reader.read_fw_version(serial_device)
        except SerialExceptionBase as err:
            logger.warning("Can't get firmware info for %s: %s", serial_device.description, err)
            raise JSONRPCDispatchException(
                code=MQTTRPCErrorCode.REQUEST_HANDLING_ERROR.value, message=str(err)
            ) from err

        res["model"] = get_human_readable_device_model(await read_device_model(serial_device))

        try:
            signature = await self._fw_info_reader.read_fw_signature(serial_device)
        except SerialExceptionBase as err:
            logger.warning("Can't get firmware signature for %s: %s", serial_device.description, err)
            return res

        # Can't update WB-MSW-LORA devices, so don't check new firmware
        if signature in ["msw5GL", "msw3G419L"]:
            return res

        try:
            res["available_fw"] = self._fw_info_reader.read_released_fw(signature).version
        except NoReleasedFwError as err:
            logger.warning("Can't get released firmware info for %s: %s", serial_device.description, err)

        res["fw_has_update"] = firmware_is_newer(res["fw"], res["available_fw"])

        bootloader = await self._fw_info_reader.read_bootloader_info(serial_device, signature)
        res["bootloader"] = bootloader.current_version
        res["available_bootloader"] = bootloader.available.version if bootloader.available is not None else ""
        res["bootloader_has_update"] = firmware_is_newer(res["bootloader"], res["available_bootloader"])

        try:
            res["can_update"] = await serial_device.check_updatable(bootloader.can_preserve_port_settings)
        except SerialExceptionBase as err:
            logger.warning("Can't check if firmware for %s is updatable: %s", serial_device.description, err)

        try:
            components_info = await self._fw_info_reader.read_components_info(serial_device)
            for number, component in components_info.items():
                res["components"][number] = {
                    "model": component.model,
                    "fw": component.current_version,
                    "available_fw": component.available.version,
                    "has_update": component_firmware_is_newer(
                        component.current_version, component.available.version
                    ),
                }
        except (NoReleasedFwError, SerialExceptionBase) as err:
            logger.warning(
                "Can't get components info for %s (%s): %s",
                serial_device.slave_id,
                serial_device.get_port_config(),
                err,
            )

        return res

    async def update_software(self, **kwargs):
        """
        MQTT RPC handler. Starts a software update for a device. The device must be in bootloader mode.

        Args:
            **kwargs: Additional keyword arguments.
                slave_id (int): Modbus slave ID.
                port (dict): The port configuration.
                type (str): The type of software to update. Can be "firmware"/"bootloader"/"components".
                protocol (str): The Modbus protocol to use.

        Raises:
            MQTTRPCAlreadyProcessingException: If a software update is already in progress.
            ValueError: If firmware update over TCP is not possible.

        Returns:
            str: "Ok" if the update was started.
        """

        if self._update_software_task and not self._update_software_task.done():
            raise MQTTRPCAlreadyProcessingException()

        software_type = SoftwareType(kwargs.get("type", SoftwareType.FIRMWARE.value))
        logger.debug("Start %s update", software_type.value)
        serial_device = create_device_from_json(kwargs, self._serial_rpc)
        fw_info = await self._fw_info_reader.read(serial_device)
        if software_type != SoftwareType.COMPONENT and not await serial_device.check_updatable(
            fw_info.bootloader.can_preserve_port_settings
        ):
            raise ValueError("Can't update firmware over TCP")

        components_info = await self._fw_info_reader.read_components_info(serial_device)
        components_to_update = {
            n: c
            for n, c in components_info.items()
            if component_firmware_is_newer(c.current_version, c.available.version)
        }

        if software_type == SoftwareType.BOOTLOADER:
            self._update_software_task = self._asyncio_loop.create_task(
                self._catch_all_exceptions(
                    self._update_bootloader(serial_device, fw_info, components_info),
                    "Bootloader update failed",
                ),
                name="Update bootloader (long running)",
            )
        elif software_type == SoftwareType.FIRMWARE:
            self._update_software_task = self._asyncio_loop.create_task(
                self._catch_all_exceptions(
                    self._update_firmware(serial_device, fw_info, components_info),
                    "Firmware update failed",
                ),
                name="Update firmware (long running)",
            )
        else:
            self._update_software_task = self._asyncio_loop.create_task(
                self._catch_all_exceptions(
                    self._update_components(serial_device, components_to_update),
                    "Component update failed",
                ),
                name="Update component (long running)",
            )

        return "Ok"

    async def clear_error(self, **kwargs):
        """
        MQTT RPC handler. Clears the software update error for a specific device.

        Args:
            **kwargs: Additional keyword arguments.
                slave_id (int): Modbus slave ID.
                port (dict): The port object with path to the device.
                type (str): The type of software. Can be "firmware" or "bootloader".

        Returns:
            str: "Ok" if the error was cleared.
        """

        slave_id = self._get_slave_id(**kwargs)
        port = Port(kwargs.get("port", {}).get("path"))
        software_type = SoftwareType(kwargs.get("type", SoftwareType.FIRMWARE.value))
        logger.debug("Clear error: %d %s", slave_id, port.path)
        self._state.clear_error(slave_id, port, software_type)
        return "Ok"

    async def restore_firmware(self, **kwargs):
        """
        MQTT RPC handler. Restores the firmware of a device. The device must be in bootloader mode.

        Args:
            **kwargs: Additional keyword arguments.
                slave_id (int): Modbus slave ID.
                port (dict): The port configuration.

        Returns:
            str: "Ok" if the firmware restore was started or the device is not in bootloader mode.

        Raises:
            MQTTRPCAlreadyProcessingException: If a firmware update is already in progress.
        """

        if self._update_software_task and not self._update_software_task.done():
            raise MQTTRPCAlreadyProcessingException()
        logger.debug("Start firmware restore")
        serial_device = create_device_from_json(kwargs, self._serial_rpc)
        if not await is_in_bootloader_mode(serial_device):
            return "Ok"
        fw_info = await self._fw_info_reader.read(serial_device, True)
        self._update_software_task = self._asyncio_loop.create_task(
            self._catch_all_exceptions(
                self._restore_firmware(serial_device, fw_info),
                "Firmware restore failed",
            ),
            name="Restore firmware (long running)",
        )
        return "Ok"

    async def _update_firmware(
        self, serial_device: Device, fw_info: FirmwareInfo, components_info: dict[int, ComponentInfo]
    ) -> None:
        """
        Asyncio task body to update the firmware of a device.

        Args:
            serial_device (Device): The serial device to update.
            fw_info (FirmwareInfo): Information about the firmware.
        """
        first_update_notifier = UpdateStateNotifier(
            make_device_update_info(serial_device, fw_info), self._state
        )
        async with PollingManager(serial_device):
            logger.debug(
                "Start firmware update for %d %s", serial_device.slave_id, serial_device.get_port_config()
            )
            update_result = await update_software(
                serial_device,
                first_update_notifier,
                fw_info,
                self._binary_downloader,
                fw_info.bootloader.can_preserve_port_settings,
            )
            if not update_result:
                return

            first_update_notifier.delete()
            await asyncio.sleep(1)
            await update_components(
                serial_device,
                self._state,
                self._binary_downloader,
                components_info,
            )

    async def _update_bootloader(
        self, serial_device: Device, fw_info: FirmwareInfo, components_info: dict[int, ComponentInfo]
    ) -> None:
        """
        Asyncio task body to update the bootloader of a device.

        Args:
            serial_device (Device): The serial device to update.
            port_config (Union[SerialConfig, TcpConfig]): The configuration of the device's port.
            fw_info (FirmwareInfo): Information about the firmware.
        """

        first_update_notifier = UpdateStateNotifier(
            make_device_update_info(serial_device, fw_info.bootloader), self._state
        )
        async with PollingManager(serial_device):
            logger.debug(
                "Start bootloader update for %d %s", serial_device.slave_id, serial_device.get_port_config()
            )
            update_result = await update_software(
                serial_device,
                first_update_notifier,
                fw_info.bootloader,
                self._binary_downloader,
                fw_info.bootloader.can_preserve_port_settings,
            )

            if not update_result:
                return

            first_update_notifier.delete(False)
            fw_update_notifier = UpdateStateNotifier(
                make_device_update_info(serial_device, fw_info), self._state
            )
            await asyncio.sleep(1)
            logger.debug(
                "Start firmware update for %d %s", serial_device.slave_id, serial_device.get_port_config()
            )
            restore_result = await restore_firmware(
                serial_device, fw_update_notifier, fw_info.available, self._binary_downloader
            )
            if not restore_result:
                return

            await asyncio.sleep(1)
            await update_components(
                serial_device,
                self._state,
                self._binary_downloader,
                components_info,
            )

    async def _update_components(
        self,
        serial_device: Device,
        components_info: dict[int, ComponentInfo],
    ) -> None:
        """
        Asyncio task body to update the components of a device.

        Args:
            slave_id (int): The ID of the device to update.
            port_config (Union[SerialConfig, TcpConfig]): The configuration of the device's port.
            fw_info (FirmwareInfo): Information about the firmware.

        """
        if len(components_info.keys()) == 0:
            return
        async with PollingManager(serial_device):
            await update_components(
                serial_device,
                self._state,
                self._binary_downloader,
                components_info,
            )

    async def _restore_firmware(self, serial_device: Device, fw_info: FirmwareInfo) -> None:
        """
        Asyncio task body to restore the firmware of a device in bootloader mode.

        Args:
            serial_device (Device): The serial device to restore the firmware.
            fw_info (FirmwareInfo): Information about the firmware.
        """

        first_update_notifier = UpdateStateNotifier(
            make_device_update_info(serial_device, fw_info), self._state
        )
        async with PollingManager(serial_device):
            await restore_firmware(
                serial_device, first_update_notifier, fw_info.available, self._binary_downloader
            )

    def publish_state(self):
        return self._state.publish_state()

    def clear_state(self):
        return self._state.clear_state()

    def start(self) -> None:
        self._state.reset()
