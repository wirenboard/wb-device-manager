#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import uuid
import time
import logging
from sys import argv, stdout
from argparse import ArgumentParser
from itertools import product
from dataclasses import dataclass, asdict, field, is_dataclass
from mqttrpc import Dispatcher
from wb_modbus import instruments, minimalmodbus, ALLOWED_BAUDRATES, ALLOWED_PARITIES, ALLOWED_STOPBITS, logger as mb_logger
from wb_modbus.bindings import WBModbusDeviceBase
from . import logger, serial_bus, mqtt_rpc, shutdown_event


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

    def __init__(self, state_topic):
        with mqtt_rpc.MQTTConnManager().get_mqtt_connection() as conn:
            self.mqtt_connection = conn
        self.state_topic = state_topic
        self._init_state()

    def _init_state(self):
        self.state = BusScanState()

    @property
    def state_json(self):
        return json.dumps(asdict(self.state), indent=None, separators=(",", ":"), cls=SetEncoder)  # most compact

    def publish_state(self):  # TODO: an observer pattern; on_change callbacks
        self.mqtt_connection.publish(self.state_topic, self.state_json, retain=True)
        if shutdown_event.is_set():
            raise Exception("Shutdown event detected")  # TODO: more specified; with rpc-code

    def _get_mb_connection(self, device_info):
        conn = WBModbusDeviceBase(
            addr=device_info.cfg.slave_id,
            port=device_info.port.path,
            baudrate=device_info.cfg.baud_rate,
            parity=device_info.cfg.parity,
            stopbits=device_info.cfg.stop_bits,
            response_timeout=0.5,  # TODO: to arg
            instrument=instruments.SerialRPCBackendInstrument
        )
        return conn

    def _fill_fw_info(self, device_info):
        mb_conn = self._get_mb_connection(device_info)
        device_info.fw.version = mb_conn.get_fw_version()
        #TODO: fill available version from fw-releases

    def scan_serial_bus(self, *ports):

        def make_uuid(sn):
            return str(uuid.uuid3(namespace=uuid.NAMESPACE_OID, name=str(sn)))

        self._init_state()

        for port, bd, parity, stopbits in product(
            ports,
            ALLOWED_BAUDRATES,
            ALLOWED_PARITIES,
            ALLOWED_STOPBITS
        ):
            debug_str = "%s: %d-%s-%d" % (port, bd, parity, stopbits)
            logger.info("Scanning (via extended modbus) %s", debug_str)
            extended_modbus_scanner = serial_bus.WBExtendedModbusScanner(port)
            try:
                for slaveid, sn in extended_modbus_scanner.scan_bus(
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
                        device_info.fw_signature = mb_conn.get_fw_signature()
                        device_info.device_signature = mb_conn.get_device_signature()
                        self._fill_fw_info(device_info)
                    except minimalmodbus.ModbusException as e:
                        logger.exception("Treating device as offline")
                        device_info.online = False
                        device_info.error = str(e)

                    self.state.devices.add(device_info)
            except minimalmodbus.NoResponseError:
                logger.debug("No extended-modbus devices on %s", debug_str)
            self.state.progress += 1
            self.publish_state()
            #TODO: check all slaveids via ordinary modbus
        return True


def main(args=argv):

    parser = ArgumentParser(
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

    state_topic = mqtt_rpc.get_topic_path("bus_scan", "state")

    callables_mapping = {
        ("bus_scan", "scan") : lambda: DeviceManager(state_topic).scan_serial_bus(
                                    "/dev/ttyRS485-1",
                                    "/dev/ttyRS485-2",
                                    ),
        ("bus_scan", "test") : lambda: "Result of short-running task",
        }

    server = mqtt_rpc.MQTTServer(Dispatcher(callables_mapping))
    server.setup()
    server.loop()
