#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from unittest.mock import AsyncMock

from wb.device_manager import main
from wb.device_manager.bus_scan_state import ParsedPorts, get_all_uart_params


class DummyBusScanner(main.BusScanner):
    def __init__(self):
        self._mqtt_connection = AsyncMock()
        self._rpc_client = AsyncMock()
        self._state_update_queue = AsyncMock()
        self._asyncio_loop = AsyncMock()
        self._is_scanning = False


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


class TestDeviceManager(unittest.TestCase):
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
