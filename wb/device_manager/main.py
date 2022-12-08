#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import uuid
import time
import logging
import asyncio
import copy
import paho.mqtt.client as mosquitto
from sys import argv, stdout, stderr
from argparse import ArgumentParser
from itertools import product, count
from collections import deque
from dataclasses import dataclass, asdict, field, is_dataclass
from mqttrpc import Dispatcher
from wb_modbus import minimalmodbus, ALLOWED_BAUDRATES, ALLOWED_PARITIES, ALLOWED_STOPBITS, logger as mb_logger
from . import logger, serial_bus, mqtt_rpc


EXIT_INVALIDARGUMENT = 2
EXIT_FAILURE = 1


def count_any_iterable(iterable):
    it = copy.deepcopy(iterable)
    counter = count()
    deque(zip(it, counter), maxlen=0)
    return next(counter)


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
    error: str = None
    devices: set[DeviceInfo] = field(default_factory=set)

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


class DeviceManager():
    MQTT_CLIENT_NAME = "wb-device-manager"
    STATE_PUBLISH_TOPIC = "/wb-device-manager/state"

    def __init__(self):
        self._mqtt_connection = mosquitto.Client(self.MQTT_CLIENT_NAME)
        self._rpc_client = mqtt_rpc.SRPCClient(self.mqtt_connection)
        self._state_update_queue = asyncio.Queue()
        self._asyncio_loop = asyncio.get_event_loop()
        self.asyncio_loop.create_task(self.consume_state_update(), name="Build & publish overall state")
        self._init_state()

    def _init_state(self):
        self.state = BusScanState()

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

    @property
    def state_json(self):
        return json.dumps(asdict(self.state), indent=None, separators=(",", ":"), cls=SetEncoder)  # most compact

    async def produce_state_update(self, event={}):  # TODO: an observer pattern; on_change callbacks
        await self.state_update_queue.put(event)

    async def consume_state_update(self):
        """
        The only func, allowed to change state directly
        """
        while True:
            event = await self.state_update_queue.get()
            try:
                if isinstance(event, DeviceInfo):
                    self.state.devices.add(event)
                elif isinstance(event, dict):
                    self.state.update(event)
                else:
                    e = RuntimeError("Got incorrect state-update event: %s", repr(event))
                    self.state.error = str(e)
                    self.state.scanning = False
                    raise e
            finally:
                self.mqtt_connection.publish(self.STATE_PUBLISH_TOPIC, self.state_json, retain=True)

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

    async def _get_all_uart_params(self, bds=ALLOWED_BAUDRATES, parities=ALLOWED_PARITIES.keys(),
        stopbits=ALLOWED_STOPBITS):
        iterable = product(bds, parities, stopbits)
        len_iterable = count_any_iterable(iterable)
        for pos, (bd, parity, stopbit) in enumerate(iterable):
            yield bd, parity, stopbit, int(pos / len_iterable * 100)

    async def _get_ports(self):
        response = await self.rpc_client.make_rpc_call(
            driver="wb-mqtt-serial",
            service="ports",
            method="Load",
            params={},
            timeout=1.0  # rpc call goes around scheduler queue => relatively small
            )
        return [port["path"] for port in response]  # TODO: use serial_params from response?

    async def launch_scan(self):
        if self.state.scanning:
            raise mqtt_rpc.MQTTRPCAlreadyProcessingException()
        else:
            self.asyncio_loop.create_task(self.scan_serial_bus(), name="Scan serial bus (long running)")
            return "Ok"

    async def scan_serial_bus(self):
        tasks = []
        self._init_state()
        await self.produce_state_update(
                {
                    "devices" : set(),  # TODO: unit test?
                    "scanning" : True,
                    "progress" : 0
                }
            )
        try:
            ports = await self._get_ports()
            for port in ports:
                tasks.append(self.scan_serial_port(port))
            await asyncio.gather(*tasks)
            await self.produce_state_update(
                {
                    "scanning" : False,
                    "progress" : 100
                }
            )
        except Exception as e:
            await self.produce_state_update({"error" : str(e)})
        finally:
            await self.produce_state_update(
                {
                    "scanning" : False,
                    "progress" : 0
                }
            )

    async def scan_serial_port(self, port):

        def make_uuid(sn):
            return str(uuid.uuid3(namespace=uuid.NAMESPACE_OID, name=str(sn)))

        extended_modbus_scanner = serial_bus.WBExtendedModbusScanner(port, self.rpc_client)

        async for bd, parity, stopbits, progress_precent in self._get_all_uart_params(stopbits=[1,]):
            debug_str = "%s: %d-%s-%d" % (port, bd, parity, stopbits)
            logger.info("Scanning (via extended modbus) %s", debug_str)
            try:
                async for slaveid, sn in extended_modbus_scanner.scan_bus(
                    baudrate=bd,
                    parity=parity,
                    stopbits=stopbits
                ):
                    device_info = DeviceInfo(
                        uuid=make_uuid(sn),
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

                    try:
                        device_info.title = await self._get_mb_connection(device_info).read_string(
                            first_addr=200, regs_length=6
                            )
                        device_info.fw_signature = await self._get_mb_connection(device_info).read_string(
                            first_addr=290, regs_length=12
                            )
                        device_info.device_signature = await self._get_mb_connection(device_info).read_string(
                            first_addr=200, regs_length=6
                            )
                        await self._fill_fw_info(device_info)
                    except minimalmodbus.ModbusException as e:
                        logger.exception("Treating device %s as offline", str(device_info))
                        device_info.online = False
                        device_info.error = str(e)
                    finally:
                        await self.produce_state_update(device_info)

            except minimalmodbus.NoResponseError:
                    logger.debug("No extended-modbus devices on %s", debug_str)
            if progress_precent > self.state.progress:
                await self.produce_state_update({"progress" : progress_precent})
            #TODO: check all slaveids via ordinary modbus

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
        additional_topics_to_clear=[device_manager.state_publish_topic, ],
        asyncio_loop=device_manager.asyncio_loop
    )

    try:
        server.setup()
    except Exception:
        ec = EXIT_FAILURE
        logger.exception("Exiting with %d", ec)
        return ec
    return server.run()
