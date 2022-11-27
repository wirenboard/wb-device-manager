#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from unittest.mock import AsyncMock

from wb.device_manager import serial_bus
from wb_modbus import minimalmodbus


class DummyScanner(serial_bus.WBExtendedModbusScanner):

    def __init__(self, *args, **kwargs):
        self.instrument = AsyncMock()
        self.port = kwargs.get("port", "/dev/dummyport")


class TestMBExtendedScanner(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        cls.scanner = DummyScanner(port="/dev/dummyport")

    @classmethod
    def mock_response(cls, response_hex_str):
        ret = minimalmodbus._hexdecode(response_hex_str)
        cls.scanner.instrument._communicate = AsyncMock(return_value=ret)  # we have no actual serial devices

    async def test_correct_response(self):
        assumed_response = "FE34359603"
        self.mock_response(
            "fffffffffffffffffd6003fe34359603f6a80000000000000000"
        )
        ret = await self.scanner.get_next_device_data()
        ret = minimalmodbus._hexencode(ret)
        self.assertEqual(assumed_response, ret)

    async def test_correct_scan_init(self):
        self.mock_response("")  # No answer to broadcast scan-init cmd
        await self.scanner.init_bus_scan()

    async def test_correct_scan_end(self):
        operation_code = "04"
        self.mock_response(
            "fffffffd60" + operation_code + "c9f3000000"
        )
        ret = await self.scanner.get_next_device_data()
        self.assertIsNone(ret)

    async def test_unsupported_operation_code(self):
        unsupported_code = "11"
        self.mock_response(
            "fffffffffffffffffd60" + unsupported_code + "fe34359603f6a80000000000000000"
        )
        with self.assertRaises(minimalmodbus.InvalidResponseError):
            await self.scanner.get_next_device_data()

    async def test_empty_answer(self):
        self.mock_response("")  # TODO: empty rpc-answer means no support in wb-mqtt-serial; maybe custom error?
        with self.assertRaises(minimalmodbus.InvalidResponseError):
            await self.scanner.get_next_device_data()

    async def test_corrupted_answer(self):
        incorrect_crc = "ffff"
        self.mock_response(
            "fffffffffd6003fe34359603" + incorrect_crc + "00000000"
        )
        with self.assertRaises(minimalmodbus.InvalidResponseError):
            await self.scanner.get_next_device_data()
