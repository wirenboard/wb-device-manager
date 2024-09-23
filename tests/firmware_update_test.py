#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from wb.device_manager.firmware_update import DeviceUpdateInfo, Port
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


class DeviceUpdateInfoTest(unittest.TestCase):
    def test_eq(self):
        d1 = DeviceUpdateInfo(port=Port("test"), slave_id=1)
        d2 = DeviceUpdateInfo(port=Port("test"), slave_id=1)
        d3 = DeviceUpdateInfo(port=Port("test"), slave_id=2)
        d4 = DeviceUpdateInfo(port=Port("test1"), slave_id=1)
        self.assertEqual(d1, d2)
        self.assertNotEqual(d1, d3)
        self.assertNotEqual(d1, d4)
