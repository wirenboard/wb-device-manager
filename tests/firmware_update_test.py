#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import random
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from jsonrpc.exceptions import JSONRPCDispatchException
from mqttrpc import client as rpcclient

from wb.device_manager.bus_scan_state import Port
from wb.device_manager.firmware_update import (
    DeviceUpdateInfo,
    FirmwareInfo,
    FirmwareUpdater,
    SoftwareComponent,
    flash_fw,
    parse_wbfw,
    read_device_model,
    read_sn,
    reboot_to_bootloader,
    restore_firmware,
    update_software,
)
from wb.device_manager.fw_downloader import ReleasedBinary
from wb.device_manager.mqtt_rpc import MQTTRPCErrorCode
from wb.device_manager.serial_rpc import (
    WB_DEVICE_PARAMETERS,
    SerialConfig,
    SerialTimeoutException,
    TcpConfig,
    WBModbusException,
)


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
        d1 = DeviceUpdateInfo(port=Port("test"), slave_id=1, to_version="1")
        d2 = DeviceUpdateInfo(port=Port("test"), slave_id=1, to_version="1")
        d3 = DeviceUpdateInfo(port=Port("test"), slave_id=2, to_version="1")
        d4 = DeviceUpdateInfo(port=Port("test1"), slave_id=1, to_version="1")
        self.assertEqual(d1, d2)
        self.assertNotEqual(d1, d3)
        self.assertNotEqual(d1, d4)


class TestGetFirmwareInfo(unittest.IsolatedAsyncioTestCase):

    async def test_modbus_exception(self):
        reader_mock = AsyncMock()
        reader_mock.read = AsyncMock()
        reader_mock.read.side_effect = WBModbusException("test msg", 1)
        updater = FirmwareUpdater(AsyncMock(), None, None, reader_mock, None)
        with self.assertRaises(JSONRPCDispatchException) as cm:
            await updater.get_firmware_info(slave_id=1, port={"path": "test"})

        self.assertEqual(cm.exception.error.code, MQTTRPCErrorCode.REQUEST_HANDLING_ERROR.value)
        self.assertEqual(cm.exception.error.message, "test msg")
        self.assertEqual(cm.exception.error.data, None)

    async def test_rpc_timeout_exception(self):
        reader_mock = AsyncMock()
        reader_mock.read = AsyncMock()
        reader_mock.read.side_effect = SerialTimeoutException("test msg")
        updater = FirmwareUpdater(AsyncMock(), None, None, reader_mock, None)
        with self.assertRaises(JSONRPCDispatchException) as cm:
            await updater.get_firmware_info(slave_id=1, port={"path": "test"})

        self.assertEqual(cm.exception.error.code, MQTTRPCErrorCode.REQUEST_HANDLING_ERROR.value)
        self.assertEqual(cm.exception.error.message, "test msg")
        self.assertEqual(cm.exception.error.data, None)

    async def test_generic_rpc_exception(self):
        reader_mock = AsyncMock()
        reader_mock.read = AsyncMock()
        reader_mock.read.side_effect = rpcclient.MQTTRPCError(
            "test msg", MQTTRPCErrorCode.REQUEST_HANDLING_ERROR.value, "test data"
        )
        updater = FirmwareUpdater(AsyncMock(), None, None, reader_mock, None)
        with self.assertRaises(rpcclient.MQTTRPCError) as cm:
            await updater.get_firmware_info(slave_id=1, port={"path": "test"})

        self.assertEqual(cm.exception, reader_mock.read.side_effect)

    async def test_successful_read(self):
        reader_mock = AsyncMock()
        reader_mock.read = AsyncMock()
        serial_rpc = AsyncMock()
        serial_rpc.read = AsyncMock()
        serial_rpc.read.return_value = "MAP12\x02E"
        firmware_info = FirmwareInfo(
            current_version="1", available=ReleasedBinary("2", "endpoint"), signature="sig"
        )
        firmware_info.bootloader.can_preserve_port_settings = True
        reader_mock.read.return_value = firmware_info
        updater = FirmwareUpdater(AsyncMock(), serial_rpc, None, reader_mock, None)
        res = await updater.get_firmware_info(slave_id=1, port={"path": "test"})

        self.assertEqual(res.get("fw"), "1")
        self.assertEqual(res.get("available_fw"), "2")
        self.assertEqual(res.get("can_update"), True)
        self.assertEqual(res.get("model"), "MAP12E")


