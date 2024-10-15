#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional, Union

from jsonrpc.exceptions import JSONRPCDispatchException

from . import logger
from .bootloader_scan import is_in_bootloader_mode
from .bus_scan_state import Port
from .fw_downloader import (
    BinaryDownloader,
    ReleasedBinary,
    RemoteFileReadingError,
    get_bootloader_info,
    get_released_fw,
)
from .mqtt_rpc import MQTTRPCAlreadyProcessingException, MQTTRPCErrorCode
from .releases import parse_releases
from .serial_bus import fix_sn
from .serial_rpc import (
    WB_DEVICE_PARAMETERS,
    ParameterConfig,
    SerialConfig,
    SerialExceptionBase,
    SerialRPCWrapper,
    SerialTimeoutException,
    TcpConfig,
)


class SoftwareType(Enum):
    FIRMWARE = "firmware"
    BOOTLOADER = "bootloader"


@dataclass
class StateError:
    message: Optional[str] = None


@dataclass
class DeviceUpdateInfo:
    port: Port
    slave_id: int
    progress: int = 0
    from_version: Optional[str] = None
    to_version: str
    type: SoftwareType = SoftwareType.FIRMWARE
    error: StateError = field(default_factory=StateError)

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

    def is_updating(self, slave_id: int, port: Port, software_type: SoftwareType) -> bool:
        return any(
            d.slave_id == slave_id
            and d.port == port
            and d.type == software_type
            and d.progress < 100
            and d.error.message is None
            for d in self._devices
        )

    def clear_error(self, slave_id: int, port: Port, software_type: SoftwareType) -> None:
        for d in self._devices:
            if (
                d.slave_id == slave_id
                and d.port == port
                and d.type == software_type
                and d.error.message is not None
            ):
                self._devices.remove(d)
                self._mqtt_connection.publish(self._topic, self._to_json_string(), retain=True)
                return

    def reset(self) -> None:
        self._devices = []
        self._mqtt_connection.publish(self._topic, self._to_json_string(), retain=True)


def to_dict_for_json(device_update_info: DeviceUpdateInfo) -> dict:
    d = asdict(device_update_info)
    if device_update_info.error.message is None:
        del d["error"]
    d["type"] = device_update_info.type.value
    return d


@dataclass
class SoftwareComponent:
    type: SoftwareType = SoftwareType.FIRMWARE
    current_version: Optional[str] = None
    available: ReleasedBinary = None


@dataclass
class BootloaderInfo(SoftwareComponent):
    can_preserve_port_settings: bool = False
    type: SoftwareType = SoftwareType.BOOTLOADER


@dataclass
class FirmwareInfo(SoftwareComponent):
    signature: str
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


def read_port_config(port: dict) -> Union[SerialConfig, TcpConfig]:
    return TcpConfig(**port) if "address" in port else SerialConfig(**port)


class UpdateNotifier:
    step: int = -1
    notification_step: int = 1

    def __init__(self, notifications_count: int):
        self.notification_step = max(100 / notifications_count, 1)

    def should_notify(self, progress_percent: int) -> bool:
        if progress_percent == 100:
            return True
        current_step = int(progress_percent / self.notification_step)
        if current_step > self.step:
            self.step = current_step
            return True
        return False


class FirmwareInfoReader:
    def __init__(self, serial_rpc, downloader: BinaryDownloader) -> None:
        self._serial_rpc = serial_rpc
        self._downloader = downloader

    async def _read_bootloader(
        self, port_config: Union[SerialConfig, TcpConfig], slave_id: int, fw_signature: str
    ) -> BootloaderInfo:
        res = BootloaderInfo()
        try:
            res.can_preserve_port_settings = (
                await self._serial_rpc.read(
                    port_config, slave_id, WB_DEVICE_PARAMETERS["reboot_to_bootloader_preserve_port_settings"]
                )
                == 0
            )
        except SerialExceptionBase:
            pass
        try:
            res.current_version = await self._serial_rpc.read(
                port_config, slave_id, WB_DEVICE_PARAMETERS["bootloader_version"]
            )
            res.available = get_bootloader_info(fw_signature, self._downloader)
        except (SerialExceptionBase, RemoteFileReadingError) as err:
            logger.debug("Can't get bootloader information for %d %s: %s", slave_id, port_config, err)
        return res

    async def read(
        self, port_config: Union[SerialConfig, TcpConfig], slave_id: int, bootloader_mode: bool = False
    ) -> FirmwareInfo:
        res = FirmwareInfo()
        res.signature = await self._serial_rpc.read(
            port_config, slave_id, WB_DEVICE_PARAMETERS["fw_signature"]
        )
        logger.debug("Get firmware info for: %s", res.signature)
        res.available = get_released_fw(
            res.signature, parse_releases("/usr/lib/wb-release").get("SUITE"), self._downloader
        )
        if not bootloader_mode:
            res.current_version = await self._serial_rpc.read(
                port_config, slave_id, WB_DEVICE_PARAMETERS["fw_version"]
            )
            res.bootloader = await self._read_bootloader(port_config, slave_id, res.signature)
        return res


