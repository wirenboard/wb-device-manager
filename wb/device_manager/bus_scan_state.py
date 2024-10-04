#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Optional, Union

from .serial_rpc import SerialConfig, TcpConfig

BAUDRATES_TO_SCAN = [9600, 115200, 57600, 1200, 2400, 4800, 19200, 38400]
MAX_MODBUS_SLAVE_ID_TO_SCAN = 246


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
class StateError:
    id: str = None
    message: str = None
    metadata: dict = None


@dataclass
class SerialParams:
    slave_id: int
    baud_rate: int = 9600
    parity: str = "N"
    data_bits: int = 8
    stop_bits: int = 2


@dataclass
class Firmware:
    version: str = None
    ext_support: bool = False


@dataclass
class DeviceInfo:
    uuid: str
    port: Port
    title: str = None
    sn: str = None
    device_signature: str = None
    fw_signature: str = None
    last_seen: int = None
    bootloader_mode: bool = False
    errors: list[StateError] = field(default_factory=list)
    cfg: SerialParams = field(default_factory=SerialParams)
    fw: Firmware = field(default_factory=Firmware)

    def __hash__(self):
        return hash(self.uuid) ^ hash(self.port.path)

    def __eq__(self, o):
        return self.__hash__() == o.__hash__()


@dataclass
class BusScanState:
    progress: int = 0
    scanning: bool = False
    scanning_ports: list[str] = field(default_factory=list)
    is_ext_scan: bool = False
    error: StateError = None
    devices: list[DeviceInfo] = field(default_factory=list)

    def update(self, new):
        for k, v in new.items():
            if hasattr(self, k):
                setattr(self, k, v)


class SetEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, set):
            return list(o)
        if is_dataclass(o):
            return asdict(o)
        super().default(o)


@dataclass
class ParsedPorts:
    serial: list[str] = field(default_factory=list)
    tcp: list[str] = field(default_factory=list)


"""
Errors, shown in json-state
"""


class GenericStateError(StateError):
    ID = "com.wb.device_manager.generic_error"
    MESSAGE = "Internal error. Check logs for more info"

    def __init__(self):
        super().__init__(id=self.ID, message=self.MESSAGE)


class RPCCallTimeoutStateError(GenericStateError):
    ID = "com.wb.device_manager.rpc_call_timeout_error"
    MESSAGE = "RPC call to wb-mqtt-serial timed out. Check, wb-mqtt-serial is running"


class FailedScanStateError(GenericStateError):
    ID = "com.wb.device_manager.failed_to_scan_error"
    MESSAGE = "Some ports failed to scan. Check logs for more info"

    def __init__(self, failed_ports):
        super().__init__()
        self.metadata = {"failed_ports": failed_ports}


class ReadFWVersionDeviceError(GenericStateError):
    ID = "com.wb.device_manager.device.read_fw_version_error"
    MESSAGE = "Failed to read FW version."


class ReadFWSignatureDeviceError(GenericStateError):
    ID = "com.wb.device_manager.device.read_fw_signature_error"
    MESSAGE = "Failed to read FW signature."


class ReadDeviceSignatureDeviceError(GenericStateError):
    ID = "com.wb.device_manager.device.read_device_signature_error"
    MESSAGE = "Failed to read device signature."


class ReadSerialParamsDeviceError(GenericStateError):
    ID = "com.wb.device_manager.device.read_serial_params_error"
    MESSAGE = "Failed to read serial params from device."


def get_all_uart_params(bds=BAUDRATES_TO_SCAN, parities=["N", "E", "O"], stopbits=[2, 1]):
    """There are the following assumptions:
    1. Most frequently used baudrates are 9600, 115200, 57600
    2. Most frequently used parity is "N"
    So, yield them first, then yield less frequently used baudrates and parities
    """
    len_iterable = len(bds) * len(parities) * len(stopbits)
    pos = 0
    most_frequent_bds, less_frequent_bds = bds[:3], bds[3:]
    for parity in parities:
        for stopbit in stopbits:
            for bd in most_frequent_bds:
                pos += 1
                yield bd, parity, stopbit, int(pos / len_iterable * 100)
        for stopbit in stopbits:
            for bd in less_frequent_bds:
                pos += 1
                yield bd, parity, stopbit, int(pos / len_iterable * 100)


