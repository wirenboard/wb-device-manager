#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from unittest.mock import AsyncMock, Mock, patch

from jsonrpc.exceptions import JSONRPCDispatchException
from mqttrpc import client as rpcclient

from wb.device_manager.fw_update_proxy import FirmwareUpdateProxy


class TestFirmwareUpdateProxy(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rpc_client = AsyncMock()
        self.mqtt_connection = AsyncMock()
        self.proxy = FirmwareUpdateProxy(self.rpc_client, self.mqtt_connection)

    async def test_get_firmware_info_forwards_call(self):
        expected = {"fw": "1.0", "available_fw": "2.0", "can_update": True}
        self.rpc_client.make_rpc_call = AsyncMock(return_value=expected)

        result = await self.proxy.get_firmware_info(slave_id=1, port={"path": "/dev/ttyRS485-1"})

        self.assertEqual(result, expected)
        self.rpc_client.make_rpc_call.assert_called_once_with(
            driver="wb-mqtt-serial",
            service="fw-update",
            method="GetFirmwareInfo",
            params={"slave_id": 1, "port": {"path": "/dev/ttyRS485-1"}},
            timeout=30,
        )

    async def test_update_forwards_call(self):
        self.rpc_client.make_rpc_call = AsyncMock(return_value="Ok")

        result = await self.proxy.update_software(
            slave_id=1, port={"path": "/dev/ttyRS485-1"}, type="firmware"
        )

        self.assertEqual(result, "Ok")
        self.rpc_client.make_rpc_call.assert_called_once_with(
            driver="wb-mqtt-serial",
            service="fw-update",
            method="Update",
            params={"slave_id": 1, "port": {"path": "/dev/ttyRS485-1"}, "type": "firmware"},
            timeout=30,
        )

    async def test_clear_error_forwards_call(self):
        self.rpc_client.make_rpc_call = AsyncMock(return_value="Ok")

        result = await self.proxy.clear_error(slave_id=1, port={"path": "/dev/ttyRS485-1"}, type="firmware")

        self.assertEqual(result, "Ok")
        self.rpc_client.make_rpc_call.assert_called_once_with(
            driver="wb-mqtt-serial",
            service="fw-update",
            method="ClearError",
            params={"slave_id": 1, "port": {"path": "/dev/ttyRS485-1"}, "type": "firmware"},
            timeout=30,
        )

    async def test_restore_forwards_call(self):
        self.rpc_client.make_rpc_call = AsyncMock(return_value="Ok")

        result = await self.proxy.restore_firmware(slave_id=1, port={"path": "/dev/ttyRS485-1"})

        self.assertEqual(result, "Ok")
        self.rpc_client.make_rpc_call.assert_called_once_with(
            driver="wb-mqtt-serial",
            service="fw-update",
            method="Restore",
            params={"slave_id": 1, "port": {"path": "/dev/ttyRS485-1"}},
            timeout=30,
        )

    async def test_rpc_error_converted_to_jsonrpc_exception(self):
        self.rpc_client.make_rpc_call = AsyncMock(
            side_effect=rpcclient.MQTTRPCError("device timeout", -32600, "extra data")
        )

        with self.assertRaises(JSONRPCDispatchException) as cm:
            await self.proxy.get_firmware_info(slave_id=1, port={"path": "/dev/ttyRS485-1"})

        self.assertEqual(cm.exception.error.code, -32600)
        self.assertEqual(cm.exception.error.message, "device timeout")

    async def test_deprecation_warning_logged(self):
        self.rpc_client.make_rpc_call = AsyncMock(return_value="Ok")

        with patch("wb.device_manager.fw_update_proxy.logger") as mock_logger:
            await self.proxy.clear_error(slave_id=1, port={"path": "/dev/ttyRS485-1"})
            mock_logger.warning.assert_called_once()
            # The warning uses format string with %s, check args contain the deprecation message
            warning_args = mock_logger.warning.call_args[0]
            formatted = warning_args[0] % warning_args[1:]
            self.assertIn("deprecated", formatted.lower())

    def test_clear_state_removes_old_topic(self):
        mock_msg_info = Mock()
        self.mqtt_connection.publish = Mock(return_value=mock_msg_info)

        self.proxy.clear_state()

        self.mqtt_connection.publish.assert_called_once_with(
            "/wb-device-manager/firmware_update/state", payload=None, retain=True, qos=1
        )
        mock_msg_info.wait_for_publish.assert_called_once()

    def test_start_is_noop(self):
        self.proxy.start()  # Should not raise

    def test_publish_state_is_noop(self):
        self.proxy.publish_state()  # Should not raise
