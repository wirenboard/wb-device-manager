#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import os
import time
import uuid
from argparse import ArgumentParser
from collections import defaultdict
from dataclasses import asdict, dataclass, field, is_dataclass
from itertools import chain, product
from sys import argv, stderr, stdout
from urllib.parse import urlparse

import paho_socket
from mqttrpc import Dispatcher
from paho.mqtt import client as mqttclient
from wb_modbus import ALLOWED_BAUDRATES, ALLOWED_PARITIES, ALLOWED_STOPBITS, bindings
from wb_modbus import logger as mb_logger
from wb_modbus import minimalmodbus

from . import logger, mqtt_rpc, serial_bus

EXIT_INVALIDARGUMENT = 2
EXIT_FAILURE = 1


@dataclass
class StateError:
    id: str = None
    message: str = None
    metadata: dict = None


@dataclass
class Port:
    path: str = None


@dataclass
class SerialParams:
    slave_id: int
    baud_rate: int = 9600
    parity: str = "N"
    data_bits: int = 8
    stop_bits: int = 2


@dataclass
class FWUpdate:
    progress: int = 0
    error: StateError = None
    available_fw: str = None


@dataclass
class Firmware:
    version: str = None
    ext_support: bool = False
    update: FWUpdate = field(default_factory=FWUpdate)


@dataclass
class DeviceInfo:
    uuid: str
    port: Port
    title: str = None
    sn: str = None
    device_signature: str = None
    fw_signature: str = None
    online: bool = False
    poll: bool = False
    last_seen: int = None
    bootloader_mode: bool = False
    error: StateError = None
    slave_id_collision: bool = False
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
    scanning_port: str = None
    ports_to_scan: list[str] = field(default_factory=list)
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


class PortScanningError(Exception):
    def __init__(self, *args, **kwargs):
        self.port = kwargs.pop("port")
        super().__init__(*args, **kwargs)


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


