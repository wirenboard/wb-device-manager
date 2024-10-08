#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from unittest.mock import AsyncMock

from wb_modbus import minimalmodbus

from wb.device_manager import serial_bus


class DummyWBAsyncModbus(serial_bus.WBAsyncModbus):
    def __init__(self, *args, **kwargs):
        self.device = AsyncMock()
        self.addr = kwargs.get("addr", 1)
        self.port = kwargs.get("port", "/dev/dummyport")
        self.uart_params = {"baudrate": 9600, "parity": "N", "stopbits": 2}


class DummyWBAsyncExtendedModbus(serial_bus.WBAsyncExtendedModbus):
    def __init__(self, *args, **kwargs):
        self.device = AsyncMock()
        self.port = kwargs.get("port", "/dev/dummyport")
        self.serial_number = kwargs.get("sn", 4267654341)
        self.extended_modbus_wrapper = serial_bus.WBExtendedModbusWrapper()
        self.addr = 0
        self.uart_params = {"baudrate": 9600, "parity": "N", "stopbits": 2}


class TestMBExtendedScanner(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.scanner = serial_bus.WBExtendedModbusScanner(port="/dev/ttyDUMMY", rpc_client=AsyncMock())
        cls.uart_params = {"baudrate": 9600, "parity": "N", "stopbits": 1}

    @classmethod
    def mock_response(cls, response_hex_str):
        ret = minimalmodbus._hexdecode(response_hex_str)
        cls.scanner.instrument._communicate = AsyncMock(return_value=ret)  # we have no actual serial devices

    def test_arbitration_timeout_calculation(self):
        bds = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]
        timeouts = {bd: minimalmodbus._calculate_minimum_silent_period(bd) for bd in bds}

        for bd, assumed_timeout in timeouts.items():
            self.assertEqual(self.scanner._get_arbitration_timeout(bd), assumed_timeout)

    async def test_correct_response(self):
        mocks = [
            ("fffffffffffffffffd6003fe34359603f6a80000000000000000", "FE34359603"),
            ("fffffffffffffffffd600300019424c7f4610000000000000000", "00019424C7"),
            ("fffffffffffffffffd6003fed2efd601501a0000000000000000", "FED2EFD601"),
            ("fffffffffffffffffd60033938169d38f7000000000000000000", "3938169D38"),  # crc ends with zeroes
        ]
        for mock, assumed_response in mocks:
            self.mock_response(mock)
            ret = await self.scanner.get_next_device_data(
                cmd_code=self.scanner.extended_modbus_wrapper.CMDS.single_scan, uart_params=self.uart_params
            )
            ret = minimalmodbus._hexencode(ret)
            self.assertEqual(assumed_response, ret)

    async def test_correct_scan_end(self):
        operation_code = "04"
        self.mock_response("fffffffd60" + operation_code + "c9f3000000")
        ret = await self.scanner.get_next_device_data(
            cmd_code=self.scanner.extended_modbus_wrapper.CMDS.single_scan, uart_params=self.uart_params
        )
        self.assertIsNone(ret)

    async def test_unsupported_operation_code(self):
        unsupported_code = "11"
        self.mock_response("fffffffffffffffffd60" + unsupported_code + "fe34359603f6a80000000000000000")
        with self.assertRaises(minimalmodbus.InvalidResponseError):
            await self.scanner.get_next_device_data(
                self.scanner.extended_modbus_wrapper.CMDS.single_scan, uart_params=self.uart_params
            )

    async def test_empty_answer(self):
        self.mock_response("")
        with self.assertRaises(minimalmodbus.InvalidResponseError):
            await self.scanner.get_next_device_data(
                self.scanner.extended_modbus_wrapper.CMDS.single_scan, uart_params=self.uart_params
            )

    async def test_incorrect_response_extraction(self):
        self.mock_response("0d6003c9f3000000")
        with self.assertRaises(minimalmodbus.InvalidResponseError):
            await self.scanner.get_next_device_data(
                self.scanner.extended_modbus_wrapper.CMDS.single_scan, uart_params=self.uart_params
            )

    async def test_corrupted_answer(self):
        incorrect_crc = "ffff"
        self.mock_response("fffffffffd6003fe34359603" + incorrect_crc + "00000000")
        with self.assertRaises(minimalmodbus.InvalidResponseError):
            await self.scanner.get_next_device_data(
                self.scanner.extended_modbus_wrapper.CMDS.single_scan, uart_params=self.uart_params
            )


