#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import time
import uuid
from argparse import ArgumentParser
from collections import defaultdict
from dataclasses import asdict, dataclass, field, is_dataclass
from sys import argv, stderr, stdout

from mqttrpc import Dispatcher
from wb_common.mqtt_client import DEFAULT_BROKER_URL, MQTTClient
from wb_mcu_fw_updater import releases, update_monitor
from wb_modbus import bindings
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
    errors: list[StateError] = field(default_factory=list)
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


class ScanCancelCondition:
    def __init__(self):
        self._bus_scanning_task_cancel_event = asyncio.Event()

    def should_cancel(self):
        return self._bus_scanning_task_cancel_event.is_set()

    def request_cancel(self):
        self._bus_scanning_task_cancel_event.set()


class DeviceManager:
    MQTT_CLIENT_NAME = "wb-device-manager"
    STATE_PUBLISH_TOPIC = "/wb-device-manager/state"

    def __init__(self, broker_url: str):
        self._mqtt_connection = MQTTClient(self.MQTT_CLIENT_NAME, broker_url)
        self._rpc_client = mqtt_rpc.SRPCClient(self.mqtt_connection)
        self._state_update_queue = asyncio.Queue()
        self._asyncio_loop = asyncio.get_event_loop()
        self.asyncio_loop.create_task(self.consume_state_update(), name="Build & publish overall state")
        self._bus_scanning_task = None
        self._bus_scanning_task_cancel_condition = None
        self._found_devices = []
        self._ports_now_scanning = set()
        self._ports_errored = set()

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

    async def fill_device_info(self, device_info, mb_conn):
        errors = []

        err_ctx = None
        for reg_len in [20, bindings.WBModbusDeviceBase.DEVICE_SIGNATURE_LENGTH]:
            try:
                device_signature = await mb_conn.read_string(
                    first_addr=bindings.WBModbusDeviceBase.COMMON_REGS_MAP["device_signature"],
                    regs_length=reg_len,
                )
                device_info.device_signature = device_signature
                device_info.title = device_signature.strip(
                    "\x02"
                )  # TODO: store somewhere human-readable titles
                err_ctx = None
                break
            except minimalmodbus.ModbusException as e:
                err_ctx = e
        if err_ctx:
            logger.error("Failed to read device signature", exc_info=err_ctx)
            errors.append(ReadDeviceSignatureDeviceError())

        try:
            device_info.fw_signature = await mb_conn.read_string(
                first_addr=bindings.WBModbusDeviceBase.COMMON_REGS_MAP["fw_signature"],
                regs_length=bindings.WBModbusDeviceBase.FIRMWARE_SIGNATURE_LENGTH,
            )
        except minimalmodbus.ModbusException:
            logger.exception("Failed to read fw_signature")
            errors.append(ReadFWSignatureDeviceError())

        try:
            device_info.fw.version = await mb_conn.read_string(
                first_addr=bindings.WBModbusDeviceBase.COMMON_REGS_MAP["fw_version"],
                regs_length=bindings.WBModbusDeviceBase.FIRMWARE_VERSION_LENGTH,
            )
        except minimalmodbus.ModbusException:
            logger.exception("Failed to read fw_version")
            errors.append(ReadFWVersionDeviceError())

        device_info.errors.extend(errors)

    async def fill_serial_params(self, device_info, scanner):
        parities = {0: "N", 1: "O", 2: "E"}
        bd, parity, stopbits = "-", "-", "-"
        try:
            bd, parity, stopbits = await scanner.get_uart_params(device_info.cfg.slave_id, device_info.sn)
            if parity in parities.keys():
                parity = parities[parity]
        except minimalmodbus.ModbusException:
            logger.exception("Failed to read serial params from device")
            device_info.errors.append(ReadSerialParamsDeviceError())

        device_info.cfg.baud_rate = bd
        device_info.cfg.parity = parity
        device_info.cfg.stop_bits = stopbits

    def _get_all_uart_params(
        self,
        bds=[9600, 115200, 57600, 1200, 2400, 4800, 19200, 38400],
        parities=["N", "E", "O"],
        stopbits=[2, 1],
    ):
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

    async def get_ports(self) -> ParsedPorts:
        response = await self.rpc_client.make_rpc_call(
            driver="wb-mqtt-serial",
            service="ports",
            method="Load",
            params={},
            timeout=1.0,  # s; rpc call goes around scheduler queue => relatively small
        )
        serial_ports = []
        tcp_ports = []
        for port_info in response:
            if "path" in port_info:
                serial_ports.append(port_info["path"])
            elif "address" in port_info and "port" in port_info:
                tcp_ports.append(f"{port_info['address']}:{port_info['port']}")
        return ParsedPorts(serial=serial_ports, tcp=tcp_ports)

    async def launch_bus_scan(self, **kwargs):
        if self._bus_scanning_task and not self._bus_scanning_task.done():
            raise mqtt_rpc.MQTTRPCAlreadyProcessingException()
        logger.info("Start bus scanning")
        self._bus_scanning_task_cancel_condition = ScanCancelCondition()
        self._bus_scanning_task = self.asyncio_loop.create_task(
            self.scan_serial_bus(
                kwargs.get("scan_type"), kwargs.get("preserve_old_results"), kwargs.get("port")
            ),
            name="Scan serial bus (long running)",
        )
        return "Ok"

    async def stop_bus_scan(self):
        """
        TODO: check, tasks are actually cancelled before returning "Ok" via rpc
        Check https://docs.python.org/dev/library/asyncio-task.html#asyncio.Task.cancel for more info
        """
        if self._bus_scanning_task and not self._bus_scanning_task.done():
            logger.info("Stop bus scanning")
            self._bus_scanning_task_cancel_condition.request_cancel()
            self._bus_scanning_task.cancel()
            try:
                await self._bus_scanning_task
            except asyncio.CancelledError:
                pass
            return "Ok"
        else:
            raise mqtt_rpc.MQTTRPCAlreadyProcessingException()

    async def get_firmware_info(self, **kwargs):
        logger.debug("Request firmware info")
        modbus_wrapper = serial_bus.WBAsyncModbus(
            kwargs.get("address"),
            kwargs.get("path"),
            kwargs.get("baud_rate", 9600),
            kwargs.get("parity", "N"),
            kwargs.get("stop_bits", 2),
            self.rpc_client,
        )
        try:
            fw_signature = await modbus_wrapper.read_string(
                bindings.WBModbusDeviceBase.COMMON_REGS_MAP["fw_signature"],
                bindings.WBModbusDeviceBase.FIRMWARE_SIGNATURE_LENGTH,
            )
            logger.debug("Get firmware info for: %s", fw_signature)
            available_fw, _released_fw_endpoint = update_monitor.get_released_fw(
                fw_signature, releases.parse_releases("/usr/lib/wb-release")
            )
        except minimalmodbus.IllegalRequestError:
            available_fw = ""
            logger.debug("Can't get firmware signature, may be the device is too old")

        fw = await modbus_wrapper.read_string(
            bindings.WBModbusDeviceBase.COMMON_REGS_MAP["fw_version"],
            bindings.WBModbusDeviceBase.FIRMWARE_VERSION_LENGTH,
        )
        return {"fw": fw, "available_fw": available_fw}

    def _create_scan_tasks(self, ports, is_extended=False):
        tasks = []
        name_tmpl = "Extended scan %s %s" if is_extended else "Ordinary scan %s %s"
        for serial_port in ports.serial:
            tasks.append(
                asyncio.create_task(
                    self.scan_serial_port(serial_port, is_ext_scan=is_extended),
                    name=name_tmpl % (serial_port, "serial"),
                )
            )
        for tcp_port in ports.tcp:
            tasks.append(
                asyncio.create_task(
                    self.scan_tcp_port(tcp_port, is_ext_scan=is_extended), name=name_tmpl % (tcp_port, "tcp")
                )
            )

        return tasks

    @staticmethod
    def make_uuid(sn):
        return str(uuid.uuid3(namespace=uuid.NAMESPACE_OID, name=str(sn)))

    async def scan_serial_bus(self, scan_type, preserve_old_results, port_config):
        # TODO: introduce state-accumulator to communicate with worker-coros and get rid of these global vars
        new_state = {
            "scanning": True,
            "progress": 0,
            "error": None,
        }
        if not preserve_old_results:
            self._found_devices = []
            new_state["devices"] = []
        await self.produce_state_update(new_state)
        self._ports_now_scanning = set()
        self._ports_errored = set()

        state_error = None
        if isinstance(port_config, dict) and "path" in port_config:
            ports = ParsedPorts(serial=[port_config["path"]], tcp=[])
        else:
            try:
                ports = await self.get_ports()
            except mqtt_rpc.MQTTRPCInternalServerError:
                logger.exception("No answer from wb-mqtt-serial")
                state_error = RPCCallTimeoutStateError()
                ports = ParsedPorts()
        try:
            if scan_type == "extended":
                await asyncio.gather(
                    *self._create_scan_tasks(ports, is_extended=True), return_exceptions=True
                )
            elif scan_type == "standard":
                await asyncio.gather(
                    *self._create_scan_tasks(ports, is_extended=False), return_exceptions=True
                )
            else:
                await asyncio.gather(
                    *self._create_scan_tasks(ports, is_extended=True), return_exceptions=True
                )
                await self.produce_state_update({"progress": 0})
                await asyncio.gather(
                    *self._create_scan_tasks(ports, is_extended=False), return_exceptions=True
                )

            await self.produce_state_update(
                {"scanning": False, "progress": 100, "scanning_ports": self._ports_now_scanning}
            )
            if self._ports_errored:
                logger.warning("Unsuccessful scan: %s", str(self._ports_errored))
                state_error = FailedScanStateError(failed_ports=self._ports_errored)
        except Exception as e:
            state_error = GenericStateError()
            logger.exception("Unhandled exception during overall scan")
        finally:
            await self.produce_state_update({"scanning": False, "progress": 0, "error": state_error})

    async def do_scan_port(self, path, scanner, is_ext_scan, **scan_kwargs):
        debug_str = path + " " + "-".join(map(str, scan_kwargs.values()))

        self._ports_now_scanning.add(debug_str)
        logger.info(
            "Scanning %s (extended modbus: %r)",
            debug_str,
            is_ext_scan,
        )
        await self.produce_state_update(
            {"scanning_ports": self._ports_now_scanning, "is_ext_scan": is_ext_scan}
        )

        try:
            async for slaveid, sn in scanner.scan_bus(
                **scan_kwargs, cancel_condition=self._bus_scanning_task_cancel_condition
            ):
                if self._bus_scanning_task_cancel_condition.should_cancel():
                    return
                if sn in self._found_devices:
                    logger.info("Device %s already scanned; skipping", str(sn))
                    continue

                device_info = DeviceInfo(
                    uuid=self.make_uuid(sn),
                    title="Unknown",
                    sn=str(sn),
                    last_seen=int(time.time()),
                    online=True,
                    poll=True,  # TODO: support "is_polling" rpc call in wb-mqtt-serial
                    port=Port(path),
                    cfg=SerialParams(slave_id=slaveid),
                )
                device_info.fw.ext_support = is_ext_scan

                addr = int(device_info.sn) if is_ext_scan else device_info.cfg.slave_id
                mb_conn = scanner.get_mb_connection(addr, path, **scan_kwargs)

                await self.fill_device_info(device_info, mb_conn)
                await self.fill_serial_params(device_info, scanner)

                self._found_devices.append(sn)
                await self.produce_state_update(device_info)

        except minimalmodbus.NoResponseError:
            logger.debug("No %s-modbus devices on %s", "extended" if is_ext_scan else "ordinary", debug_str)
        except minimalmodbus.InvalidResponseError as err:
            logger.error("Invalid response during scan %s: %s", debug_str, err)
        except Exception:
            logger.exception("Unhandled exception during scan %s", debug_str)
            self._ports_errored.add(debug_str)
            await self.produce_state_update({"error": FailedScanStateError(failed_ports=self._ports_errored)})
            raise
        finally:
            self._ports_now_scanning.discard(debug_str)

    async def scan_serial_port(self, port, is_ext_scan=True):
        if is_ext_scan:
            scanner = serial_bus.WBExtendedModbusScanner(port, self.rpc_client)
        else:
            scanner = serial_bus.WBModbusScanner(port, self.rpc_client)

        # New firmwares can work with any stopbits, but old ones can't
        # Since it doesn't matter what to use, let's use 2
        allowed_stopbits = (
            [
                2,
            ]
            if is_ext_scan
            else [2, 1]
        )

        for bd, parity, stopbits, progress_percent in self._get_all_uart_params(stopbits=allowed_stopbits):
            if self._bus_scanning_task_cancel_condition.should_cancel():
                return
            await self.do_scan_port(port, scanner, is_ext_scan, baudrate=bd, parity=parity, stopbits=stopbits)
            await self.produce_state_update({"progress": progress_percent})

    async def scan_tcp_port(self, ip_port, is_ext_scan=True):
        if is_ext_scan:
            scanner = serial_bus.WBExtendedModbusScannerTCP(ip_port, self.rpc_client)
        else:
            scanner = serial_bus.WBModbusScannerTCP(ip_port, self.rpc_client)

        await self.do_scan_port(ip_port, scanner, is_ext_scan)


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
        "--broker",
        "--broker_url",
        dest="broker_url",
        type=str,
        help="MQTT broker url",
        default=DEFAULT_BROKER_URL,
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
        ("bus-scan", "GetFirmwareInfo"): device_manager.get_firmware_info,
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