class DeviceManager:
    MQTT_CLIENT_NAME = "wb-device-manager"
    STATE_PUBLISH_TOPIC = "/wb-device-manager/state"

    def __init__(self, broker_url: str):
        url = urlparse(broker_url)
        client_id = "%s-%d" % (self.MQTT_CLIENT_NAME, os.getpid())
        if url.scheme == "mqtt-tcp":
            self._mqtt_connection = mqttclient.Client(client_id)
        elif url.scheme == "unix":
            self._mqtt_connection = paho_socket.Client(client_id)
        else:
            raise Exception("Unkown mqtt url scheme")

        self._rpc_client = mqtt_rpc.SRPCClient(self.mqtt_connection)
        self._state_update_queue = asyncio.Queue()
        self._asyncio_loop = asyncio.get_event_loop()
        self.asyncio_loop.create_task(self.consume_state_update(), name="Build & publish overall state")
        self._is_scanning = False
        self._found_devices = []

    @property
    def mqtt_connection(self):
        return self._mqtt_connection

    @property
    def rpc_client(self):
        return self._rpc_client

    @property
    def state_update_queue(self):
        return self._state_update_queue

    @property
    def state_publish_topic(self):
        return self.STATE_PUBLISH_TOPIC

    @property
    def asyncio_loop(self):
        return self._asyncio_loop

    @staticmethod
    def state_json(state_obj):
        return json.dumps(
            asdict(state_obj), indent=None, separators=(",", ":"), cls=SetEncoder
        )  # most compact

    async def produce_state_update(self, event={}):  # TODO: an observer pattern; on_change callbacks
        await self.state_update_queue.put(event)

    async def consume_state_update(self):
        """
        The only func, allowed to change state directly
        """
        state = BusScanState()
        self.mqtt_connection.publish(self.STATE_PUBLISH_TOPIC, self.state_json(state), retain=True)

        devices_by_connection_params = defaultdict(list)

        while True:
            event = await self.state_update_queue.get()
            try:
                if isinstance(event, DeviceInfo):
                    state.devices.append(event)
                    # wb-mqtt-serial treats devices with equal slaveid-port as same (even with different serial_params)
                    neighbors = devices_by_connection_params[str((event.cfg.slave_id, event.port))]
                    neighbors.append(event)
                    if len(neighbors) > 1:
                        for entry in neighbors:
                            entry.slave_id_collision = True
                        logger.debug("Collision on: %s", str(neighbors))
                elif isinstance(event, dict):
                    progress = event.pop("progress", -1)  # could be filled asynchronously
                    if (progress == 0) or (progress > state.progress):
                        state.progress = progress
                    state.update(event)
                    if "devices" in event and not event["devices"]:
                        devices_by_connection_params.clear()
                else:
                    e = RuntimeError("Got incorrect state-update event: %s", repr(event))
                    state.error = GenericStateError()
                    state.scanning = False
                    state.progress = 0
                    raise e
            finally:
                self.mqtt_connection.publish(self.STATE_PUBLISH_TOPIC, self.state_json(state), retain=True)

    def _get_mb_connection(self, device_info, is_ext_scan):
        if is_ext_scan:
            conn = serial_bus.WBAsyncExtendedModbus(
                sn=int(device_info.sn),
                port=device_info.port.path,
                baudrate=device_info.cfg.baud_rate,
                parity=device_info.cfg.parity,
                stopbits=device_info.cfg.stop_bits,
                rpc_client=self.rpc_client,
            )
        else:
            conn = serial_bus.WBAsyncModbus(
                addr=device_info.cfg.slave_id,
                port=device_info.port.path,
                baudrate=device_info.cfg.baud_rate,
                parity=device_info.cfg.parity,
                stopbits=device_info.cfg.stop_bits,
                rpc_client=self.rpc_client,
            )
        return conn

    async def _fill_device_signature(self, device_info, is_ext_scan) -> None:
        mb_conn = self._get_mb_connection(device_info, is_ext_scan)
        reg = bindings.WBModbusDeviceBase.COMMON_REGS_MAP["device_signature"]
        number_of_regs = 20
        device_signature = ""
        try:
            device_signature = await mb_conn.read_string(first_addr=reg, regs_length=number_of_regs)
        except minimalmodbus.ModbusException:
            number_of_regs = bindings.WBModbusDeviceBase.DEVICE_SIGNATURE_LENGTH
            device_signature = await mb_conn.read_string(first_addr=reg, regs_length=number_of_regs)
        finally:
            device_info.device_signature = device_signature.strip("\x02")  # WB-MAP* fws failure
            device_info.title = device_signature.strip("\x02")  # TODO: store somewhere human-readable titles

    async def _fill_fw_signature(self, device_info, is_ext_scan) -> None:
        mb_conn = self._get_mb_connection(device_info, is_ext_scan)
        reg = bindings.WBModbusDeviceBase.COMMON_REGS_MAP["fw_signature"]
        number_of_regs = bindings.WBModbusDeviceBase.FIRMWARE_SIGNATURE_LENGTH
        device_info.fw_signature = await mb_conn.read_string(first_addr=reg, regs_length=number_of_regs)

    async def _fill_fw_info(self, device_info, is_ext_scan) -> None:
        mb_conn = self._get_mb_connection(device_info, is_ext_scan)
        reg = bindings.WBModbusDeviceBase.COMMON_REGS_MAP["fw_version"]
        number_of_regs = bindings.WBModbusDeviceBase.FIRMWARE_VERSION_LENGTH
        device_info.fw.version = await mb_conn.read_string(first_addr=reg, regs_length=number_of_regs)
        # TODO: fill available version from fw-releases

    def _get_all_uart_params(
        self,
        bds=ALLOWED_BAUDRATES,
        parities=ALLOWED_PARITIES.keys(),
        stopbits=[
            1,
        ]
    ):
        iterable = product(bds, parities, stopbits)
        len_iterable = len(bds) * len(parities) * len(stopbits)
        for pos, (bd, parity, stopbit) in enumerate(iterable, start=1):
            yield bd, parity, stopbit, int(pos / len_iterable * 100)

    async def _get_ports(self) -> list[str]:
        response = await self.rpc_client.make_rpc_call(
            driver="wb-mqtt-serial",
            service="ports",
            method="Load",
            params={},
            timeout=1.0,  # s; rpc call goes around scheduler queue => relatively small
        )
        ports = [port.get("path") for port in response]
        return list(filter(None, ports))

    async def launch_bus_scan(self):
        if self._is_scanning:  # TODO: store mqtt topics and binded launched tasks (instead of launcher-cb)
            raise mqtt_rpc.MQTTRPCAlreadyProcessingException()
        else:
            logger.info("Start bus scanning")
            self.asyncio_loop.create_task(self.scan_serial_bus(), name="Scan serial bus (long running)")
            return "Ok"

    async def stop_bus_scan(self):
        """
        TODO: check, tasks are actually cancelled before returning "Ok" via rpc
        Check https://docs.python.org/dev/library/asyncio-task.html#asyncio.Task.cancel for more info
        """
        if self._is_scanning:
            logger.info("Stop bus scanning")
            for task in self._bus_scanning_tasks:
                if not task.done():
                    logger.debug("Cancelling task %s", task.get_name())
                    task.cancel()
            self._bus_scanning_tasks.clear()
            self._is_scanning = False
            return "Ok"
        else:
            raise mqtt_rpc.MQTTRPCAlreadyProcessingException()

    def _create_scan_tasks(self, ports, is_extended=False):
        tasks, debug_port_records = [], []
        name_tmpl = "Extended scan %s" if is_extended else "Ordinary scan %s"
        for port in ports:
            tasks.append(
                asyncio.create_task(
                    self.scan_serial_port(port, is_ext_scan=is_extended), name=name_tmpl % port
                )
            )
            debug_port_records.extend(
                [
                    "%s %d %d%s%d" % (port, bd, 8, parity, sb)
                    for bd, parity, sb, _ in self._get_all_uart_params()
                ]
            )
        return tasks, set(debug_port_records)

    async def scan_serial_bus(self):
        self._is_scanning = True
        self._found_devices = []
        self._bus_scanning_tasks = []
        self.ports_dbg_strs = set()

        await self.produce_state_update(
            {"devices": [], "scanning": True, "progress": 0, "error": None}  # TODO: unit test?
        )
        state_error = None
        try:
            ports = await self._get_ports()
        except mqtt_rpc.MQTTRPCInternalServerError:
            logger.exception("No answer from wb-mqtt-serial")
            state_error = RPCCallTimeoutStateError()
            ports = []

        results = []

        try:
            self._bus_scanning_tasks, self.ports_dbg_strs = self._create_scan_tasks(ports, is_extended=True)
            results.extend(await asyncio.gather(*self._bus_scanning_tasks, return_exceptions=True))
            await self.produce_state_update({"progress": 0})
            if self._is_scanning:  # multiple gather() could re-launch cancelled tasks
                self._bus_scanning_tasks, self.ports_dbg_strs = self._create_scan_tasks(
                    ports, is_extended=False
                )
                results.extend(await asyncio.gather(*self._bus_scanning_tasks, return_exceptions=True))

            if self._bus_scanning_tasks:
                await self.produce_state_update(
                    {"scanning": False, "progress": 100, "ports_to_scan": self.ports_dbg_strs}
                )
            failed_ports = [x.port for x in results if isinstance(x, PortScanningError)]
            if failed_ports:
                logger.warning("Unsuccessful scan: %s", str(failed_ports))
                state_error = FailedScanStateError(failed_ports=failed_ports)
        except Exception as e:
            state_error = GenericStateError()
            logger.exception("Unhandled exception during overall scan")
        finally:
            await self.produce_state_update({"scanning": False, "progress": 0, "error": state_error})
            self._is_scanning = False
            self._bus_scanning_tasks.clear()

    async def scan_serial_port(self, port, is_ext_scan=True):
        def make_uuid(sn):
            return str(uuid.uuid3(namespace=uuid.NAMESPACE_OID, name=str(sn)))

        if is_ext_scan:
            modbus_scanner = serial_bus.WBExtendedModbusScanner(port, self.rpc_client)
        else:
            modbus_scanner = serial_bus.WBModbusScanner(port, self.rpc_client)

        for bd, parity, stopbits, progress_percent in self._get_all_uart_params():
            debug_str = "%s %d %d%s%d" % (port, bd, 8, parity, stopbits)
            self.ports_dbg_strs.add(debug_str)
            logger.info("Scanning (via %s modbus) %s", "extended" if is_ext_scan else "ordinary", debug_str)
            await self.produce_state_update(
                {"scanning_port": debug_str, "ports_to_scan": self.ports_dbg_strs, "is_ext_scan": is_ext_scan}
            )
            try:
                async for slaveid, sn in modbus_scanner.scan_bus(
                    baudrate=bd, parity=parity, stopbits=stopbits
                ):
                    if sn in self._found_devices:
                        logger.info("Skipping device %s", str(sn))
                        continue

                    device_info = DeviceInfo(
                        uuid=make_uuid(sn),
                        title="Unknown",
                        sn=str(sn),
                        last_seen=int(time.time()),
                        online=True,
                        poll=True,  # TODO: support "is_polling" rpc call in wb-mqtt-serial
                        port=Port(path=port),
                        cfg=SerialParams(slave_id=slaveid, baud_rate=bd, parity=parity, stop_bits=stopbits),
                    )
                    device_info.fw.ext_support = is_ext_scan

                    try:
                        await self._fill_device_signature(device_info, is_ext_scan)
                        await self._fill_fw_signature(device_info, is_ext_scan)
                        await self._fill_fw_info(device_info, is_ext_scan)
                    except minimalmodbus.ModbusException as e:
                        logger.exception("Treating device %s as offline", str(device_info))
                        device_info.online = False
                    finally:
                        self._found_devices.append(sn)
                        await self.produce_state_update(device_info)

            except minimalmodbus.NoResponseError:
                logger.debug(
                    "No %s-modbus devices on %s", "extended" if is_ext_scan else "ordinary", debug_str
                )
            except Exception as e:
                logger.exception("Unhandled exception during scan %s" % port)
                raise PortScanningError(port=port) from e
            finally:
                self.ports_dbg_strs.remove(debug_str)
            await self.produce_state_update({"progress": progress_percent})