class AsyncModbusTestBase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.mb_connection = AsyncMock()

    @classmethod
    def mock_response(cls, response_hex_str):
        ret = minimalmodbus._hexdecode(response_hex_str)
        cls.mb_connection.device._communicate = AsyncMock(
            return_value=ret
        )  # we have no actual serial devices

    async def _test_no_answer(self):
        self.mock_response("")
        with self.assertRaises(minimalmodbus.InvalidResponseError):
            await self.mb_connection.read_string(first_addr=200, regs_length=6)

    async def _test_read_string(self, mock, string):
        self.mock_response(mock)
        ret = await self.mb_connection.read_string(first_addr=200, regs_length=6)
        self.assertEqual(ret, string)

    async def _test_read_u16_regs(self, mock, vals):
        self.mock_response(mock)
        ret = await self.mb_connection.read_u16_regs(first_addr=200, regs_length=6)
        self.assertListEqual(ret, vals)

    async def _test_corrupted_answer(self, mock_without_crc):
        incorrect_crc = "0000"
        self.mock_response(mock_without_crc + incorrect_crc)
        with self.assertRaises(minimalmodbus.InvalidResponseError):
            await self.mb_connection.read_string(first_addr=200, regs_length=6)

    async def _test_slave_reported(self, illegal_data_address_mock):
        self.mock_response(illegal_data_address_mock)
        with self.assertRaises(minimalmodbus.IllegalRequestError):
            await self.mb_connection.read_string(first_addr=200, regs_length=6)


class TestWBAsyncModbus(AsyncModbusTestBase):
    @classmethod
    def setUpClass(cls):
        cls.mb_connection = DummyWBAsyncModbus(addr=11)

    async def test_no_answer(self):
        await self._test_no_answer()

    async def test_read_string(self):
        await self._test_read_string(mock="0b030c00570042004d0041004f0034bbaa", string="WBMAO4")

    async def test_read_u16_regs(self):
        await self._test_read_u16_regs(
            mock="0b030c00570042004d0041004f0034bbaa", vals=[87, 66, 77, 65, 79, 52]
        )

    async def test_corrupted_answer(self):
        await self._test_corrupted_answer(mock_without_crc="0b030c00570042004d0041004f0034")

    async def test_slave_reported(self):
        await self._test_slave_reported(illegal_data_address_mock="0b8302e0f3")


class TestWBAsyncExtendedModbus(AsyncModbusTestBase):
    @classmethod
    def setUpClass(cls):
        cls.mb_connection = DummyWBAsyncExtendedModbus(sn=4266178293)

    async def test_no_answer(self):
        await self._test_no_answer()

    async def test_read_string(self):
        await self._test_read_string(mock="fd6009fe48b6f5030c00570042004d0041004f0034e141", string="WBMAO4")

    async def test_read_u16_regs(self):
        await self._test_read_u16_regs(
            mock="fd6009fe48b6f5030c00570042004d0041004f0034e141", vals=[87, 66, 77, 65, 79, 52]
        )

    async def test_corrupted_answer(self):
        await self._test_corrupted_answer(mock_without_crc="fd6009fe48b6f5030c00570042004d0041004f0034")

    async def test_slave_reported(self):
        await self._test_slave_reported(illegal_data_address_mock="fd6009fe48b6f58302ea17")


class TestFixSn(unittest.TestCase):
    def test_fix_sn(self):
        sn = 0xFE123456
        self.assertEqual(serial_bus.fix_sn("WB-MAP12E", sn), 0x00123456)
        self.assertEqual(serial_bus.fix_sn("WB-MR6C", sn), sn)
