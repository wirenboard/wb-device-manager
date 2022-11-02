#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from typing import Any
from itertools import product
from dataclasses import dataclass, asdict, field
from wb_modbus import instruments, minimalmodbus, ALLOWED_BAUDRATES, ALLOWED_PARITIES, ALLOWED_STOPBITS
from wb_modbus.bindings import WBModbusDeviceBase
from . import logger, serial_bus


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

    def _init_state(self):
        self.state = BusScanState(
            progress=None,
            scanning=False
        )

    def scan_serial_bus(self, *ports_list):
        self._init_state()

        for port, bd, parity, stopbits in product(
            ports_list,
            ALLOWED_BAUDRATES,
            ALLOWED_PARITIES,
            ALLOWED_STOPBITS
        ):
            debug_str = "%s: %d-%s-%d" % (port, bd, parity, stopbits)
            logger.debug("Scanning %s", debug_str)
            extended_scanner = serial_bus.ExtendedMBusScanner(port)
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
                    print(self.state)  #  TODO: publish to mqtt
            except minimalmodbus.NoResponseError:
                logger.debug("No extended-modbus devices on %s", debug_str)
            #TODO: check all slaveids via ordinary modbus

    def fill_devices_info(self):
        for device_info in self.stat.devices:
            mb_conn = WBModbusDeviceBase(
                addr=device_info.cfg.slave_id,
                port=device_info.port,
                baudrate=device_info.cfg.baud_rate,
                parity=device_info.cfg.parity,
                stopbits=device_info.cfg.stop_bits,
                instrument=instruments.SerialRPCBackendInstrument
            )
