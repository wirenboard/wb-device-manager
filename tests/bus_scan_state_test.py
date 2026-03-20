#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import unittest

from wb.device_manager.bus_scan_state import (
    BusScanState,
    BusScanStateManager,
    DeviceInfo,
    Firmware,
    ParsedPorts,
    Port,
    ProgressMeter,
    SerialParams,
    SetEncoder,
    make_uuid,
)
from wb.device_manager.serial_rpc import SerialConfig, TcpConfig


class TestPort(unittest.TestCase):
    def test_from_serial_config(self):
        cfg = SerialConfig(path="/dev/ttyRS485-1")
        port = Port(cfg)
        self.assertEqual(port.path, "/dev/ttyRS485-1")

    def test_from_tcp_config(self):
        cfg = TcpConfig(address="192.168.0.7", port=23)
        port = Port(cfg)
        self.assertEqual(port.path, "192.168.0.7:23")

    def test_from_string(self):
        port = Port("/dev/ttyRS485-2")
        self.assertEqual(port.path, "/dev/ttyRS485-2")


class TestProgressMeter(unittest.TestCase):
    def test_basic_progress(self):
        pm = ProgressMeter()
        pm.reset(4)
        self.assertEqual(pm.increment(), 25)
        self.assertEqual(pm.increment(), 50)
        self.assertEqual(pm.increment(), 75)
        self.assertEqual(pm.increment(), 100)

    def test_progress_caps_at_100(self):
        pm = ProgressMeter()
        pm.reset(1)
        self.assertEqual(pm.increment(), 100)
        self.assertEqual(pm.increment(), 100)

    def test_progress_zero_total(self):
        pm = ProgressMeter()
        self.assertEqual(pm.increment(), 0)

    def test_reset_raises_on_zero(self):
        pm = ProgressMeter()
        with self.assertRaises(ValueError):
            pm.reset(0)

    def test_reset_raises_on_negative(self):
        pm = ProgressMeter()
        with self.assertRaises(ValueError):
            pm.reset(-1)

    def test_progress_rounding(self):
        pm = ProgressMeter()
        pm.reset(3)
        self.assertEqual(pm.increment(), 34)  # ceil(1/3*100)
        self.assertEqual(pm.increment(), 67)  # ceil(2/3*100)
        self.assertEqual(pm.increment(), 100)


class TestBusScanState(unittest.TestCase):
    def test_default_values(self):
        state = BusScanState()
        self.assertEqual(state.progress, 0)
        self.assertFalse(state.scanning)
        self.assertEqual(state.scanning_ports, [])
        self.assertFalse(state.is_ext_scan)
        self.assertIsNone(state.error)
        self.assertEqual(state.devices, [])

    def test_update(self):
        state = BusScanState()
        state.update({"scanning": True, "progress": 50})
        self.assertTrue(state.scanning)
        self.assertEqual(state.progress, 50)

    def test_update_ignores_unknown_keys(self):
        state = BusScanState()
        state.update({"nonexistent_key": "value"})
        self.assertEqual(state.progress, 0)


class TestDeviceInfo(unittest.TestCase):
    def test_hash_and_equality(self):
        port = Port("/dev/ttyRS485-1")
        cfg = SerialParams(slave_id=1)
        d1 = DeviceInfo(uuid="abc-123", port=port, cfg=cfg)
        d2 = DeviceInfo(uuid="abc-123", port=port, cfg=cfg)
        self.assertEqual(d1, d2)
        self.assertEqual(hash(d1), hash(d2))

    def test_inequality(self):
        cfg = SerialParams(slave_id=1)
        d1 = DeviceInfo(uuid="abc-123", port=Port("/dev/ttyRS485-1"), cfg=cfg)
        d2 = DeviceInfo(uuid="xyz-789", port=Port("/dev/ttyRS485-1"), cfg=cfg)
        self.assertNotEqual(d1, d2)

    def test_firmware_defaults(self):
        fw = Firmware()
        self.assertIsNone(fw.version)
        self.assertFalse(fw.ext_support)
        self.assertIsNone(fw.fast_modbus_command)


class TestSetEncoder(unittest.TestCase):
    def test_encodes_set(self):
        data = {"items": {1, 2, 3}}
        result = json.loads(json.dumps(data, cls=SetEncoder))
        self.assertIsInstance(result["items"], list)
        self.assertEqual(sorted(result["items"]), [1, 2, 3])

    def test_encodes_dataclass(self):
        fw = Firmware(version="1.2.3", ext_support=True)
        result = json.loads(json.dumps(fw, cls=SetEncoder))
        self.assertEqual(result["version"], "1.2.3")
        self.assertTrue(result["ext_support"])

    def test_raises_on_unknown_type(self):
        with self.assertRaises(TypeError):
            json.dumps(object(), cls=SetEncoder)


class TestMakeUuid(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(make_uuid("SN123"), make_uuid("SN123"))

    def test_different_sns(self):
        self.assertNotEqual(make_uuid("SN123"), make_uuid("SN456"))


class TestParsedPorts(unittest.TestCase):
    def test_defaults(self):
        pp = ParsedPorts()
        self.assertEqual(pp.serial, [])
        self.assertEqual(pp.tcp, [])
        self.assertEqual(pp.modbus_tcp, [])


class TestStateJson(unittest.TestCase):
    def test_serializes_bus_scan_state(self):
        result = BusScanStateManager.state_json(BusScanState())
        parsed = json.loads(result)
        self.assertEqual(parsed["progress"], 0)
        self.assertFalse(parsed["scanning"])
        self.assertEqual(parsed["devices"], [])
