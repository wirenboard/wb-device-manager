#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import signal
import unittest
from unittest.mock import MagicMock

from wb.device_manager import mqtt_rpc


def make_server(event_loop=None):
    event_loop = event_loop or MagicMock()
    dispatcher = MagicMock()
    dispatcher.keys.return_value = []
    mqtt_connection = MagicMock()
    bus_scanner = MagicMock()
    fw_updater = MagicMock()
    server = mqtt_rpc.AsyncMQTTServer(
        methods_dispatcher=dispatcher,
        mqtt_connection=mqtt_connection,
        mqtt_url_str="tcp://localhost:1883",
        rpc_client=MagicMock(),
        bus_scanner=bus_scanner,
        fw_updater=fw_updater,
        asyncio_loop=event_loop,
    )
    return server, mqtt_connection, bus_scanner, fw_updater


class TestAsyncMQTTServerLifecycle(unittest.TestCase):
    def test_signal_stops_service_with_not_running_code(self):
        event_loop = MagicMock()
        server, _, _, _ = make_server(event_loop)

        server._setup_event_loop()  # pylint: disable=protected-access
        handlers = {call.args[0]: call.args[1] for call in event_loop.add_signal_handler.call_args_list}
        handlers[signal.SIGTERM]()

        self.assertEqual(server.run(), mqtt_rpc.EXIT_NOTRUNNING)
        event_loop.stop.assert_called_once_with()

    def test_authentication_error_stops_loop_threadsafe(self):
        event_loop = MagicMock()
        server, _, _, _ = make_server(event_loop)

        server._on_mqtt_connect(None, None, None, 5)  # pylint: disable=protected-access

        self.assertEqual(server.run(), mqtt_rpc.EXIT_INVALIDARGUMENT)
        event_loop.call_soon_threadsafe.assert_called_once_with(event_loop.stop)

    def test_other_connection_error_keeps_service_running(self):
        event_loop = MagicMock()
        server, _, _, _ = make_server(event_loop)

        server._on_mqtt_connect(None, None, None, 3)  # pylint: disable=protected-access

        self.assertEqual(server.run(), mqtt_rpc.EXIT_NOTRUNNING)
        event_loop.call_soon_threadsafe.assert_not_called()

    def test_disconnected_shutdown_skips_retained_cleanup(self):
        server, mqtt_connection, bus_scanner, fw_updater = make_server()
        server._mqtt_started = True  # pylint: disable=protected-access
        mqtt_connection.is_connected.return_value = False

        with self.assertLogs("wb.device_manager", level="ERROR") as logs:
            server._close_mqtt_connection()  # pylint: disable=protected-access

        bus_scanner.clear_state.assert_not_called()
        fw_updater.clear_state.assert_not_called()
        mqtt_connection.stop.assert_called_once_with()
        self.assertIn("broker is unavailable", " ".join(logs.output))

    def test_close_cancels_pending_asyncio_tasks(self):
        event_loop = asyncio.new_event_loop()
        try:
            server, _, _, _ = make_server(event_loop)
            task = event_loop.create_task(asyncio.Event().wait())

            server.close()

            self.assertTrue(task.cancelled())
        finally:
            event_loop.close()


if __name__ == "__main__":
    unittest.main()
