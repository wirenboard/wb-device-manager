#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import functools
import logging
import signal
import unittest
from unittest.mock import AsyncMock, MagicMock

from mqttrpc import Dispatcher

from wb.device_manager import mqtt_rpc


class TestAsyncMQTTServerLifecycle(unittest.TestCase):
    """
    Connection handling and shutdown of AsyncMQTTServer with a mocked MQTT client.
    """

    def setUp(self):
        self.mqtt_connection = MagicMock()
        self.bus_scanner = MagicMock()
        self.fw_updater = MagicMock()
        self.signal_handlers = {}
        asyncio_loop = asyncio.new_event_loop()
        asyncio_loop.add_signal_handler = self._record_signal_handler
        self.server = mqtt_rpc.AsyncMQTTServer(
            methods_dispatcher=Dispatcher(
                {("bus-scan", "Start"): AsyncMock(), ("fw-update", "Update"): AsyncMock()}
            ),
            mqtt_connection=self.mqtt_connection,
            mqtt_url_str="unix:///var/run/mosquitto/mosquitto.sock",
            rpc_client=MagicMock(),
            bus_scanner=self.bus_scanner,
            fw_updater=self.fw_updater,
            asyncio_loop=asyncio_loop,
        )
        self.server.setup()

    def tearDown(self):
        self.server.asyncio_loop.close()

    def _record_signal_handler(self, sig, callback, *args):
        """
        Stand-in for loop.add_signal_handler: the handler is only recorded, so no real SIGINT/SIGTERM
        handler is installed in the pytest process.
        """
        self.signal_handlers[sig] = functools.partial(callback, *args)

    def test_setup_lets_paho_retry_an_unavailable_broker(self):
        self.mqtt_connection.start.assert_called_once_with(retry_first_connection=True)

    def test_connect_publishes_state_and_subscribes(self):
        self.server._on_mqtt_connect(None, None, None, 0)  # pylint: disable=protected-access

        self.bus_scanner.publish_state.assert_called_once_with()
        self.mqtt_connection.subscribe.assert_any_call("/rpc/v1/wb-device-manager/bus-scan/Start/+")
        self.mqtt_connection.publish.assert_any_call(
            "/rpc/v1/wb-device-manager/bus-scan/Start", "1", retain=True, qos=1
        )

    def test_rejected_login_stops_with_exit_code_2(self):
        """
        CONNACK 5 arrives on paho's thread; the loop is stopped through call_soon_threadsafe
        and run() returns 2 without removing topics that were never published.
        """
        self.mqtt_connection.is_connected.return_value = False

        self.server._on_mqtt_connect(None, None, None, 5)  # pylint: disable=protected-access

        with self.assertLogs(mqtt_rpc.logger, level=logging.ERROR) as logs:
            self.assertEqual(self.server.run(), mqtt_rpc.EXIT_INVALIDARGUMENT)
        self.assertIn("retained topics cannot be removed", "".join(logs.output))
        self.mqtt_connection.publish.assert_not_called()
        self.mqtt_connection.stop.assert_called_once_with()

    def test_other_connect_failures_keep_running(self):
        self.server._on_mqtt_connect(None, None, None, 1)  # pylint: disable=protected-access

        self.mqtt_connection.publish.assert_not_called()
        self.assertFalse(self.server.asyncio_loop.is_closed())

    def test_stop_cancels_tasks_and_removes_retained_topics(self):
        """
        A signal stops the loop; run() then cancels the pending tasks, clears the states and the
        RPC topics while still connected, stops the client and returns 0.
        """
        self.mqtt_connection.is_connected.return_value = True
        pending = self.server.asyncio_loop.create_task(asyncio.sleep(3600))
        self.server.asyncio_loop.call_soon(self.signal_handlers[signal.SIGTERM])

        self.assertEqual(self.server.run(), mqtt_rpc.EXIT_SUCCESS)

        self.assertTrue(pending.cancelled())
        self.bus_scanner.clear_state.assert_called_once_with()
        self.fw_updater.clear_state.assert_called_once_with()
        self.mqtt_connection.publish.assert_any_call(
            "/rpc/v1/wb-device-manager/bus-scan/Start", payload=None, retain=True, qos=1
        )
        self.mqtt_connection.publish.assert_any_call(
            "/rpc/v1/wb-device-manager/fw-update/Update", payload=None, retain=True, qos=1
        )
        # the states' and the RPC topics' clears are confirmed within the shared deadline
        waits = self.mqtt_connection.publish.return_value.wait_for_publish.call_args_list
        self.assertEqual(len(waits), 2)
        for receipt in (self.bus_scanner.clear_state.return_value, self.fw_updater.clear_state.return_value):
            receipt.wait_for_publish.assert_called_once()
        self.assertTrue(all(0 < wait.args[0] <= mqtt_rpc.CLEAR_RETAINED_TIMEOUT_S for wait in waits))
        self.mqtt_connection.stop.assert_called_once_with()

    def test_unconfirmed_removal_is_logged_not_raised(self):
        """
        paho raises when the client lost the broker meanwhile; a receipt that never confirms
        runs into the deadline. Both end with one error line and a stopped client.
        """
        self.mqtt_connection.is_connected.return_value = True
        self.bus_scanner.clear_state.return_value.wait_for_publish.side_effect = RuntimeError("not connected")
        self.server.asyncio_loop.call_soon(self.server.asyncio_loop.stop)

        with self.assertLogs(mqtt_rpc.logger, level=logging.ERROR) as logs:
            self.assertEqual(self.server.run(), mqtt_rpc.EXIT_SUCCESS)
        self.assertIn("not confirmed: not connected", "".join(logs.output))
        self.mqtt_connection.stop.assert_called_once_with()

        self.bus_scanner.clear_state.return_value.wait_for_publish.side_effect = None
        self.fw_updater.clear_state.return_value.is_published.return_value = False
        with self.assertLogs(mqtt_rpc.logger, level=logging.ERROR) as logs:
            mqtt_rpc.AsyncMQTTServer._wait_published(  # pylint: disable=protected-access
                [self.bus_scanner.clear_state(), self.fw_updater.clear_state()]
            )
        self.assertIn("not confirmed within 5 s", "".join(logs.output))
