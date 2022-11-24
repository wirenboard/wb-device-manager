#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from unittest.mock import MagicMock

from wb.device_manager import serial_bus
from wb_modbus import minimalmodbus


class DummyScanner(serial_bus.WBExtendedModbusScanner):

    def __init__(self, *args, **kwargs):
        self.instrument = MagicMock()
        self.port = kwargs.get("port", "/dev/dummyport")


class TestMBExtendedScanner(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.scanner = DummyScanner(port="/dev/dummyport")

    @classmethod
    def mock_response(cls, response_hex_str):
        ret = minimalmodbus.hexdecode(response_hex_str)
        cls.scanner.instrument._communicate = MagicMock(return_value=ret)  # we have no actual serial devices

    def test_correct_response(self):
        assumed_response = "FE34359603"
        self.mock_response(
            "fffffffffffffffffd6003fe34359603f6a80000000000000000"
        )
        ret = self.scanner.get_next_device_data()
        ret = minimalmodbus.hexencode(ret)
        self.assertEqual(assumed_response, ret)

    def test_correct_scan_init(self):
        self.mock_response("")  # No answer to broadcast scan-init cmd
        self.scanner.init_bus_scan()

    def test_correct_scan_end(self):
        operation_code = "04"
        self.mock_response(
            "fffffffd60" + operation_code + "c9f3000000"
        )
        ret = self.scanner.get_next_device_data()
        self.assertIsNone(ret)

    def test_unsupported_operation_code(self):
        unsupported_code = "11"
        self.mock_response(
            "fffffffffffffffffd60" + unsupported_code + "fe34359603f6a80000000000000000"
        )
        self.assertRaises(
            minimalmodbus.InvalidResponseError,
            lambda: self.scanner.get_next_device_data()
        )

    def test_empty_answer(self):
        self.mock_response("")
        self.assertRaises(  # TODO: empty rpc-answer means no support in wb-mqtt-serial; maybe custom error?
            minimalmodbus.InvalidResponseError,
            lambda: self.scanner.get_next_device_data()
        )

    def test_corrupted_answer(self):
        incorrect_crc = "ffff"
        self.mock_response(
            "fffffffffd6003fe34359603" + incorrect_crc + "00000000"
        )
        self.assertRaises(
            minimalmodbus.InvalidResponseError,
            lambda: self.scanner.get_next_device_data()
        )
