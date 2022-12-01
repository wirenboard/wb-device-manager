#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import uuid
import time
import logging
import asyncio
import paho.mqtt.client as mosquitto
from sys import argv, stdout, stderr
from argparse import ArgumentParser
from itertools import product
from dataclasses import dataclass, asdict, field, is_dataclass
from mqttrpc import Dispatcher
from wb_modbus import minimalmodbus, ALLOWED_BAUDRATES, ALLOWED_PARITIES, ALLOWED_STOPBITS, logger as mb_logger
from . import logger, serial_bus, mqtt_rpc


EXIT_INVALIDARGUMENT = 2
EXIT_UNKNOWN = 4


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
    error: str = None
    available_fw: str = None

@dataclass
class Firmware:
    version: str = None
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
    error: str = None
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
    devices: set[DeviceInfo] = field(default_factory=set)


class SetEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, set):
            return list(o)
        if is_dataclass(o):
            return asdict(o)
        super().default(o)


class DeviceManager():
    MQTT_CLIENT_NAME = "wb-device-manager"
    STATE_PUBLISH_TOPIC = "/wb-device-manager/state"

    def __init__(self):
        self._mqtt_connection = mosquitto.Client(self.MQTT_CLIENT_NAME)
        self._rpc_client = mqtt_rpc.SRPCClient(self.mqtt_connection)
        self._state_publish_queue = asyncio.Queue()
        self._asyncio_loop = asyncio.get_event_loop()
        self._init_state()
        self.asyncio_loop.create_task(self.publish_state_consume(), name="Publish overall state")
        self.state_correctness_lock = asyncio.Lock()

    def _init_state(self):
        self.state = BusScanState()

    @property
    def mqtt_connection(self):
        return self._mqtt_connection

    @property
    def rpc_client(self):
        return self._rpc_client

    @property
    def state_publish_queue(self):
        return self._state_publish_queue

    @property
    def asyncio_loop(self):
        return self._asyncio_loop

    @property
    def state_json(self):
        return json.dumps(asdict(self.state), indent=None, separators=(",", ":"), cls=SetEncoder)  # most compact

    async def publish_state_produce(self):  # TODO: an observer pattern; on_change callbacks
        await self.state_publish_queue.put(self.state_json)

    async def publish_state_consume(self):
        while True:
            state_json = await self.state_publish_queue.get()
            self.mqtt_connection.publish(self.STATE_PUBLISH_TOPIC, state_json, retain=True)

    def _get_mb_connection(self, device_info):
        conn = serial_bus.WBAsyncModbus(
            addr=device_info.cfg.slave_id,
            port=device_info.port.path,
            baudrate=device_info.cfg.baud_rate,
            parity=device_info.cfg.parity,
            stopbits=device_info.cfg.stop_bits,
            rpc_client=self.rpc_client
        )
        return conn

    async def _fill_fw_info(self, device_info):
        mb_conn = self._get_mb_connection(device_info)
        device_info.fw.version = await mb_conn.read_string(first_addr=250, regs_length=16)
        #TODO: fill available version from fw-releases

    async def _get_all_uart_params(self):
        for bd, parity, stopbits in product(
            ALLOWED_BAUDRATES,
            ALLOWED_PARITIES,
            ALLOWED_STOPBITS
        ):
            yield bd, parity, stopbits

    async def _get_ports(self):
        #TODO: rpc call to wb-mqtt-serial
        for port in ["/dev/ttyRS485-1", "/dev/ttyRS485-2"]:
            yield port

    async def launch_scan(self):
        if self.state.scanning:
            raise mqtt_rpc.MQTTRPCAlreadyProcessingException()
        else:
            self.asyncio_loop.create_task(self.scan_serial_bus(), name="Scan serial bus (long running)")
            return "Ok"

    async def scan_serial_bus(self):
        tasks = []
        self._init_state()
        self.state.scanning = True
        async for port in self._get_ports():
            tasks.append(self.scan_serial_port(port))
        try:
            await asyncio.gather(*tasks)
        except Exception as e:
            self.state.error = str(e)
        finally:
            self.state.scanning = False
            await self.publish_state_produce()

    async def scan_serial_port(self, port):

        def make_uuid(sn):
            return str(uuid.uuid3(namespace=uuid.NAMESPACE_OID, name=str(sn)))

        async for bd, parity, stopbits in self._get_all_uart_params():
            debug_str = "%s: %d-%s-%d" % (port, bd, parity, stopbits)
            logger.info("Scanning (via extended modbus) %s", debug_str)
            extended_modbus_scanner = serial_bus.WBExtendedModbusScanner(port, self.rpc_client)
            async with self.state_correctness_lock:
                try:
                    async for slaveid, sn in extended_modbus_scanner.scan_bus(
                        baudrate=bd,
                        parity=parity,
                        stopbits=stopbits
                    ):
                        device_info = DeviceInfo(
                            uuid=make_uuid(sn),
                            title="Scanned device",
                            sn=str(sn),
                            last_seen=int(time.time()),
                            online=True,
                            port=Port(path=port),
                            cfg=SerialParams(
                                slave_id=slaveid,
                                baud_rate=bd,
                                parity=parity,
                                stop_bits=stopbits
                            )
                        )

                        mb_conn = self._get_mb_connection(device_info)

                        try:
                            device_info.fw_signature = await mb_conn.read_string(first_addr=290, regs_length=12)
                            device_info.device_signature = await mb_conn.read_string(first_addr=200, regs_length=6)
                            await self._fill_fw_info(device_info)
                        except minimalmodbus.ModbusException as e:
                            logger.exception("Treating device as offline")
                            device_info.online = False
                            device_info.error = str(e)

                        self.state.devices.add(device_info)
                        await self.publish_state_produce()
                except minimalmodbus.NoResponseError:
                    logger.debug("No extended-modbus devices on %s", debug_str)
            self.state.progress += 1
            await self.publish_state_produce()
            #TODO: check all slaveids via ordinary modbus
        return True

    async def _all_devices(self):
        for device in self.state.devices:
            yield device

    async def read_fwsigs(self):  # For testing purpose
        fwsigs = []
        async for device in self._all_devices():
            mb_conn = self._get_mb_connection(device)
            ret = await mb_conn.read_string(290, 12)
            fwsigs.append(ret)
        return fwsigs


