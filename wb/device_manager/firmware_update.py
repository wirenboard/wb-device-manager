#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Union

import semantic_version

from . import logger
from .fw_downloader import ReleasedFirmware, download_remote_file, get_released_fw
from .mqtt_rpc import MQTTRPCAlreadyProcessingException
from .releases import parse_releases
from .serial_rpc import (
    WB_DEVICE_PARAMETERS,
    ModbusExceptionCode,
    SerialConfig,
    SerialRPCWrapper,
    TcpConfig,
    WBModbusException,
)


@dataclass
class StateError:
    message: str = None


@dataclass
class Port:
    path: str = None

    def __init__(self, port_config: Union[SerialConfig, TcpConfig, str]):
        if isinstance(port_config, SerialConfig):
            self.path = port_config.path
        elif isinstance(port_config, TcpConfig):
            self.path = f"{port_config.address}:{port_config.port}"
        elif isinstance(port_config, str):
            self.path = port_config


@dataclass
class DeviceUpdateInfo:
    port: Port
    slave_id: int
    progress: int = 0
    from_fw: str = None
    to_fw: str = None
    error: StateError = field(default_factory=StateError)

    def __eq__(self, o):
        return self.slave_id == o.slave_id and self.port == o.port


@dataclass
class FirmwareUpdateState:
    devices: list[DeviceUpdateInfo] = field(default_factory=list)

    def update(self, device_info: DeviceUpdateInfo) -> None:
        for i, d in enumerate(self.devices):
            if d == device_info:
                self.devices[i] = device_info
                return
        self.devices.append(device_info)

    def remove(self, device_info: DeviceUpdateInfo) -> None:
        for i, d in enumerate(self.devices):
            if d == device_info:
                self.devices.pop(i)
                return True
        return False

    def is_updating(self, slave_id: int, port: Port) -> bool:
        for d in self.devices:
            if d.slave_id == slave_id and d.port == port and d.progress < 100 and d.error.message is None:
                return True
        return False

    def clear_error(self, slave_id: int, port: Port) -> bool:
        for d in self.devices:
            if d.slave_id == slave_id and d.port == port and d.error.message is not None:
                self.devices.remove(d)
                return True
        return False


def to_dict_for_json(device_update_info: DeviceUpdateInfo) -> dict:
    d = asdict(device_update_info)
    if device_update_info.error.message is None:
        del d["error"]
    return d


def to_json_string(firmware_update_state: FirmwareUpdateState) -> str:
    dict_for_json = {
        "devices": [to_dict_for_json(d) for d in firmware_update_state.devices],
    }
    return json.dumps(dict_for_json, indent=None, separators=(",", ":"))


@dataclass
class FirmwareInfo:
    current: str = None
    available: ReleasedFirmware = None
    signature: str = None
    bootloader_can_preserve_port_settings: bool = False

    def has_update(self) -> bool:
        return self.available is not None and semantic_version.Version(
            self.available.version
        ) > semantic_version.Version(self.current)


@dataclass
class ParsedWBFW:
    info: bytes
    data: bytes


def read_wbfw(fw_fpath: str) -> ParsedWBFW:
    INFO_BLOCK_LENGTH = WB_DEVICE_PARAMETERS["fw_info_block"].register_count * 2

    bs = int(os.path.getsize(fw_fpath))
    if bs % 2:
        raise ValueError(f"Fw file should be even-bytes long! Got {fw_fpath} ({bs}b)")

    with open(fw_fpath, "rb") as fp:
        raw_bytes = fp.read()

    res = ParsedWBFW(raw_bytes[:INFO_BLOCK_LENGTH], raw_bytes[INFO_BLOCK_LENGTH:])
    if len(res.info) != INFO_BLOCK_LENGTH:
        raise ValueError(
            f"Info block size should be {INFO_BLOCK_LENGTH} bytes! Got {len(res.info)} instead\nRaw: {res.info}"
        )

    return res


def read_port_config(port: dict) -> Union[SerialConfig, TcpConfig]:
    return TcpConfig(**port) if "address" in port else SerialConfig(**port)


class UpdateNotifier:
    step: int = 0
    notification_step: int = 1

    def __init__(self, notifications_count: int):
        self.notification_step = 100 / notifications_count

    def should_notify(self, progress_percent: int) -> bool:
        current_step = int(progress_percent / self.notification_step)
        if current_step > self.step:
            self.step = current_step
            return True
        return False


