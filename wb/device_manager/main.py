#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from sys import argv
from typing import Any
from itertools import product
from dataclasses import dataclass, asdict, field
from mqttrpc import Dispatcher
from wb_modbus import instruments, minimalmodbus, ALLOWED_BAUDRATES, ALLOWED_PARITIES, ALLOWED_STOPBITS
from wb_modbus.bindings import WBModbusDeviceBase
from . import logger, serial_bus, mqtt_rpc, shutdown_event


@dataclass
class SerialParams:
    slave_id: int
    baud_rate: int = 9600
    parity: str = "N"
    data_bits: int = 8
    stop_bits: int = 2

@dataclass
class DeviceInfo:  # TODO: make hashable by sn
    type: str
    serial: str  # str(int) for wb-devices
    port: str  # TODO: dataclass for port?
    is_polled: bool = False
    is_online: bool = False
    is_in_bootloader: bool = False
    error: str = None
    cfg: Any = None

@dataclass
class BusScanState:
    progress: int  # TODO: maybe no scanning?
    scanning: bool = False
    error: str = None
    devices: list[DeviceInfo] = field(default_factory=list)


class DeviceManager():

    def __init__(self):
        with mqtt_rpc.MQTTConnManager().get_mqtt_connection() as conn:
            self.mqtt_connection = conn

    def _init_state(self):
        self.state = BusScanState(
            progress=None,  # TODO: maybe separate "progress" topic?
            scanning=False
        )

    def state_json(self):
        return json.dumps(asdict(self.state), indent=4)

    def scan_serial_bus(self, state_topic, ports):

        def publish_state():
            self.mqtt_connection.publish(state_topic, self.state_json(), retain=True)
            if shutdown_event.is_set():
                raise Exception("Shutdown event detected")  # TODO: more specified; with rpc-code

        self._init_state()

        for port, bd, parity, stopbits in product(
            ports,
            ALLOWED_BAUDRATES,
            ALLOWED_PARITIES,
            ALLOWED_STOPBITS
        ):
            debug_str = "%s: %d-%s-%d" % (port, bd, parity, stopbits)
            logger.debug("Scanning %s", debug_str)
            extended_scanner = serial_bus.WBExtendedModbusScanner(port)
            try:
                for slaveid, sn in extended_scanner.scan_bus(
                    baudrate=bd,
                    parity=parity,
                    stopbits=stopbits
                ):
                    device_state = DeviceInfo(
                        type="Scanned device",
                        serial=str(sn),
                        port=port,
                        cfg=SerialParams(
                            slave_id=slaveid,
                            baud_rate=bd,
                            parity=parity,
                            stop_bits=stopbits
                        )
                    )
                    self.state.devices.append(device_state)
            except minimalmodbus.NoResponseError:
                logger.debug("No extended-modbus devices on %s", debug_str)
            publish_state()
            #TODO: check all slaveids via ordinary modbus
        return True


def main(args=argv):
    #TODO: separate debug for mqtt/modbus/logic via -d?

    callables_mapping = {
        ("bus_scan", "scan") : lambda: DeviceManager().scan_serial_bus(
                                    state_topic=mqtt_rpc.get_topic_path("bus_scan", "state"),
                                    ports=["/dev/ttyRS485-1", "/dev/ttyRS485-2"]
                                    ),
        ("bus_scan", "test") : lambda: "Result of short-running task"
        }

    server = mqtt_rpc.MQTTServer(Dispatcher(callables_mapping))
    server.setup()
    server.loop()