class RetcodeArgParser(ArgumentParser):
    def error(self, message):
        self.print_usage(stderr)
        self.exit(EXIT_INVALIDARGUMENT, "%s: error: %s\n" % (self.prog, message))


def main(args=argv):

    parser = RetcodeArgParser(
        description="Wiren Board serial devices manager")
    parser.add_argument("-d", "--debug", dest="log_level", action="store_const", default=logging.INFO,
                const=logging.DEBUG, help="Set log_level to debug")
    args = parser.parse_args(argv[1:])

    # setup systemd logger
    formatter = logging.Formatter("[%(levelname)s] %(message)s")
    handler = logging.StreamHandler(stream=stdout)
    handler.setFormatter(formatter)
    handler.setLevel(args.log_level)
    for lgr in (logger, mb_logger):
        lgr.addHandler(handler)

    device_manager = DeviceManager()

    callables_mapping = {
        ("bus-scan", "Scan") : device_manager.launch_scan,
        ("bus-scan", "Fwsigs") : device_manager.read_fwsigs  # for testing purpose
        }

    server = mqtt_rpc.AsyncMQTTServer(
        methods_dispatcher=Dispatcher(callables_mapping),
        mqtt_connection=device_manager.mqtt_connection,
        rpc_client=device_manager.rpc_client,
        asyncio_loop=device_manager.asyncio_loop
    )

    try:
        server.setup()
    except Exception:
        ec = EXIT_UNKNOWN
        logger.exception("Exiting with %d", ec)
        return ec
    return server.run()
