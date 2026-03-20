#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from unittest.mock import AsyncMock

from wb.device_manager.serial_device import (
    SerialDevice,
    TcpDevice,
    create_device,
    create_device_from_json,
)
from wb.device_manager.serial_rpc import ModbusProtocol, SerialConfig, TcpConfig, WB_DEVICE_PARAMETERS


class TestCreateDevice(unittest.TestCase):
    def test_create_serial_device(self):
        rpc = AsyncMock()
        cfg = SerialConfig(path="/dev/ttyRS485-1")
        dev = create_device(cfg, ModbusProtocol.MODBUS_RTU, 1, rpc)
        self.assertIsInstance(dev, SerialDevice)
        self.assertEqual(dev.slave_id, 1)
        self.assertEqual(dev.protocol, ModbusProtocol.MODBUS_RTU)

    def test_create_tcp_device(self):
        rpc = AsyncMock()
        cfg = TcpConfig(address="192.168.0.1", port=502)
        dev = create_device(cfg, ModbusProtocol.MODBUS_TCP, 5, rpc)
        self.assertIsInstance(dev, TcpDevice)
        self.assertEqual(dev.slave_id, 5)
        self.assertEqual(dev.protocol, ModbusProtocol.MODBUS_TCP)


class TestCreateDeviceFromJson(unittest.TestCase):
    def test_serial_device_from_json(self):
        rpc = AsyncMock()
        data = {
            "slave_id": 10,
            "port": {"path": "/dev/ttyRS485-2"},
            "protocol": "modbus",
        }
        dev = create_device_from_json(data, rpc)
        self.assertIsInstance(dev, SerialDevice)
        self.assertEqual(dev.slave_id, 10)

    def test_tcp_device_from_json(self):
        rpc = AsyncMock()
        data = {
            "slave_id": 3,
            "port": {"address": "192.168.0.7", "port": 23},
            "protocol": "modbus",
        }
        dev = create_device_from_json(data, rpc)
        self.assertIsInstance(dev, TcpDevice)
        self.assertEqual(dev.slave_id, 3)

    def test_defaults_from_json(self):
        rpc = AsyncMock()
        data = {"port": {"path": "/dev/ttyRS485-1"}}
        dev = create_device_from_json(data, rpc)
        self.assertIsInstance(dev, SerialDevice)
        self.assertEqual(dev.slave_id, 0)
        self.assertEqual(dev.protocol, ModbusProtocol.MODBUS_RTU)


class TestDeviceDescription(unittest.TestCase):
    def test_serial_device_description(self):
        rpc = AsyncMock()
        cfg = SerialConfig(path="/dev/ttyRS485-1")
        dev = SerialDevice(cfg, ModbusProtocol.MODBUS_RTU, 42, rpc)
        self.assertIn("42", dev.description)

    def test_tcp_device_description(self):
        rpc = AsyncMock()
        cfg = TcpConfig(address="10.0.0.1", port=502)
        dev = TcpDevice(cfg, ModbusProtocol.MODBUS_TCP, 7, rpc)
        self.assertIn("7", dev.description)


class TestDevicePortConfig(unittest.TestCase):
    def test_serial_get_port_config(self):
        rpc = AsyncMock()
        cfg = SerialConfig(path="/dev/ttyRS485-1")
        dev = SerialDevice(cfg, ModbusProtocol.MODBUS_RTU, 1, rpc)
        self.assertIs(dev.get_port_config(), cfg)

    def test_tcp_get_port_config(self):
        rpc = AsyncMock()
        cfg = TcpConfig(address="10.0.0.1", port=502)
        dev = TcpDevice(cfg, ModbusProtocol.MODBUS_TCP, 1, rpc)
        self.assertIs(dev.get_port_config(), cfg)

    def test_serial_set_default_port_settings(self):
        rpc = AsyncMock()
        cfg = SerialConfig(path="/dev/ttyRS485-1", baud_rate=115200, parity="E")
        dev = SerialDevice(cfg, ModbusProtocol.MODBUS_RTU, 1, rpc)
        dev.set_default_port_settings()
        port_cfg = dev.get_port_config()
        self.assertEqual(port_cfg.baud_rate, 9600)
        self.assertEqual(port_cfg.parity, "N")

    def test_tcp_set_default_port_settings_is_noop(self):
        rpc = AsyncMock()
        cfg = TcpConfig(address="10.0.0.1", port=502)
        dev = TcpDevice(cfg, ModbusProtocol.MODBUS_TCP, 1, rpc)
        dev.set_default_port_settings()  # Should not raise


class TestCheckUpdatable(unittest.IsolatedAsyncioTestCase):
    async def test_serial_always_updatable(self):
        rpc = AsyncMock()
        cfg = SerialConfig(path="/dev/ttyRS485-1")
        dev = SerialDevice(cfg, ModbusProtocol.MODBUS_RTU, 1, rpc)
        self.assertTrue(await dev.check_updatable(False))
        self.assertTrue(await dev.check_updatable(True))

    async def test_tcp_updatable_with_preserve(self):
        rpc = AsyncMock()
        cfg = TcpConfig(address="10.0.0.1", port=502)
        dev = TcpDevice(cfg, ModbusProtocol.MODBUS_RTU, 1, rpc)
        self.assertTrue(await dev.check_updatable(bootloader_can_preserve_port_settings=True))

    async def test_tcp_updatable_modbus_tcp(self):
        rpc = AsyncMock()
        cfg = TcpConfig(address="10.0.0.1", port=502)
        dev = TcpDevice(cfg, ModbusProtocol.MODBUS_TCP, 1, rpc)
        self.assertTrue(await dev.check_updatable(bootloader_can_preserve_port_settings=False))


class TestSetPoll(unittest.IsolatedAsyncioTestCase):
    async def test_set_poll_delegates_to_rpc(self):
        rpc = AsyncMock()
        cfg = SerialConfig(path="/dev/ttyRS485-1")
        dev = SerialDevice(cfg, ModbusProtocol.MODBUS_RTU, 1, rpc)
        await dev.set_poll(True)
        rpc.set_poll.assert_called_once_with(cfg, 1, True)

    async def test_read_delegates_to_rpc(self):
        rpc = AsyncMock()
        rpc.read = AsyncMock(return_value=42)
        cfg = SerialConfig(path="/dev/ttyRS485-1")
        dev = SerialDevice(cfg, ModbusProtocol.MODBUS_RTU, 1, rpc)
        result = await dev.read(WB_DEVICE_PARAMETERS["baud_rate"])
        self.assertEqual(result, 42)

    async def test_write_delegates_to_rpc(self):
        rpc = AsyncMock()
        cfg = SerialConfig(path="/dev/ttyRS485-1")
        dev = SerialDevice(cfg, ModbusProtocol.MODBUS_RTU, 1, rpc)
        await dev.write(WB_DEVICE_PARAMETERS["baud_rate"], 9600)
        rpc.write.assert_called_once()