class SerialDevice:
    def __init__(
        self, serial_rpc: SerialRPCWrapper, port_config: Union[SerialConfig, TcpConfig], slave_id: int
    ) -> None:
        self._serial_rpc = serial_rpc
        self._port_config = port_config
        self._slave_id = slave_id

    async def write(
        self, param_config: ParameterConfig, value: Union[int, bytes], response_timeout_s: float = None
    ) -> None:
        await self._serial_rpc.write(
            self._port_config, self._slave_id, param_config, value, response_timeout_s
        )

    async def read(self, param_config: ParameterConfig) -> Union[str, int, bytes]:
        return await self._serial_rpc.read(self._port_config, self._slave_id, param_config)

    def set_default_port_settings(self) -> None:
        if isinstance(self._port_config, SerialConfig):
            self._port_config.set_default_settings()

    def get_description(self) -> str:
        return f"slave id: {self._slave_id}, {self._port_config}"


class UpdateStateNotifier:
    def __init__(self, device_update_info: DeviceUpdateInfo, update_state: UpdateState) -> None:
        self._device_update_info = device_update_info
        self._update_notifier = UpdateNotifier(30)
        self._update_state = update_state

    def set_progress(self, progress: int) -> None:
        self._device_update_info.progress = progress
        if self._update_notifier.should_notify(progress):
            self._update_state.update(self._device_update_info)

    def set_error(self, error: str) -> None:
        self._device_update_info.error.message = error
        self._update_state.update(self._device_update_info)

    def delete(self, should_notify: bool = True) -> None:
        self._update_state.remove(self._device_update_info, should_notify)


async def flash_fw(
    serial_device: SerialDevice, parsed_wbfw: ParsedWBFW, progress_notifier: UpdateStateNotifier
) -> None:
    # Bl needs some time to perform info-block magic
    info_block_timeout_s = 1.0
    await serial_device.write(WB_DEVICE_PARAMETERS["fw_info_block"], parsed_wbfw.info, info_block_timeout_s)

    data_chunk_length = WB_DEVICE_PARAMETERS["fw_data_block"].register_count * 2
    chunks = [
        parsed_wbfw.data[i : i + data_chunk_length]
        for i in range(0, len(parsed_wbfw.data), data_chunk_length)
    ]

    # Due to bootloader's behavior, actual flashing failure is current-chunk failure + next-chunk failure
    has_previous_chunk_failed = False

    for index, chunk in enumerate(chunks):
        try:
            await serial_device.write(WB_DEVICE_PARAMETERS["fw_data_block"], chunk)
            progress_notifier.set_progress(int((index + 1) * 100 / len(chunks)))
            has_previous_chunk_failed = False
        except SerialExceptionBase as e:
            if has_previous_chunk_failed:
                raise e
            has_previous_chunk_failed = True
            continue


async def reboot_to_bootloader(
    serial_device: SerialDevice, bootloader_can_preserve_port_settings: bool = False
) -> None:
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
    serial_device: SerialDevice,
    update_state_notifier: UpdateStateNotifier,
    software: SoftwareComponent,
    binary_downloader: BinaryDownloader,
    bootloader_can_preserve_port_settings: bool = False,
) -> bool:
    device_model = ""
    sn = 0
    try:
        update_state_notifier.set_progress(0)
        device_model = await read_device_model(serial_device)
        sn = await read_sn(serial_device, device_model)

        await reboot_to_bootloader(serial_device, bootloader_can_preserve_port_settings)
        await flash_fw(
            serial_device,
            parse_wbfw(binary_downloader.download_file(software.available.endpoint)),
            update_state_notifier,
        )

        logger.info(
            "%s (sn: %d, %s) %s update from %s to %s completed",
            device_model,
            sn,
            serial_device.get_description(),
            software.type.value,
            software.current_version,
            software.available.version,
        )
        return True
    except Exception as e:
        logger.error(
            "%s (sn: %d, %s) %s update from %s to %s failed: %s",
            device_model,
            sn,
            serial_device.get_description(),
            software.type.value,
            software.current_version,
            software.available.version,
            e,
        )
        update_state_notifier.set_error(str(e))
        return False


