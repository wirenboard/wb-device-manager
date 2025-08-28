#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional, Union, cast

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
    get_bootloader_info,
    get_released_fw,
)
from .mqtt_rpc import MQTTRPCAlreadyProcessingException, MQTTRPCErrorCode
from .releases import parse_releases
from .serial_device import Device, create_device
from .serial_rpc import (
    WB_DEVICE_PARAMETERS,
    ModbusExceptionCode,
    ModbusProtocol,
    SerialConfig,
    SerialExceptionBase,
    SerialRPCTimeoutException,
    SerialRPCWrapper,
    SerialTimeoutException,
    TcpConfig,
    WBModbusException,
)
from .state_error import (
    DeviceResponseTimeoutError,
    FileDownloadError,
    GenericStateError,
    RPCCallTimeoutStateError,
    StateError,
)
from .ttl_lru_cache import ttl_lru_cache

WBMAP_MARKER = re.compile(r"\S*MAP\d+\S*")  # *MAP%d* matches


class SoftwareType(Enum):
    FIRMWARE = "firmware"
    BOOTLOADER = "bootloader"


@dataclass
class DeviceUpdateInfo:
    port: Port
    slave_id: int
    to_version: str
    progress: int = 0
    from_version: Optional[str] = None
    type: SoftwareType = SoftwareType.FIRMWARE
    error: Optional[StateError] = None

    def __eq__(self, o):
        return self.slave_id == o.slave_id and self.port == o.port and self.type == o.type


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
    return d


@dataclass
class SoftwareComponent:
    type: SoftwareType = SoftwareType.FIRMWARE
    current_version: Optional[str] = None
    available: Optional[ReleasedBinary] = None


@dataclass
class BootloaderInfo(SoftwareComponent):
    can_preserve_port_settings: bool = False
    type: SoftwareType = SoftwareType.BOOTLOADER


@dataclass
class FirmwareInfo(SoftwareComponent):
    signature: str = ""
    type: SoftwareType = SoftwareType.FIRMWARE
    bootloader: BootloaderInfo = field(default_factory=BootloaderInfo)


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


def read_port_config(port: dict) -> Union[SerialConfig, TcpConfig]:
    return TcpConfig(**port) if "address" in port else SerialConfig(**port)


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

    async def read_bootloader(self, serial_device: Device, fw_signature: str) -> BootloaderInfo:
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
            res.available = get_bootloader_info(fw_signature, self._downloader)
        except (SerialExceptionBase, WBRemoteStorageError) as err:
            logger.debug("Can't get bootloader information for %s: %s", serial_device.description, err)
        return res

    async def read_fw_signature(self, serial_device: Device) -> str:
        return cast(str, await serial_device.read(WB_DEVICE_PARAMETERS["fw_signature"]))

    async def read_fw_version(self, serial_device: Device) -> str:
        return cast(str, await serial_device.read(WB_DEVICE_PARAMETERS["fw_version"]))

    def read_released_fw(self, signature: str) -> ReleasedBinary:
        return get_released_fw(
            signature, parse_releases("/usr/lib/wb-release").get("SUITE", ""), self._downloader
        )

    async def read(self, serial_device: Device, bootloader_mode: bool = False) -> FirmwareInfo:
        res = FirmwareInfo()
        res.signature = await self.read_fw_signature(serial_device)
        logger.debug("Get firmware info for: %s", res.signature)
        res.available = self.read_released_fw(res.signature)
        if not bootloader_mode:
            res.current_version = await self.read_fw_version(serial_device)
            res.bootloader = await self.read_bootloader(serial_device, res.signature)
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
    device_model = get_human_readable_device_model(await read_device_model(serial_device))
    sn = await read_sn(serial_device, device_model)
    try:
        await serial_device.set_poll(False)  # suspend device poll
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
    finally:
        await serial_device.set_poll(True)  # resume device poll
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
) -> None:
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
        await serial_device.set_poll(False)  # suspend device poll
        await flash_fw(
            serial_device,
            download_wbfw(binary_downloader, firmware.endpoint),
            update_state_notifier,
        )
    except (WBRemoteStorageError, SerialExceptionBase) as e:
        update_state_notifier.set_error_from_exception(e)
        logger.error("Firmware restore of %s failed: %s", serial_device.description, e)
        return
    finally:
        await serial_device.set_poll(True)  # resume device poll
    update_state_notifier.delete()
    logger.info("Firmware of device %s is restored to %s", serial_device.description, firmware.version)


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