class TestFlashFw(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.chunk_size = WB_DEVICE_PARAMETERS["fw_data_block"].register_count * 2
        data = random.randbytes(
            WB_DEVICE_PARAMETERS["fw_info_block"].register_count * 2 + 3 * self.chunk_size
        )
        self.wbfw = parse_wbfw(data)

    async def test_success(self):
        mock = AsyncMock()
        mock.set_progress = Mock()
        mock.write = AsyncMock()
        await flash_fw(mock, self.wbfw, mock)
        self.assertEqual(len(mock.mock_calls), 7)
        expected_calls = [
            call.write(WB_DEVICE_PARAMETERS["fw_info_block"], self.wbfw.info, 1.0),
            call.write(WB_DEVICE_PARAMETERS["fw_data_block"], self.wbfw.data[: self.chunk_size]),
            call.set_progress(33),
            call.write(
                WB_DEVICE_PARAMETERS["fw_data_block"],
                self.wbfw.data[self.chunk_size : 2 * self.chunk_size],
            ),
            call.set_progress(66),
            call.write(WB_DEVICE_PARAMETERS["fw_data_block"], self.wbfw.data[2 * self.chunk_size :]),
            call.set_progress(100),
        ]
        mock.assert_has_calls(expected_calls, False)
        self.assertEqual(len(mock.mock_calls), len(expected_calls))

    async def test_fail(self):
        mock = AsyncMock()
        mock.set_progress = Mock()
        mock.write = AsyncMock()
        mock.write.side_effect = [None, None, SerialTimeoutException("1"), SerialTimeoutException("2")]
        with self.assertRaises(SerialTimeoutException) as cm:
            await flash_fw(mock, self.wbfw, mock)
        self.assertEqual(str(cm.exception), "2")
        self.assertEqual(len(mock.mock_calls), 5)
        expected_calls = [
            call.write(WB_DEVICE_PARAMETERS["fw_info_block"], self.wbfw.info, 1.0),
            call.write(WB_DEVICE_PARAMETERS["fw_data_block"], self.wbfw.data[: self.chunk_size]),
            call.set_progress(33),
            call.write(
                WB_DEVICE_PARAMETERS["fw_data_block"],
                self.wbfw.data[self.chunk_size : 2 * self.chunk_size],
            ),
            call.write(WB_DEVICE_PARAMETERS["fw_data_block"], self.wbfw.data[2 * self.chunk_size :]),
        ]
        mock.assert_has_calls(expected_calls, False)
        self.assertEqual(len(mock.mock_calls), len(expected_calls))


class TestRebootToBootloader(unittest.IsolatedAsyncioTestCase):

    async def test_preserve_port_settings(self):
        mock = AsyncMock()
        mock.write = AsyncMock()
        await reboot_to_bootloader(mock, True)
        self.assertEqual(len(mock.mock_calls), 1)
        mock.write.assert_called_once_with(
            WB_DEVICE_PARAMETERS["reboot_to_bootloader_preserve_port_settings"], 1, 1.0
        )

    async def test_switch_to_9600(self):
        mock = AsyncMock()
        mock.set_default_port_settings = Mock()
        await reboot_to_bootloader(mock, False)
        self.assertEqual(len(mock.mock_calls), 2)
        expected_calls = [
            call.write(WB_DEVICE_PARAMETERS["reboot_to_bootloader"], 1, 1.0),
            call.set_default_port_settings(),
        ]
        mock.assert_has_calls(expected_calls, False)
        self.assertEqual(len(mock.mock_calls), len(expected_calls))

    async def test_switch_to_9600_timeout(self):
        mock = AsyncMock()
        mock.write = AsyncMock()
        mock.write.side_effect = SerialTimeoutException("1")
        mock.set_default_port_settings = Mock()
        await reboot_to_bootloader(mock, False)
        expected_calls = [
            call.write(WB_DEVICE_PARAMETERS["reboot_to_bootloader"], 1, 1.0),
            call.set_default_port_settings(),
        ]
        mock.assert_has_calls(expected_calls, False)
        self.assertEqual(len(mock.mock_calls), len(expected_calls))

    async def test_switch_to_9600_exception(self):
        mock = AsyncMock()
        mock.write = AsyncMock()
        mock.write.side_effect = WBModbusException("1", 1)
        with self.assertRaises(WBModbusException) as cm:
            await reboot_to_bootloader(mock, False)
        self.assertEqual(cm.exception, mock.write.side_effect)
        self.assertEqual(len(mock.mock_calls), 1)
        mock.write.assert_called_once_with(WB_DEVICE_PARAMETERS["reboot_to_bootloader"], 1, 1.0)


class TestRestoreFirmware(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.chunk_size = WB_DEVICE_PARAMETERS["fw_data_block"].register_count * 2
        self.fw_data = random.randbytes(
            WB_DEVICE_PARAMETERS["fw_info_block"].register_count * 2 + 3 * self.chunk_size
        )
        self.wbfw = parse_wbfw(self.fw_data)

    async def test_success(self):
        mock = AsyncMock()
        mock.download_file = Mock()
        mock.download_file.return_value = self.fw_data
        mock.set_progress = Mock()
        mock.delete = Mock()
        fw = ReleasedBinary("1.1.1", "test")
        with patch("wb.device_manager.firmware_update.flash_fw", mock.flash_fw):
            await restore_firmware(mock, mock, fw, mock)
            expected_calls = [
                call.set_progress(0),
                call.download_file(fw.endpoint),
                call.flash_fw(mock, self.wbfw, mock),
                call.delete(),
            ]
            mock.assert_has_calls(expected_calls, False)
            self.assertEqual(len(mock.mock_calls) - len(mock.description.mock_calls), len(expected_calls))

    async def test_exception(self):
        mock = AsyncMock()
        mock.download_file = Mock()
        mock.download_file.return_value = self.fw_data
        mock.set_progress = Mock()
        mock.set_error = Mock()
        fw = ReleasedBinary("1.1.1", "test")
        with patch("wb.device_manager.firmware_update.flash_fw", mock.flash_fw):
            mock.flash_fw.side_effect = SerialTimeoutException("ex")
            await restore_firmware(mock, mock, fw, mock)
            expected_calls = [
                call.set_progress(0),
                call.download_file(fw.endpoint),
                call.flash_fw(mock, self.wbfw, mock),
                call.set_error("ex"),
            ]
            mock.assert_has_calls(expected_calls, False)
            self.assertEqual(len(mock.mock_calls) - len(mock.description.mock_calls), len(expected_calls))


class TestUpdateSoftware(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.chunk_size = WB_DEVICE_PARAMETERS["fw_data_block"].register_count * 2
        self.fw_data = random.randbytes(
            WB_DEVICE_PARAMETERS["fw_info_block"].register_count * 2 + 3 * self.chunk_size
        )
        self.wbfw = parse_wbfw(self.fw_data)

    async def test_success(self):
        mock = AsyncMock()
        mock.download_file = Mock()
        mock.download_file.return_value = self.fw_data
        mock.set_progress = Mock()
        mock.delete = Mock()
        fw = ReleasedBinary("1.1.1", "test")
        sw = SoftwareComponent(available=fw)
        with patch("wb.device_manager.firmware_update.flash_fw", mock.flash_fw), patch(
            "wb.device_manager.firmware_update.reboot_to_bootloader", mock.reboot_to_bootloader
        ), patch("wb.device_manager.firmware_update.read_sn"), patch(
            "wb.device_manager.firmware_update.read_device_model"
        ):
            await update_software(mock, mock, sw, mock, True)
            expected_calls = [
                call.set_progress(0),
                call.reboot_to_bootloader(mock, True),
                call.download_file(fw.endpoint),
                call.flash_fw(mock, self.wbfw, mock),
            ]
            mock.assert_has_calls(expected_calls, False)
            self.assertEqual(len(mock.mock_calls) - len(mock.description.mock_calls), len(expected_calls))

    async def test_exception(self):
        mock = AsyncMock()
        mock.download_file = Mock()
        mock.download_file.return_value = self.fw_data
        mock.set_progress = Mock()
        mock.set_error = Mock()
        mock.delete = Mock()
        fw = ReleasedBinary("1.1.1", "test")
        sw = SoftwareComponent(available=fw)
        with patch("wb.device_manager.firmware_update.flash_fw", mock.flash_fw), patch(
            "wb.device_manager.firmware_update.reboot_to_bootloader", mock.reboot_to_bootloader
        ), patch("wb.device_manager.firmware_update.read_sn"), patch(
            "wb.device_manager.firmware_update.read_device_model"
        ):
            mock.flash_fw.side_effect = SerialTimeoutException("ex")
            await update_software(mock, mock, sw, mock, True)
            expected_calls = [
                call.set_progress(0),
                call.reboot_to_bootloader(mock, True),
                call.download_file(fw.endpoint),
                call.flash_fw(mock, self.wbfw, mock),
                call.set_error("ex"),
            ]
            mock.assert_has_calls(expected_calls, False)
            self.assertEqual(len(mock.mock_calls) - len(mock.description.mock_calls), len(expected_calls))


class TestParseWbfw(unittest.TestCase):

    def setUp(self):
        self.chunk_size = WB_DEVICE_PARAMETERS["fw_data_block"].register_count * 2

    def test_success(self):
        data = random.randbytes(
            WB_DEVICE_PARAMETERS["fw_info_block"].register_count * 2 + 3 * self.chunk_size
        )
        res = parse_wbfw(data)
        assert res.info == data[: WB_DEVICE_PARAMETERS["fw_info_block"].register_count * 2]
        assert res.data == data[WB_DEVICE_PARAMETERS["fw_info_block"].register_count * 2 :]

    def test_even(self):
        data = random.randbytes(11)
        with self.assertRaises(ValueError):
            parse_wbfw(data)

    def test_too_small(self):
        data = random.randbytes(2)
        with self.assertRaises(ValueError):
            parse_wbfw(data)
