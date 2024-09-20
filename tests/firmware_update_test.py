#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from unittest.mock import AsyncMock

from jsonrpc.exceptions import JSONRPCDispatchException
from mqttrpc import client as rpcclient

from wb.device_manager.firmware_update import FirmwareInfo, FirmwareUpdater
from wb.device_manager.fw_downloader import ReleasedFirmware
from wb.device_manager.mqtt_rpc import MQTTRPCErrorCode
from wb.device_manager.serial_rpc import WBModbusException


class TestGetFirmwareInfo(unittest.IsolatedAsyncioTestCase):

    async def test_modbus_exception(self):
        reader_mock = AsyncMock()
        reader_mock.read = AsyncMock()
        reader_mock.read.side_effect = WBModbusException("test msg", 1)
        updater = FirmwareUpdater(None, None, None, reader_mock)
        with self.assertRaises(JSONRPCDispatchException) as cm:
            await updater.get_firmware_info(slave_id=1, port={"path": "test"})

        self.assertEqual(cm.exception.error.code, MQTTRPCErrorCode.REQUEST_HANDLING_ERROR.value)
        self.assertEqual(cm.exception.error.message, "test msg")
        self.assertEqual(cm.exception.error.data, 1)

    async def test_rpc_timeout_exception(self):
        reader_mock = AsyncMock()
        reader_mock.read = AsyncMock()
        reader_mock.read.side_effect = rpcclient.MQTTRPCError(
            "test msg", MQTTRPCErrorCode.REQUEST_TIMEOUT_ERROR.value, "test data"
        )
        updater = FirmwareUpdater(None, None, None, reader_mock)
        with self.assertRaises(JSONRPCDispatchException) as cm:
            await updater.get_firmware_info(slave_id=1, port={"path": "test"})

        self.assertEqual(cm.exception.error.code, MQTTRPCErrorCode.REQUEST_TIMEOUT_ERROR.value)
        self.assertEqual(cm.exception.error.message, "test msg")
        self.assertEqual(cm.exception.error.data, "test data")

    async def test_generic_rpc_exception(self):
        reader_mock = AsyncMock()
        reader_mock.read = AsyncMock()
        reader_mock.read.side_effect = rpcclient.MQTTRPCError(
            "test msg", MQTTRPCErrorCode.REQUEST_HANDLING_ERROR.value, "test data"
        )
        updater = FirmwareUpdater(None, None, None, reader_mock)
        with self.assertRaises(rpcclient.MQTTRPCError) as cm:
            await updater.get_firmware_info(slave_id=1, port={"path": "test"})

        self.assertEqual(cm.exception, reader_mock.read.side_effect)

    async def test_successful_read(self):
        reader_mock = AsyncMock()
        reader_mock.read = AsyncMock()
        reader_mock.read.return_value = FirmwareInfo("1", ReleasedFirmware("2", "endpoint"), "sig", True)
        updater = FirmwareUpdater(None, None, None, reader_mock)
        res = await updater.get_firmware_info(slave_id=1, port={"path": "test"})

        self.assertEqual(res.get("fw"), "1")
        self.assertEqual(res.get("available_fw"), "2")
        self.assertEqual(res.get("can_update"), True)