def make_device_update_info(serial_device: Device, software_component: SoftwareComponent) -> DeviceUpdateInfo:
    to_version = software_component.available.version if software_component.available else "unknown"
    res = DeviceUpdateInfo(
        port=Port(serial_device.get_port_config()),
        slave_id=serial_device.slave_id,
        to_version=to_version,
        from_version=software_component.current_version,
    )
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

    def _get_serial_device_from_request(self, **kwargs) -> Device:
        port_config = read_port_config(kwargs.get("port", {}))
        protocol = ModbusProtocol(kwargs.get("protocol", ModbusProtocol.MODBUS_RTU.value))
        slave_id = self._get_slave_id(**kwargs)
        return create_device(port_config, protocol, slave_id, self._serial_rpc)

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
                bootloader (str): The current bootloader version.
                available_bootloader (str): The available bootloader version.
                model (str): The device model.
        """

        logger.debug("Request firmware info")
        serial_device = self._get_serial_device_from_request(**kwargs)
        if self._state.is_updating(serial_device.slave_id, Port(serial_device.get_port_config())):
            raise MQTTRPCAlreadyProcessingException()
        res = {
            "fw": "",
            "available_fw": "",
            "can_update": False,
            "bootloader": "",
            "available_bootloader": "",
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

        try:
            res["available_fw"] = self._fw_info_reader.read_released_fw(signature).version
        except NoReleasedFwError as err:
            logger.warning("Can't get released firmware info for %s: %s", serial_device.description, err)

        bootloader = await self._fw_info_reader.read_bootloader(serial_device, signature)
        res["bootloader"] = bootloader.current_version
        res["available_bootloader"] = bootloader.available.version if bootloader.available is not None else ""

        try:
            res["can_update"] = await serial_device.check_updatable(bootloader.can_preserve_port_settings)
        except SerialExceptionBase as err:
            logger.warning("Can't check if firmware for %s is updatable: %s", serial_device.description, err)

        return res

    async def update_software(self, **kwargs):
        """
        MQTT RPC handler. Starts a software update for a device. The device must be in bootloader mode.

        Args:
            **kwargs: Additional keyword arguments.
                slave_id (int): Modbus slave ID.
                port (dict): The port configuration.
                type (str): The type of software to update. Can be "firmware" or "bootloader".
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
        serial_device = self._get_serial_device_from_request(**kwargs)
        fw_info = await self._fw_info_reader.read(serial_device)
        if not await serial_device.check_updatable(fw_info.bootloader.can_preserve_port_settings):
            raise ValueError("Can't update firmware over TCP")
        if software_type == SoftwareType.BOOTLOADER:
            self._update_software_task = self._asyncio_loop.create_task(
                self._catch_all_exceptions(
                    self._update_bootloader(serial_device, fw_info),
                    "Bootloader update failed",
                ),
                name="Update bootloader (long running)",
            )
        else:
            self._update_software_task = self._asyncio_loop.create_task(
                self._catch_all_exceptions(
                    self._update_firmware(serial_device, fw_info),
                    "Firmware update failed",
                ),
                name="Update firmware (long running)",
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
        serial_device = self._get_serial_device_from_request(**kwargs)
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

    async def _update_firmware(self, serial_device: Device, fw_info: FirmwareInfo) -> None:
        """
        Asyncio task body to update the firmware of a device.

        Args:
            serial_device (Device): The serial device to update.
            fw_info (FirmwareInfo): Information about the firmware.
        """

        update_notifier = UpdateStateNotifier(make_device_update_info(serial_device, fw_info), self._state)
        if await update_software(
            serial_device,
            update_notifier,
            fw_info,
            self._binary_downloader,
            fw_info.bootloader.can_preserve_port_settings,
        ):
            update_notifier.delete()

    async def _update_bootloader(self, serial_device: Device, fw_info: FirmwareInfo) -> None:
        """
        Asyncio task body to update the bootloader of a device.

        Args:
            serial_device (Device): The serial device to update.
            port_config (Union[SerialConfig, TcpConfig]): The configuration of the device's port.
            fw_info (FirmwareInfo): Information about the firmware.
        """

        bootloader_update_notifier = UpdateStateNotifier(
            make_device_update_info(serial_device, fw_info.bootloader), self._state
        )
        if await update_software(
            serial_device,
            bootloader_update_notifier,
            fw_info.bootloader,
            self._binary_downloader,
            fw_info.bootloader.can_preserve_port_settings,
        ):
            bootloader_update_notifier.delete(False)
            fw_update_notifier = UpdateStateNotifier(
                make_device_update_info(serial_device, fw_info), self._state
            )
            await asyncio.sleep(1)
            await restore_firmware(
                serial_device, fw_update_notifier, fw_info.available, self._binary_downloader
            )

    async def _restore_firmware(self, serial_device: Device, fw_info: FirmwareInfo) -> None:
        """
        Asyncio task body to restore the firmware of a device in bootloader mode.

        Args:
            serial_device (Device): The serial device to restore the firmware.
            fw_info (FirmwareInfo): Information about the firmware.
        """

        update_notifier = UpdateStateNotifier(make_device_update_info(serial_device, fw_info), self._state)
        await restore_firmware(serial_device, update_notifier, fw_info.available, self._binary_downloader)

    def publish_state(self):
        return self._state.publish_state()

    def clear_state(self):
        return self._state.clear_state()

    def start(self) -> None:
        self._state.reset()
