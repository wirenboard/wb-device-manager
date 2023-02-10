#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
import uuid
from unittest.mock import AsyncMock

from wb_modbus import minimalmodbus

from wb.device_manager import main


class DummyDeviceManager(main.DeviceManager):
    def __init__(self):
        self._mqtt_connection = AsyncMock()
        self._rpc_client = AsyncMock()
        self._state_update_queue = AsyncMock()
        self._asyncio_loop = AsyncMock()
        self._is_scanning = False


class TestRPCClient(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.device_manager = DummyDeviceManager()

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
        assumed_response = ["/dev/ttyRS485-1", "/dev/ttyRS485-2"]
        self.mock_response(response)
        ret = await self.device_manager._get_ports()
        self.assertListEqual(ret, assumed_response)


class TestExternalDeviceErrors(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.device_manager = DummyDeviceManager()

        cls.device_info = main.DeviceInfo(
            uuid=uuid.uuid3(namespace=uuid.NAMESPACE_OID, name="dummy_device"),
            port=main.Port(path="/dev/ttyDUMMY"),
            sn=12345,
            cfg=main.SerialParams(slave_id=1),
        )

        cls.mb_conn = cls.device_manager._get_mb_connection(cls.device_info, True)

    @classmethod
    def mock_error(cls, errtype=minimalmodbus.ModbusException):
        cls.device_manager.rpc_client.make_rpc_call = AsyncMock(side_effect=errtype)

    async def test_erroneous_fill_device_info(self):
        self.mock_error()
        assumed_errors = [
            main.ReadDeviceSignatureDeviceError(),
            main.ReadFWSignatureDeviceError(),
            main.ReadFWVersionDeviceError(),
        ]
        ret = await self.device_manager.fill_device_info(self.device_info, self.mb_conn)
        self.assertListEqual(ret, assumed_errors)


class TestDeviceManager(unittest.TestCase):
    def setUp(self):
        self.device_manager = DummyDeviceManager()

    def test_get_all_uart_params(self):
        assumed_order = [
            "115200-N2",
            "9600-N2",
            "57600-N2",
            "115200-N1",
            "9600-N1",
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
            "115200-E2",
            "9600-E2",
            "57600-E2",
            "115200-E1",
            "9600-E1",
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
            "115200-O2",
            "9600-O2",
            "57600-O2",
            "115200-O1",
            "9600-O1",
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
        for bd, parity, stopbits, progress_percent in self.device_manager._get_all_uart_params():
            self.assertEqual(f"{bd}-{parity}{stopbits}", assumed_order.pop(0))