async def restore_firmware(
    serial_device: SerialDevice,
    update_state_notifier: UpdateStateNotifier,
    firmware: ReleasedBinary,
    binary_downloader: BinaryDownloader,
) -> None:
    try:
        update_state_notifier.set_progress(0)
        await flash_fw(
            serial_device,
            parse_wbfw(binary_downloader.download_file(firmware.endpoint)),
            update_state_notifier,
        )
        update_state_notifier.delete()
        logger.info(
            "Firmware of device %s is restored to %s", serial_device.get_description(), firmware.version
        )
    except Exception as e:
        logger.error("Firmware restore of %s failed: %s", serial_device.get_description(), e)
        update_state_notifier.set_error(str(e))


async def read_device_model(serial_device: SerialDevice) -> str:
    try:
        return await serial_device.read(WB_DEVICE_PARAMETERS["device_model_extended"])
    except SerialExceptionBase as err:
        logger.debug("Can't read extended device model: %s", err)
        try:
            return await serial_device.read(WB_DEVICE_PARAMETERS["device_model"])
        except SerialExceptionBase as err2:
            logger.debug("Can't read device model: %s", err2)
    return ""


async def read_sn(serial_device: SerialDevice, device_model: str) -> int:
    try:
        sn = int.from_bytes(await serial_device.read(WB_DEVICE_PARAMETERS["sn"]), byteorder="big")
        return fix_sn(device_model, sn)
    except SerialExceptionBase as err:
        logger.debug("Can't read SN: %s", err)
    return 0


