#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
import uuid
from unittest.mock import AsyncMock

from wb_modbus import minimalmodbus

from wb.device_manager import main
from wb.device_manager.bus_scan_state import (
    DeviceInfo,
    ParsedPorts,
    Port,
    SerialParams,
    get_all_uart_params,
)
from wb.device_manager.one_by_one_scan import OneByOneBusScanner
from wb.device_manager.serial_bus import WBAsyncModbus, WBModbusScanner
from wb.device_manager.state_error import (
    ReadDeviceSignatureDeviceError,
    ReadFWSignatureDeviceError,
    ReadFWVersionDeviceError,
)


class DummyBusScanner(main.BusScanner):
    def __init__(self):  # pylint: disable=super-init-not-called, unused-argument
        self._mqtt_connection = AsyncMock()
        self._rpc_client = AsyncMock()
        self._state_update_queue = AsyncMock()
        self._asyncio_loop = AsyncMock()
        self._is_scanning = False


class DummyScanner(WBModbusScanner):
    def __init__(self, *args, **kwargs):  # pylint: disable=super-init-not-called, unused-argument
        self.port = "dummy_port"
        self.rpc_client = AsyncMock()
        self.instrument_cls = AsyncMock
        self.modbus_wrapper = WBAsyncModbus
        self.uart_params_mapping = {}


class TestRPCClient(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.device_manager = DummyBusScanner()

    @classmethod
    def mock_response(cls, response):
        cls.device_manager.rpc_client.make_rpc_call = AsyncMock(return_value=response)

    async def test_ports_acquiring(self):
        response = [
            {
                "baud_rate": 9600,
                "data_bits": 8,
                "parity": "N",
                "path": "/dev/ttyRS485-1",
                "stop_bits": 2,
            },
            {
                "baud_rate": 9600,
                "data_bits": 8,
                "parity": "N",
                "path": "/dev/ttyRS485-2",
                "stop_bits": 2,
            },
            {"address": "192.168.0.7", "port": 23},
        ]
        assumed_response = ParsedPorts(
            serial=["/dev/ttyRS485-1", "/dev/ttyRS485-2"],
            tcp=[
                "192.168.0.7:23",
            ],
        )
        self.mock_response(response)
        ret = await self.device_manager.get_ports()
        self.assertEqual(ret, assumed_response)


class TestExternalDeviceErrors(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.one_by_one_scanner = OneByOneBusScanner(AsyncMock(), AsyncMock())

        cls.device_info = DeviceInfo(
            uuid=uuid.uuid3(namespace=uuid.NAMESPACE_OID, name="dummy_device"),
            port=Port("/dev/ttyDUMMY"),
            sn=12345,
            cfg=SerialParams(slave_id=1),
        )

        cls.mb_conn = DummyScanner().get_mb_connection(
            cls.device_info.cfg.slave_id, cls.device_info.port.path
        )

    @classmethod
    def mock_error(cls, errtype=minimalmodbus.ModbusException):
        cls.mb_conn._do_read = AsyncMock(side_effect=errtype)  # pylint: disable=protected-access

    async def test_erroneous_fill_device_info(self):
        self.mock_error()
        assumed_errors = [
            ReadDeviceSignatureDeviceError(),
            ReadFWSignatureDeviceError(),
            ReadFWVersionDeviceError(),
        ]
        await self.one_by_one_scanner._fill_device_info(  # pylint: disable=protected-access
            self.device_info, self.mb_conn
        )
        self.assertListEqual(self.device_info.errors, assumed_errors)


class TestOneByOneBusScanner(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scanner = DummyScanner()

        self.device_info = DeviceInfo(
            uuid=uuid.uuid3(namespace=uuid.NAMESPACE_OID, name="dummy_device"),
            port=Port("/dev/ttyDUMMY"),
            sn=12345,
            cfg=SerialParams(slave_id=1),
        )

    async def test_fill_uart_params(self):
        self.scanner.get_uart_params = AsyncMock(return_value=[9600, "N", 2])
        one_by_one_scanner = OneByOneBusScanner(AsyncMock(), AsyncMock())
        await one_by_one_scanner._fill_serial_params(  # pylint: disable=protected-access
            self.device_info, self.scanner
        )
        self.assertEqual(self.device_info.cfg.baud_rate, 9600)
        self.assertEqual(self.device_info.cfg.parity, "N")
        self.assertEqual(self.device_info.cfg.stop_bits, 2)

    async def test_fill_uart_params_err(self):
        self.scanner.get_uart_params = AsyncMock(  # pylint: disable=protected-access
            side_effect=minimalmodbus.ModbusException
        )
        one_by_one_scanner = OneByOneBusScanner(AsyncMock(), AsyncMock())
        await one_by_one_scanner._fill_serial_params(  # pylint: disable=protected-access
            self.device_info, self.scanner
        )
        self.assertEqual(self.device_info.cfg.baud_rate, "-")
        self.assertEqual(self.device_info.cfg.parity, "-")
        self.assertEqual(self.device_info.cfg.stop_bits, "-")


class TestDeviceManager(unittest.TestCase):
    def setUp(self):
        self.device_manager = DummyBusScanner()

    def test_get_all_uart_params(self):
        assumed_order = [
            "9600-N2",
            "115200-N2",
            "57600-N2",
            "9600-N1",
            "115200-N1",
            "57600-N1",
            "1200-N2",
            "2400-N2",
            "4800-N2",
            "19200-N2",
            "38400-N2",
            "1200-N1",
            "2400-N1",
            "4800-N1",
            "19200-N1",
            "38400-N1",
            "9600-E2",
            "115200-E2",
            "57600-E2",
            "9600-E1",
            "115200-E1",
            "57600-E1",
            "1200-E2",
            "2400-E2",
            "4800-E2",
            "19200-E2",
            "38400-E2",
            "1200-E1",
            "2400-E1",
            "4800-E1",
            "19200-E1",
            "38400-E1",
            "9600-O2",
            "115200-O2",
            "57600-O2",
            "9600-O1",
            "115200-O1",
            "57600-O1",
            "1200-O2",
            "2400-O2",
            "4800-O2",
            "19200-O2",
            "38400-O2",
            "1200-O1",
            "2400-O1",
            "4800-O1",
            "19200-O1",
            "38400-O1",
        ]
        for bd, parity, stopbits in get_all_uart_params():
            self.assertEqual(f"{bd}-{parity}{stopbits}", assumed_order.pop(0))
