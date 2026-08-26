#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from wb.device_manager.bus_scan_state import Port
from wb.device_manager.serial_rpc import SerialConfig, TcpConfig


class PortTest(unittest.TestCase):
    def test_port_init_str(self):
        port = Port("test")
        self.assertEqual(port.path, "test")

    def test_port_init_serial_config(self):
        config = SerialConfig(path="test")
        port = Port(config)
        self.assertEqual(port.path, "test")

    def test_port_init_tcp_config(self):
        config = TcpConfig(address="1.1.1.1", port=12345)
        port = Port(config)
        self.assertEqual(port.path, "1.1.1.1:12345")
