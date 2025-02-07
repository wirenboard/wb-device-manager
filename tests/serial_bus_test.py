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


class TestFixSn(unittest.TestCase):
    def test_fix_sn(self):
        sn = 0xFE123456
        self.assertEqual(serial_bus.fix_sn("WB-MAP12E", sn), 0x00123456)
        self.assertEqual(serial_bus.fix_sn("WB-MR6C", sn), sn)
