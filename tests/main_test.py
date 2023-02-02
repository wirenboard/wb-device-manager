#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from unittest.mock import AsyncMock
import uuid

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
        self.assertListEqual(list(ret), assumed_response)


class TestExternalDeviceErrors(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.device_manager = DummyDeviceManager()

        cls.device_info = main.DeviceInfo(
            uuid=uuid.uuid3(namespace=uuid.NAMESPACE_OID, name="dummy_device"),
            port=main.Port(
                path="/dev/ttyDUMMY"
            ),
            sn=12345,
            cfg=main.SerialParams(
                slave_id=1
            )
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
        ]
        ret = await self.device_manager.fill_device_info(self.device_info, self.mb_conn)
        self.assertListEqual(ret, assumed_errors)
        self.assertIsInstance(, cls)

    async def test_erroneous_fill_fw_info(self):
        self.mock_error()
        assumed_errors = [
            main.ReadDeviceSignatureDeviceError(),
            main.ReadFWSignatureDeviceError(),
        ]
        ret = await self.device_manager.fill_device_info(self.device_info, self.mb_conn)
        self.assertListEqual(ret, assumed_errors)
