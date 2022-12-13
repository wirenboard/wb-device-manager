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
        cls.uart_params = {
            "baudrate" : 9600,
            "parity" : "N",
            "stopbits" : 1
        }

    @classmethod
    def mock_response(cls, response_hex_str):
        ret = minimalmodbus._hexdecode(response_hex_str)
        cls.scanner.instrument._communicate = AsyncMock(return_value=ret)  # we have no actual serial devices

    def test_arbitration_timeout_calculation(self):
        timeouts = {  # TODO: get rid of minimalmodbus
            1200 : minimalmodbus._calculate_minimum_silent_period(1200),
            2400 : minimalmodbus._calculate_minimum_silent_period(2400),
            4800 : minimalmodbus._calculate_minimum_silent_period(4800),
            9600 : minimalmodbus._calculate_minimum_silent_period(9600),
            19200 : minimalmodbus._calculate_minimum_silent_period(19200),
            38400 : minimalmodbus._calculate_minimum_silent_period(38400),
            57600 : minimalmodbus._calculate_minimum_silent_period(57600),
            115200 : minimalmodbus._calculate_minimum_silent_period(115200),
        }

        for bd, assumed_timeout in timeouts.items():
            self.assertEqual(self.scanner._get_arbitration_timeout(bd), assumed_timeout)

    async def test_correct_response(self):
        mocks = [
            ("fffffffffffffffffd6003fe34359603f6a80000000000000000", "FE34359603"),
            ("fffffffffffffffffd600300019424c7f4610000000000000000", "00019424C7"),
        ]
        for (mock, assumed_response) in mocks:
            self.mock_response(mock)
            ret = await self.scanner.get_next_device_data(cmd_code=self.scanner.CMDS.single_scan, uart_params=self.uart_params)
            ret = minimalmodbus._hexencode(ret)
            self.assertEqual(assumed_response, ret)

    async def test_correct_scan_end(self):
        operation_code = "04"
        self.mock_response(
            "fffffffd60" + operation_code + "c9f3000000"
        )
        ret = await self.scanner.get_next_device_data(cmd_code=self.scanner.CMDS.single_scan, uart_params=self.uart_params)
        self.assertIsNone(ret)

    async def test_unsupported_operation_code(self):
        unsupported_code = "11"
        self.mock_response(
            "fffffffffffffffffd60" + unsupported_code + "fe34359603f6a80000000000000000"
        )
        with self.assertRaises(minimalmodbus.InvalidResponseError):
            await self.scanner.get_next_device_data(self.scanner.CMDS.single_scan, uart_params=self.uart_params)

    async def test_empty_answer(self):
        self.mock_response("")
        with self.assertRaises(minimalmodbus.InvalidResponseError):
            await self.scanner.get_next_device_data(self.scanner.CMDS.single_scan, uart_params=self.uart_params)

    async def test_incorrect_response_extraction(self):
        self.mock_response("0d6003c9f3000000")
        with self.assertRaises(minimalmodbus.InvalidResponseError):
            await self.scanner.get_next_device_data(self.scanner.CMDS.single_scan, uart_params=self.uart_params)

    async def test_corrupted_answer(self):
        incorrect_crc = "ffff"
        self.mock_response(
            "fffffffffd6003fe34359603" + incorrect_crc + "00000000"
        )
        with self.assertRaises(minimalmodbus.InvalidResponseError):
            await self.scanner.get_next_device_data(self.scanner.CMDS.single_scan, uart_params=self.uart_params)