class FirmwareUpdater:
    STATE_PUBLISH_TOPIC = "/wb-device-manager/firmware_update/state"

    def __init__(
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

    async def _check_updatable(
        self, slave_id: int, fw_info: FirmwareInfo, port_config: Union[SerialConfig, TcpConfig]
    ) -> bool:
        if fw_info.bootloader.can_preserve_port_settings or isinstance(port_config, SerialConfig):
            return True

        if isinstance(port_config, TcpConfig):
            baud_rate = await self._serial_rpc.read(port_config, slave_id, WB_DEVICE_PARAMETERS["baud_rate"])
            parity = await self._serial_rpc.read(port_config, slave_id, WB_DEVICE_PARAMETERS["parity"])
            if baud_rate != 96 or parity != 0:
                return False
        return True

    async def get_firmware_info(self, **kwargs) -> dict:
        logger.debug("Request firmware info")
        port_config = read_port_config(kwargs.get("port", {}))
        slave_id = kwargs.get("slave_id")
        software_type = SoftwareType(kwargs.get("type", SoftwareType.FIRMWARE.value))
        if self._state.is_updating(slave_id, Port(port_config), software_type):
            raise MQTTRPCAlreadyProcessingException()
        try:
            fw_info = await self._fw_info_reader.read(port_config, slave_id)
            can_update = await self._check_updatable(slave_id, fw_info, port_config)
        except SerialExceptionBase as err:
            logger.debug("Can't get firmware info for %s (%s): %s", slave_id, port_config, err)
            raise JSONRPCDispatchException(
                code=MQTTRPCErrorCode.REQUEST_HANDLING_ERROR.value, message=str(err)
            ) from err
        return {
            "fw": fw_info.current_version,
            "available_fw": fw_info.available.version if fw_info.available is not None else "",
            "can_update": can_update,
            "bootloader": fw_info.bootloader.current_version,
            "available_bootloader": (
                fw_info.bootloader.available.version if fw_info.bootloader.available is not None else ""
            ),
        }

    async def update_software(self, **kwargs):
        if self._update_software_task and not self._update_software_task.done():
            raise MQTTRPCAlreadyProcessingException()
        software_type = SoftwareType(kwargs.get("type", SoftwareType.FIRMWARE.value))
        logger.debug("Start %s update", software_type.value)
        slave_id = kwargs.get("slave_id")
        port_config = read_port_config(kwargs.get("port", {}))
        fw_info = await self._fw_info_reader.read(port_config, slave_id)
        if not await self._check_updatable(slave_id, fw_info, port_config):
            raise ValueError("Can't update firmware over TCP")
        if software_type == SoftwareType.BOOTLOADER:
            self._update_software_task = self._asyncio_loop.create_task(
                self._update_bootloader(slave_id, port_config, fw_info),
                name="Update bootloader (long running)",
            )
        else:
            self._update_software_task = self._asyncio_loop.create_task(
                self._update_firmware(slave_id, port_config, fw_info),
                name="Update firmware (long running)",
            )
        return "Ok"

    async def clear_error(self, **kwargs):
        slave_id = kwargs.get("slave_id")
        port = Port(kwargs.get("port", {}).get("path"))
        software_type = SoftwareType(kwargs.get("type", SoftwareType.FIRMWARE.value))
        logger.debug("Clear error: %d %s", slave_id, port.path)
        self._state.clear_error(slave_id, port, software_type)
        return "Ok"

    async def restore_firmware(self, **kwargs):
        if self._update_software_task and not self._update_software_task.done():
            raise MQTTRPCAlreadyProcessingException()
        logger.debug("Start firmware restore")
        slave_id = kwargs.get("slave_id")
        port_config = read_port_config(kwargs.get("port", {}))
        if not await is_in_bootloader_mode(slave_id, self._serial_rpc, port_config):
            return "Ok"
        fw_info = await self._fw_info_reader.read(port_config, slave_id, True)
        self._update_software_task = self._asyncio_loop.create_task(
            self._restore_firmware(slave_id, port_config, fw_info),
            name="Restore firmware (long running)",
        )
        return "Ok"

    async def _update_firmware(
        self,
        slave_id: int,
        port_config: Union[SerialConfig, TcpConfig],
        fw_info: FirmwareInfo,
    ) -> bool:
        serial_device = SerialDevice(self._serial_rpc, port_config, slave_id)
        update_notifier = UpdateStateNotifier(
            DeviceUpdateInfo(
                port=Port(port_config),
                slave_id=slave_id,
                from_version=fw_info.current_version,
                to_version=fw_info.available.version,
            ),
            self._state,
        )
        if await update_software(
            serial_device,
            update_notifier,
            fw_info.available,
            self._binary_downloader,
            fw_info.bootloader.can_preserve_port_settings,
        ):
            update_notifier.delete()

    async def _update_bootloader(
        self,
        slave_id: int,
        port_config: Union[SerialConfig, TcpConfig],
        fw_info: FirmwareInfo,
    ) -> None:
        serial_device = SerialDevice(self._serial_rpc, port_config, slave_id)
        bootloader_update_notifier = UpdateStateNotifier(
            DeviceUpdateInfo(
                port=Port(port_config),
                slave_id=slave_id,
                type=SoftwareType.BOOTLOADER,
                from_version=fw_info.bootloader.current_version,
                to_version=fw_info.bootloader.available.version,
            ),
            self._state,
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
                DeviceUpdateInfo(
                    port=Port(port_config), slave_id=slave_id, to_version=fw_info.available.version
                ),
                self._state,
            )
            await asyncio.sleep(1)
            await restore_firmware(
                serial_device, fw_update_notifier, fw_info.available, self._binary_downloader
            )

    async def _restore_firmware(
        self,
        slave_id: int,
        port_config: Union[SerialConfig, TcpConfig],
        fw_info: FirmwareInfo,
    ) -> None:
        serial_device = SerialDevice(self._serial_rpc, port_config, slave_id)
        update_notifier = UpdateStateNotifier(
            DeviceUpdateInfo(
                port=Port(port_config),
                slave_id=slave_id,
                to_version=fw_info.available.version,
            ),
            self._state,
        )
        await restore_firmware(serial_device, update_notifier, fw_info.available, self._binary_downloader)

    @property
    def state_publish_topic(self) -> str:
        return self.STATE_PUBLISH_TOPIC

    def start(self) -> None:
        self._state.reset()