class RetcodeArgParser(ArgumentParser):
    def error(self, message):
        self.print_usage(stderr)
        self.exit(EXIT_INVALIDARGUMENT, "%s: error: %s\n" % (self.prog, message))


def main(args=argv):

    parser = RetcodeArgParser(description="Wiren Board serial devices manager")
    parser.add_argument(
        "-d",
        "--debug",
        dest="log_level",
        action="store_const",
        default=logging.INFO,
        const=logging.DEBUG,
        help="Set log_level to debug",
    )
    parser.add_argument(
        "-b",
        "--broker_url",
        dest="broker_url",
        type=str,
        help="MQTT url",
        default="unix:///var/run/mosquitto/mosquitto.sock",
    )
    args = parser.parse_args(argv[1:])

    # setup systemd logger
    formatter = logging.Formatter("[%(levelname)s] %(message)s")
    handler = logging.StreamHandler(stream=stdout)
    handler.setFormatter(formatter)
    handler.setLevel(args.log_level)
    for lgr in (logger, mb_logger):
        lgr.addHandler(handler)

    device_manager = DeviceManager(args.broker_url)

    async_callables_mapping = {
        ("bus-scan", "Start"): device_manager.launch_bus_scan,
        ("bus-scan", "Stop"): device_manager.stop_bus_scan,
    }

    server = mqtt_rpc.AsyncMQTTServer(
        methods_dispatcher=Dispatcher(async_callables_mapping),
        mqtt_connection=device_manager.mqtt_connection,
        mqtt_url_str=args.broker_url,
        rpc_client=device_manager.rpc_client,
        additional_topics_to_clear=[
            device_manager.state_publish_topic,
        ],
        asyncio_loop=device_manager.asyncio_loop,
    )

    try:
        server.setup()
    except Exception:
        ec = EXIT_FAILURE
        logger.exception("Exiting with %d", ec)
        return ec
    return server.run()