def make_uuid(sn):
    return str(uuid.uuid3(namespace=uuid.NAMESPACE_OID, name=str(sn)))


class BusScanStateManager:
    STATE_PUBLISH_TOPIC = "/wb-device-manager/state"

    def __init__(self, mqtt_connection, asyncio_loop) -> None:
        self._ports_now_scanning = set()
        self._ports_errored = set()
        self._found_devices = []
        self._mqtt_connection = mqtt_connection
        self._asyncio_loop = asyncio_loop
        self._state_update_queue = asyncio.Queue()
        self._asyncio_loop.create_task(
            self._consume_state_update(), name="Build & publish overall bus scan state"
        )

    @property
    def state_publish_topic(self):
        return self.STATE_PUBLISH_TOPIC

    async def add_scanning_port(self, port: str, is_ext_scan: bool) -> None:
        self._ports_now_scanning.add(port)
        await self._produce_state_update(
            {"scanning_ports": self._ports_now_scanning, "is_ext_scan": is_ext_scan}
        )

    async def remove_scanning_port(self, port: str, progress: Optional[int] = None) -> None:
        self._ports_now_scanning.discard(port)
        update = {"scanning_ports": self._ports_now_scanning}
        if progress is not None:
            update["progress"] = progress
        await self._produce_state_update(update)

    async def add_error_port(self, port: str) -> None:
        self._ports_errored.add(port)
        await self._produce_state_update({"error": FailedScanStateError(failed_ports=self._ports_errored)})

    def is_device_found(self, sn: str) -> bool:
        return sn in self._found_devices

    async def found_device(self, sn: str, device_info: DeviceInfo) -> None:
        self._found_devices.append(sn)
        await self._produce_state_update(device_info)

    async def scan_complete(self) -> None:
        await self._produce_state_update({"scanning": True, "progress": 100, "scanning_ports": []})
        await self._produce_state_update({"scanning": False, "progress": 0})

    async def scan_finished(self, error=None) -> None:
        await self._produce_state_update(
            {"scanning": False, "progress": 0, "scanning_ports": [], "error": error}
        )

    async def reset(self, preserve_old_results: bool) -> None:
        new_state = {
            "scanning": True,
            "progress": 0,
        }
        if not preserve_old_results:
            self._found_devices = []
            new_state["error"] = None
            new_state["devices"] = []
        await self._produce_state_update(new_state)
        self._ports_now_scanning = set()
        self._ports_errored = set()

    async def _produce_state_update(self, event: Union[dict, DeviceInfo]) -> None:
        await self._state_update_queue.put(event)

    async def _consume_state_update(self):
        """
        The only func, allowed to change state directly
        """
        state = BusScanState()
        self._mqtt_connection.publish(self.STATE_PUBLISH_TOPIC, self.state_json(state), retain=True)

        while True:
            event = await self._state_update_queue.get()
            try:
                if isinstance(event, DeviceInfo):
                    state.devices.append(event)
                elif isinstance(event, dict):
                    progress = event.pop("progress", -1)  # could be filled asynchronously
                    if (progress == 0) or (progress > state.progress):
                        state.progress = progress
                    state.update(event)
                else:
                    e = RuntimeError("Got incorrect state-update event: %s", repr(event))
                    state.error = GenericStateError()
                    state.scanning = False
                    state.progress = 0
                    raise e
            finally:
                self._mqtt_connection.publish(self.STATE_PUBLISH_TOPIC, self.state_json(state), retain=True)

    @staticmethod
    def state_json(state_obj):
        return json.dumps(
            asdict(state_obj), indent=None, separators=(",", ":"), cls=SetEncoder
        )  # most compact