class FirmwareUpdater:
    STATE_PUBLISH_TOPIC = "/wb-device-manager/firmware_update/state"

    def __init__(self, mqtt_connection, rpc_client, asyncio_loop):
        self._mqtt_connection = mqtt_connection
        self._rpc_client = rpc_client
        self._serial_rpc = SerialRPCWrapper(rpc_client)
        self._asyncio_loop = asyncio_loop
        self._state = FirmwareUpdateState()
        self._update_firmware_task = None

    async def _read_firmware_info(
        self, port_config: Union[SerialConfig, TcpConfig], slave_id: int
    ) -> FirmwareInfo:
        try:
            fw_signature = await self._serial_rpc.read(
                port_config, slave_id, WB_DEVICE_PARAMETERS["fw_signature"]
            )
            logger.debug("Get firmware info for: %s", fw_signature)
            released_fw = get_released_fw(fw_signature, parse_releases("/usr/lib/wb-release").get("SUITE"))
        except WBModbusException as err:
            if err.code in [
                ModbusExceptionCode.ILLEGAL_FUNCTION.value,
                ModbusExceptionCode.ILLEGAL_DATA_ADDRESS.value,
                ModbusExceptionCode.ILLEGAL_DATA_VALUE.value,
            ]:
                logger.debug("Can't get firmware signature, maybe the device is too old")
            raise err

        fw = await self._serial_rpc.read(port_config, slave_id, WB_DEVICE_PARAMETERS["fw_version"])
        try:
            bootloader_can_preserve_port_settings = (
                await self._serial_rpc.read(
                    port_config, slave_id, WB_DEVICE_PARAMETERS["reboot_to_bootloader_preserve_port_settings"]
                )
                == 0
            )
        except Exception:
            bootloader_can_preserve_port_settings = False
        return FirmwareInfo(fw, released_fw, fw_signature, bootloader_can_preserve_port_settings)

    async def _check_updatable(
        self, slave_id: int, fw_info: FirmwareInfo, port_config: Union[SerialConfig, TcpConfig]
    ) -> bool:
        if fw_info.bootloader_can_preserve_port_settings or isinstance(port_config, SerialConfig):
            return True

        if isinstance(port_config, TcpConfig):
            baud_rate = await self._serial_rpc.read(port_config, slave_id, WB_DEVICE_PARAMETERS["baud_rate"])
            parity = await self._serial_rpc.read(port_config, slave_id, WB_DEVICE_PARAMETERS["parity"])
            stop_bits = await self._serial_rpc.read(port_config, slave_id, WB_DEVICE_PARAMETERS["stop_bits"])
            logger.debug("Check updatable: %d %d %d", baud_rate, parity, stop_bits)
            if (
                await self._serial_rpc.read(port_config, slave_id, WB_DEVICE_PARAMETERS["baud_rate"]) != 96
                or await self._serial_rpc.read(port_config, slave_id, WB_DEVICE_PARAMETERS["parity"]) != 0
                # or await self._serial_rpc.read(port_config, slave_id, WB_DEVICE_PARAMETERS["stop_bits"]) != 2
            ):
                return False
        return True

    async def get_firmware_info(self, **kwargs) -> dict:
        logger.debug("Request firmware info")
        port_config = read_port_config(kwargs.get("port", {}))
        slave_id = kwargs.get("slave_id")
        if self._state.is_updating(slave_id, Port(port_config)):
            raise MQTTRPCAlreadyProcessingException()
        fw_info = await self._read_firmware_info(port_config, slave_id)
        return {
            "fw": fw_info.current,
            "available_fw": fw_info.available.version if fw_info.available is not None else "",
            "can_update": await self._check_updatable(slave_id, fw_info, port_config),
        }

    async def update_firmware(self, **kwargs):
        if self._update_firmware_task and not self._update_firmware_task.done():
            raise MQTTRPCAlreadyProcessingException()
        logger.debug("Start firmware update")
        slave_id = kwargs.get("slave_id")
        port_config = read_port_config(kwargs.get("port", {}))
        fw_info = await self._read_firmware_info(port_config, slave_id)
        if not await self._check_updatable(slave_id, fw_info, port_config):
            raise ValueError("Can't update firmware over TCP")
        self._update_firmware_task = self._asyncio_loop.create_task(
            self._do_firmware_update(slave_id, port_config, fw_info),
            name="Update firmware (long running)",
        )
        return "Ok"

    async def clear_error(self, **kwargs):
        slave_id = kwargs.get("slave_id")
        port = Port(kwargs.get("port", {}).get("path"))
        logger.debug("Clear error: %d %s", slave_id, port.path)
        if self._state.clear_error(slave_id, port):
            self._mqtt_connection.publish(self.state_publish_topic, to_json_string(self._state), retain=True)
        return "Ok"

    async def _read_device_model(self, port_config: Union[SerialConfig, TcpConfig], slave_id: int) -> str:
        try:
            return await self._serial_rpc.read(
                port_config, slave_id, WB_DEVICE_PARAMETERS["device_model_extended"]
            )
        except Exception as err:
            logger.debug("Can't read extended device model: %s", err)
            try:
                return await self._serial_rpc.read(
                    port_config, slave_id, WB_DEVICE_PARAMETERS["device_model_extended"]
                )
            except Exception as err2:
                logger.debug("Can't read device model: %s", err2)
        return ""

    async def _read_sn(
        self, port_config: Union[SerialConfig, TcpConfig], slave_id: int, device_model: str
    ) -> int:
        try:
            sn = int.from_bytes(
                await self._serial_rpc.read(port_config, slave_id, WB_DEVICE_PARAMETERS["sn"]),
                byteorder="big",
            )
            # WB-MAP* uses 25 bit for serial number
            wbmap_re = re.compile(r"\S*MAP\d+\S*")
            if wbmap_re.match(device_model):
                return sn & 0x1FFFFFF
            return sn
        except Exception as err:
            logger.debug("Can't read SN: %s", err)
        return 0

    async def _do_firmware_update(
        self,
        slave_id: int,
        port_config: Union[SerialConfig, TcpConfig],
        fw_info: FirmwareInfo,
    ) -> None:
        update_info = DeviceUpdateInfo(
            port=Port(port_config),
            slave_id=slave_id,
            from_fw=fw_info.current,
            to_fw=fw_info.available.version,
        )
        self._update_state(update_info)

        if not fw_info.has_update():
            update_info.progress = 100
            self._update_state(update_info)
            return

        device_model = await self._read_device_model(port_config, slave_id)
        sn = await self._read_sn(port_config, slave_id, device_model)
        try:
            downloaded_wbfw_path = download_remote_file(fw_info.available.endpoint, "/tmp")
            await self._do_flash(
                slave_id,
                port_config,
                downloaded_wbfw_path,
                update_info,
                fw_info.bootloader_can_preserve_port_settings,
            )
            logger.info(
                "Firmware of %s (sn: %d, slave_id: %d, %s) updated from %s to %s",
                device_model,
                sn,
                slave_id,
                port_config,
                fw_info.current,
                fw_info.available.version,
            )
        except Exception as e:
            update_info.error.message = str(e)
            self._update_state(update_info)
            logger.error(
                "Updating firmware of %s (sn: %d, slave_id: %d, %s) from %s to %s failed: %s",
                device_model,
                sn,
                slave_id,
                port_config,
                fw_info.current,
                fw_info.available.version,
                e,
            )

    async def _do_flash(
        self,
        slave_id: int,
        port_config: Union[SerialConfig, TcpConfig],
        downloaded_wbfw_path: str,
        update_info: DeviceUpdateInfo,
        bootloader_can_preserve_port_settings: bool = False,
    ) -> None:
        if bootloader_can_preserve_port_settings:
            await self._serial_rpc.write(
                port_config,
                slave_id,
                WB_DEVICE_PARAMETERS["reboot_to_bootloader_preserve_port_settings"],
                1,
            )
        else:
            try:
                await self._serial_rpc.write(
                    port_config, slave_id, WB_DEVICE_PARAMETERS["reboot_to_bootloader"], 1
                )
            except Exception:
                # Device has rebooted and doesn't send response (Fixed in latest FWs)
                logger.debug("Device doesn't send response to reboot command, probably it has rebooted")
            if isinstance(port_config, SerialConfig):
                port_config.set_default_settings()

        # Delay before going to bootloader
        await asyncio.sleep(0.5)
        await self._flash_fw(slave_id, port_config, read_wbfw(downloaded_wbfw_path), update_info)

    @property
    def state_publish_topic(self) -> str:
        return self.STATE_PUBLISH_TOPIC

    def _update_state(self, device_info: DeviceUpdateInfo) -> None:
        self._state.update(device_info)
        self._mqtt_connection.publish(self.state_publish_topic, to_json_string(self._state), retain=True)
        if device_info.progress == 100:
            self._state.remove(device_info)
            self._mqtt_connection.publish(self.state_publish_topic, to_json_string(self._state), retain=True)

    async def _flash_fw(
        self,
        slave_id: int,
        port_config: Union[SerialConfig, TcpConfig],
        parsed_wbfw: ParsedWBFW,
        update_info: DeviceUpdateInfo,
    ) -> None:
        await self._serial_rpc.write(
            port_config,
            slave_id,
            WB_DEVICE_PARAMETERS["fw_info_block"],
            parsed_wbfw.info,
            1.0,  # Bl needs some time to perform info-block magic,
        )

        DATA_CHUNK_LENGTH = WB_DEVICE_PARAMETERS["fw_data_block"].register_count * 2
        chunks = [
            parsed_wbfw.data[i : i + DATA_CHUNK_LENGTH]
            for i in range(0, len(parsed_wbfw.data), DATA_CHUNK_LENGTH)
        ]

        # Due to bootloader's behavior, actual flashing failure is current-chunk failure + next-chunk failure
        has_previous_chunk_failed = False

        update_notifier = UpdateNotifier(30)

        for index, chunk in enumerate(chunks):
            try:
                await self._serial_rpc.write(
                    port_config, slave_id, WB_DEVICE_PARAMETERS["fw_data_block"], chunk
                )
                update_info.progress = int(index * 100 / len(chunks))
                if update_notifier.should_notify(update_info.progress):
                    self._update_state(update_info)
                has_previous_chunk_failed = False
            except Exception as e:
                if has_previous_chunk_failed:
                    raise e
                has_previous_chunk_failed = True
                continue
        update_info.progress = 100
        self._update_state(update_info)
