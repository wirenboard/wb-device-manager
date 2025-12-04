#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint: disable=protected-access, unnecessary-dunder-call, unused-argument
import random
import unittest
from typing import Union
from unittest.mock import AsyncMock, Mock, call, patch

from jsonrpc.exceptions import JSONRPCDispatchException
from mqttrpc import client as rpcclient
from parameterized import parameterized

from wb.device_manager.bus_scan_state import Port
from wb.device_manager.firmware_update import (
    BootloaderInfo,
    ComponentInfo,
    DeviceUpdateInfo,
    FirmwareInfo,
    FirmwareInfoReader,
    FirmwareUpdater,
    SoftwareComponent,
    flash_fw,
    parse_wbfw,
    reboot_to_bootloader,
    restore_firmware,
    update_software,
    write_fw_data_block,
)
from wb.device_manager.fw_downloader import ReleasedBinary
from wb.device_manager.mqtt_rpc import MQTTRPCErrorCode
from wb.device_manager.serial_rpc import (
    WB_DEVICE_PARAMETERS,
    ModbusProtocol,
    ParameterConfig,
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
        d1 = DeviceUpdateInfo(
            port=Port("test"), protocol=ModbusProtocol.MODBUS_RTU, slave_id=1, to_version="1"
        )
        d2 = DeviceUpdateInfo(
            port=Port("test"), protocol=ModbusProtocol.MODBUS_RTU, slave_id=1, to_version="1"
        )
        d3 = DeviceUpdateInfo(
            port=Port("test"), protocol=ModbusProtocol.MODBUS_RTU, slave_id=2, to_version="1"
        )
        d4 = DeviceUpdateInfo(
            port=Port("test1"), protocol=ModbusProtocol.MODBUS_RTU, slave_id=1, to_version="1"
        )
        self.assertEqual(d1, d2)
        self.assertNotEqual(d1, d3)
        self.assertNotEqual(d1, d4)


class TestGetFirmwareInfo(unittest.IsolatedAsyncioTestCase):

    async def test_modbus_exception(self):
        reader_mock = AsyncMock()
        reader_mock.read_fw_version = AsyncMock()
        reader_mock.read_fw_version.side_effect = WBModbusException("test msg", 1)
        updater = FirmwareUpdater(AsyncMock(), None, None, reader_mock, None)
        with self.assertRaises(JSONRPCDispatchException) as cm:
            await updater.get_firmware_info(slave_id=1, port={"path": "test"})

        self.assertEqual(cm.exception.error.code, MQTTRPCErrorCode.REQUEST_HANDLING_ERROR.value)
        self.assertEqual(cm.exception.error.message, "test msg")
        self.assertEqual(cm.exception.error.data, None)

    async def test_rpc_timeout_exception(self):
        reader_mock = AsyncMock()
        reader_mock.read_fw_version = AsyncMock()
        reader_mock.read_fw_version.side_effect = SerialTimeoutException("test msg")
        updater = FirmwareUpdater(AsyncMock(), None, None, reader_mock, None)
        with self.assertRaises(JSONRPCDispatchException) as cm:
            await updater.get_firmware_info(slave_id=1, port={"path": "test"})

        self.assertEqual(cm.exception.error.code, MQTTRPCErrorCode.REQUEST_HANDLING_ERROR.value)
        self.assertEqual(cm.exception.error.message, "test msg")
        self.assertEqual(cm.exception.error.data, None)

    async def test_generic_rpc_exception(self):
        reader_mock = AsyncMock()
        reader_mock.read_fw_version = AsyncMock()
        reader_mock.read_fw_version.side_effect = rpcclient.MQTTRPCError(
            "test msg", MQTTRPCErrorCode.REQUEST_HANDLING_ERROR.value, "test data"
        )
        updater = FirmwareUpdater(AsyncMock(), None, None, reader_mock, None)
        with self.assertRaises(rpcclient.MQTTRPCError) as cm:
            await updater.get_firmware_info(slave_id=1, port={"path": "test"})

        self.assertEqual(cm.exception, reader_mock.read_fw_version.side_effect)

    @parameterized.expand(
        [
            (WBModbusException("test msg", 6), lambda x: x > 1, []),
            (WBModbusException("test msg", 2), lambda x: x == 1, []),
            (b"\x00", lambda x: x == 1, []),
            (b"\x88", lambda x: x == 1, [3, 7]),
        ]
    )
    async def test_read_components_presence(self, reading_result, count_of_readings_func, result):

        count_of_readings = 0

        def read_components_presence(param_config: ParameterConfig):
            nonlocal count_of_readings
            count_of_readings += 1
            if param_config == WB_DEVICE_PARAMETERS["components_presence"]:
                if isinstance(reading_result, Exception):
                    raise reading_result
                return reading_result
            return None

        serial_device = AsyncMock()
        serial_device.read = AsyncMock()
        serial_device.read.side_effect = read_components_presence
        mock = Mock()
        mock.get = Mock()

        with patch("wb.device_manager.firmware_update.parse_releases", mock.parse_releases):
            reader_mock = FirmwareInfoReader(None)
            components = await reader_mock.read_components_presence(serial_device)

        self.assertTrue(count_of_readings_func(count_of_readings))
        self.assertEqual(components, result)

    async def test_successful_read(self):
        reader_mock = AsyncMock()
        reader_mock.read_fw_version = AsyncMock()
        reader_mock.read_fw_version.return_value = "1"
        reader_mock.read_fw_signature = AsyncMock()
        reader_mock.read_fw_signature.return_value = "sig"
        reader_mock.read_released_fw = Mock()
        reader_mock.read_released_fw.return_value = ReleasedBinary("2", "endpoint")
        reader_mock.read_bootloader_info = AsyncMock()
        reader_mock.read_bootloader_info.return_value = BootloaderInfo(can_preserve_port_settings=True)
        reader_mock.read_components_info = AsyncMock()
        reader_mock.read_components_info.return_value = {
            3: ComponentInfo(
                current_version="3", available=ReleasedBinary("4", "endpoint"), model="Component1"
            ),
            7: ComponentInfo(
                current_version="5", available=ReleasedBinary("6", "endpoint"), model="Component2"
            ),
        }

        def read_model(
            _port_config: Union[SerialConfig, TcpConfig],
            _slave_id: int,
            param_config: ParameterConfig,
            _protocol: ModbusProtocol,
        ):
            if param_config == WB_DEVICE_PARAMETERS["device_model_extended"]:
                return "MAP12\x02E"
            return None

        serial_rpc = AsyncMock()
        serial_rpc.read = AsyncMock()
        serial_rpc.read.side_effect = read_model
        updater = FirmwareUpdater(AsyncMock(), serial_rpc, None, reader_mock, None)
        res = await updater.get_firmware_info(slave_id=1, port={"path": "test"})

        self.assertEqual(res.get("fw"), "1")
        self.assertEqual(res.get("available_fw"), "2")
        self.assertEqual(res.get("can_update"), True)
        self.assertEqual(res.get("fw_has_update"), True)
        self.assertEqual(res.get("model"), "MAP12E")
        self.assertEqual(
            res.get("components"),
            {
                3: {"model": "Component1", "fw": "3", "available_fw": "4", "has_update": True},
                7: {"model": "Component2", "fw": "5", "available_fw": "6", "has_update": True},
            },
        )


class TestFlashFw(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.chunk_size = WB_DEVICE_PARAMETERS["fw_data_block"].register_count * 2
        data = random.randbytes(
            WB_DEVICE_PARAMETERS["fw_info_block"].register_count * 2 + 3 * self.chunk_size
        )
        self.wbfw = parse_wbfw(data)

    async def test_write_fw_data_block_success(self):
        serial_device_mock = AsyncMock()
        serial_device_mock.write = AsyncMock()
        await write_fw_data_block(serial_device_mock, self.wbfw.data[: self.chunk_size])
        self.assertEqual(len(serial_device_mock.mock_calls), 1)
        serial_device_mock.write.assert_called_once_with(
            WB_DEVICE_PARAMETERS["fw_data_block"], self.wbfw.data[: self.chunk_size]
        )

    async def test_write_fw_data_block_slave_device_failure(self):
        serial_device_mock = AsyncMock()
        serial_device_mock.write = AsyncMock()
        # Device sends slave device failure (0x04) if the chunk is already written
        serial_device_mock.write.side_effect = WBModbusException("test", 4)
        await write_fw_data_block(serial_device_mock, self.wbfw.data[: self.chunk_size])
        serial_device_mock.write.assert_called_once_with(
            WB_DEVICE_PARAMETERS["fw_data_block"], self.wbfw.data[: self.chunk_size]
        )

    async def test_write_fw_data_block_timeout_then_slave_device_failure(self):
        serial_device_mock = AsyncMock()
        serial_device_mock.write = AsyncMock()
        # Device sends slave device failure (0x04) if the chunk is already written
        serial_device_mock.write.side_effect = [
            SerialTimeoutException("timeout"),
            WBModbusException("test", 4),
        ]
        await write_fw_data_block(serial_device_mock, self.wbfw.data[: self.chunk_size])
        self.assertEqual(len(serial_device_mock.mock_calls), 2)
        expected_calls = [
            call.write(WB_DEVICE_PARAMETERS["fw_data_block"], self.wbfw.data[: self.chunk_size]),
            call.write(WB_DEVICE_PARAMETERS["fw_data_block"], self.wbfw.data[: self.chunk_size]),
        ]
        serial_device_mock.assert_has_calls(expected_calls)

    async def test_write_fw_data_block_failure(self):
        serial_device_mock = AsyncMock()
        serial_device_mock.write = AsyncMock()
        serial_device_mock.write.side_effect = WBModbusException("test", 1)
        with self.assertRaises(WBModbusException) as cm:
            await write_fw_data_block(serial_device_mock, self.wbfw.data[: self.chunk_size])
        self.assertEqual(cm.exception, serial_device_mock.write.side_effect)
        self.assertEqual(len(serial_device_mock.mock_calls), 3)
        expected_calls = [
            call.write(WB_DEVICE_PARAMETERS["fw_data_block"], self.wbfw.data[: self.chunk_size]),
            call.write(WB_DEVICE_PARAMETERS["fw_data_block"], self.wbfw.data[: self.chunk_size]),
            call.write(WB_DEVICE_PARAMETERS["fw_data_block"], self.wbfw.data[: self.chunk_size]),
        ]
        serial_device_mock.assert_has_calls(expected_calls)

    async def test_success(self):
        mock = AsyncMock()
        mock.set_progress = Mock()
        mock.write = AsyncMock()
        await flash_fw(mock, self.wbfw, mock)
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
        with patch("wb.device_manager.firmware_update.write_fw_data_block", mock.write_fw_data_block):
            mock.write_fw_data_block.side_effect = [None, SerialTimeoutException("timeout")]
            with self.assertRaises(SerialTimeoutException) as cm:
                await flash_fw(mock, self.wbfw, mock)
            self.assertEqual(str(cm.exception), "timeout")
            expected_calls = [
                call.write(WB_DEVICE_PARAMETERS["fw_info_block"], self.wbfw.info, 1.0),
                call.write_fw_data_block(mock, self.wbfw.data[: self.chunk_size]),
                call.set_progress(33),
                call.write_fw_data_block(mock, self.wbfw.data[self.chunk_size : 2 * self.chunk_size]),
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
        mock.download_wbfw = Mock()
        mock.download_wbfw.return_value = self.wbfw
        mock.set_progress = Mock()
        mock.delete = Mock()
        fw = ReleasedBinary("1.1.1", "test")
        with patch("wb.device_manager.firmware_update.flash_fw", mock.flash_fw), patch(
            "wb.device_manager.firmware_update.download_wbfw", mock.download_wbfw
        ):
            downloader_mock = AsyncMock()
            await restore_firmware(mock, mock, fw, downloader_mock)
            expected_calls = [
                call.set_progress(0),
                call.download_wbfw(downloader_mock, fw.endpoint),
                call.flash_fw(mock, self.wbfw, mock),
                call.delete(),
            ]
            mock.assert_has_calls(expected_calls, False)
            self.assertEqual(len(mock.mock_calls) - len(mock.description.mock_calls), len(expected_calls))

    async def test_exception(self):
        mock = AsyncMock()
        mock.download_wbfw = Mock()
        mock.download_wbfw.return_value = self.wbfw
        mock.set_progress = Mock()
        mock.set_error_from_exception = Mock()
        mock.description = Mock()
        fw = ReleasedBinary("1.1.1", "test")
        with patch("wb.device_manager.firmware_update.flash_fw", mock.flash_fw), patch(
            "wb.device_manager.firmware_update.download_wbfw", mock.download_wbfw
        ):
            mock.flash_fw.side_effect = SerialTimeoutException("ex")
            downloader_mock = AsyncMock()
            await restore_firmware(mock, mock, fw, downloader_mock)
            expected_calls = [
                call.set_progress(0),
                call.download_wbfw(downloader_mock, fw.endpoint),
                call.flash_fw(mock, self.wbfw, mock),
                call.set_error_from_exception(mock.flash_fw.side_effect),
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

    @parameterized.expand(
        [
            (SoftwareComponent(), True),
            (ComponentInfo(), False),
        ]
    )
    async def test_success(self, sw, expected_reboot_to_bl):
        mock = AsyncMock()
        mock.download_wbfw = Mock()
        mock.download_wbfw.return_value = self.wbfw
        mock.set_progress = Mock()
        mock.delete = Mock()
        fw = ReleasedBinary("1.1.1", "test")
        sw.available = fw
        with (
            patch("wb.device_manager.firmware_update.flash_fw", mock.flash_fw),
            patch("wb.device_manager.firmware_update.reboot_to_bootloader", mock.reboot_to_bootloader),
            patch("wb.device_manager.firmware_update.read_sn", new_callable=AsyncMock, return_value=12345),
            patch(
                "wb.device_manager.firmware_update.read_device_model",
                new_callable=AsyncMock,
                return_value="test_model",
            ),
            patch("wb.device_manager.firmware_update.download_wbfw", mock.download_wbfw),
        ):
            downloader_mock = AsyncMock()
            await update_software(mock, mock, sw, downloader_mock, True)
            expected_calls = [
                call.set_progress(0),
            ]
            if expected_reboot_to_bl:
                expected_calls.append(call.reboot_to_bootloader(mock, True))
            expected_calls.extend(
                [
                    call.download_wbfw(downloader_mock, fw.endpoint),
                    call.flash_fw(mock, self.wbfw, mock),
                ]
            )

            mock.assert_has_calls(expected_calls, False)
            self.assertEqual(len(mock.mock_calls) - len(mock.description.mock_calls), len(expected_calls))

    @parameterized.expand(
        [
            (SoftwareComponent(), True),
            (ComponentInfo(), False),
        ]
    )
    async def test_exception(self, sw, expected_reboot_to_bl):
        mock = AsyncMock()
        mock.download_wbfw = Mock()
        mock.download_wbfw.return_value = self.wbfw
        mock.set_progress = Mock()
        mock.set_error_from_exception = Mock()
        mock.description = Mock()
        mock.delete = Mock()
        fw = ReleasedBinary("1.1.1", "test")
        sw.available = fw
        with (
            patch("wb.device_manager.firmware_update.flash_fw", mock.flash_fw),
            patch("wb.device_manager.firmware_update.reboot_to_bootloader", mock.reboot_to_bootloader),
            patch("wb.device_manager.firmware_update.read_sn", new_callable=AsyncMock, return_value=12345),
            patch(
                "wb.device_manager.firmware_update.read_device_model",
                new_callable=AsyncMock,
                return_value="test_model",
            ),
            patch("wb.device_manager.firmware_update.download_wbfw", mock.download_wbfw),
        ):
            mock.flash_fw.side_effect = SerialTimeoutException("ex")
            downloader_mock = AsyncMock()
            await update_software(mock, mock, sw, downloader_mock, True)
            expected_calls = [
                call.set_progress(0),
            ]
            if expected_reboot_to_bl:
                expected_calls.append(call.reboot_to_bootloader(mock, True))
            expected_calls.extend(
                [
                    call.download_wbfw(downloader_mock, fw.endpoint),
                    call.flash_fw(mock, self.wbfw, mock),
                    call.set_error_from_exception(mock.flash_fw.side_effect),
                ]
            )

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


class TestUpdateSoftwareScenarios(unittest.IsolatedAsyncioTestCase):

    @parameterized.expand(
        [
            (
                {
                    "slave_id": 23,
                    "port": {"address": "192.168.1.100", "port": 1000},
                    "type": "bootloader",
                },
                False,
            ),
        ]
    )
    async def test_start_update_exception(self, kwargs, preserve_settings):
        reader_mock = AsyncMock()
        reader_mock.read_components_presence = AsyncMock()
        reader_mock.read = AsyncMock()
        bootloader = BootloaderInfo(
            current_version="1.1.0",
            available=ReleasedBinary("1.1.1", "test"),
            can_preserve_port_settings=preserve_settings,
        )
        reader_mock.read.return_value = FirmwareInfo(
            current_version="1.1.0", available=ReleasedBinary("1.1.1", "test"), bootloader=bootloader
        )

        def read_component_info(
            _port_config: Union[SerialConfig, TcpConfig], _slave_id: int, component_number: int
        ):
            if component_number == 3:
                return ComponentInfo(
                    current_version="3", available=ReleasedBinary("4", "endpoint"), model="Component1"
                )
            if component_number == 7:
                return ComponentInfo(
                    current_version="5", available=ReleasedBinary("6", "endpoint"), model="Component2"
                )
            return None

        reader_mock.read_component_info = AsyncMock()
        reader_mock.read_component_info.side_effect = read_component_info

        serial_rpc = AsyncMock()
        serial_rpc.read = AsyncMock()
        updater = FirmwareUpdater(AsyncMock(), serial_rpc, None, reader_mock, None)
        with self.assertRaises(ValueError):
            await updater.update_software(**kwargs)

    @parameterized.expand(
        [
            (
                {
                    "slave_id": 23,
                    "port": {"path": "/dev/ttyRS485-1", "baud_rate": 9600, "parity": "N", "stop_bits": 2},
                    "type": "component",
                },
                False,
                "_update_components",
            ),
            (
                {
                    "slave_id": 23,
                    "port": {"path": "/dev/ttyRS485-1", "baud_rate": 9600, "parity": "N", "stop_bits": 2},
                    "type": "firmware",
                },
                False,
                "_update_firmware",
            ),
            (
                {
                    "slave_id": 23,
                    "port": {"path": "/dev/ttyRS485-1", "baud_rate": 9600, "parity": "N", "stop_bits": 2},
                    "type": "bootloader",
                },
                True,
                "_update_bootloader",
            ),
            (
                {
                    "slave_id": 23,
                    "port": {"address": "192.168.1.100", "port": 1000},
                    "type": "firmware",
                },
                True,
                "_update_firmware",
            ),
        ]
    )
    async def test_start_update_success(
        self,
        kwargs,
        preserve_settings,
        expected_method_name,
    ):
        reader_mock = AsyncMock()
        reader_mock.read_components_presence = AsyncMock()
        reader_mock.read_components_presence.return_value = [3, 7]
        reader_mock.read = AsyncMock()
        bootloader = BootloaderInfo(
            current_version="1.1.0",
            available=ReleasedBinary("1.1.1", "test"),
            can_preserve_port_settings=preserve_settings,
        )
        reader_mock.read.return_value = FirmwareInfo(
            current_version="1.1.0", available=ReleasedBinary("1.1.1", "test"), bootloader=bootloader
        )

        reader_mock.read_components_info = AsyncMock()
        reader_mock.read_components_info.return_value = {
            3: ComponentInfo(
                current_version="3", available=ReleasedBinary("4", "endpoint"), model="Component1"
            ),
            7: ComponentInfo(
                current_version="5", available=ReleasedBinary("5", "endpoint"), model="Component2"
            ),
        }

        callable_function = None

        def create_task(coro, name):
            nonlocal callable_function
            callable_function = coro.cr_frame.f_locals.get("task")
            coro.close()
            return Mock()

        asyncio_loop = AsyncMock()
        asyncio_loop.create_task = Mock()
        asyncio_loop.create_task.side_effect = create_task

        serial_rpc = AsyncMock()
        serial_rpc.read = AsyncMock()
        updater = FirmwareUpdater(AsyncMock(), serial_rpc, asyncio_loop, reader_mock, None)
        await updater.update_software(**kwargs)
        self.assertIsInstance(callable_function.cr_frame.f_locals.get("self"), FirmwareUpdater)
        self.assertEqual(callable_function.cr_code.co_name, expected_method_name)
        callable_function.close()

    async def test_update_bootloader(self):
        bootloader = BootloaderInfo(current_version="1.1.10", available=ReleasedBinary("1.1.11", "test"))
        fw_info = FirmwareInfo(
            current_version="1.1.0", available=ReleasedBinary("1.1.1", "test"), bootloader=bootloader
        )
        components_info = {
            3: ComponentInfo(
                current_version="3", available=ReleasedBinary("4", "endpoint"), model="Component1"
            ),
            7: ComponentInfo(
                current_version="5", available=ReleasedBinary("6", "endpoint"), model="Component2"
            ),
        }
        updater = FirmwareUpdater(AsyncMock(), None, None, None, None)

        mock = AsyncMock()
        serial_device_instance = Mock()
        serial_device_instance.set_poll = mock.set_poll
        serial_device_instance.slave_id = 1
        serial_device_instance._port_config = ""
        update_notifier_instance = Mock()
        not_needed_mock = Mock()
        not_needed_mock.Device = Mock(return_value=serial_device_instance)
        not_needed_mock.UpdateStateNotifier = Mock(return_value=update_notifier_instance)

        with patch("wb.device_manager.firmware_update.Device", not_needed_mock.Device), patch(
            "wb.device_manager.firmware_update.UpdateStateNotifier", not_needed_mock.UpdateStateNotifier
        ), patch("wb.device_manager.firmware_update.update_software", mock.update_software), patch(
            "wb.device_manager.firmware_update.restore_firmware", mock.restore_firmware
        ):
            await updater._update_bootloader(serial_device_instance, fw_info, components_info)
            expected_calls = [
                call.set_poll(False),
                call.update_software(
                    serial_device_instance, update_notifier_instance, bootloader, None, False
                ),
                call.update_software().__bool__(),
                call.restore_firmware(
                    serial_device_instance, update_notifier_instance, fw_info.available, None
                ),
                call.restore_firmware().__bool__(),
                call.update_software(
                    serial_device_instance, update_notifier_instance, components_info[3], None
                ),
                call.update_software().__bool__(),
                call.update_software(
                    serial_device_instance, update_notifier_instance, components_info[7], None
                ),
                call.update_software().__bool__(),
                call.set_poll(True),
            ]
            mock.assert_has_calls(expected_calls, any_order=False)

    async def test_update_firmware(self):
        bootloader = BootloaderInfo(current_version="1.1.10", available=ReleasedBinary("1.1.11", "test"))
        fw_info = FirmwareInfo(
            current_version="1.1.0", available=ReleasedBinary("1.1.1", "test"), bootloader=bootloader
        )
        components_info = {
            3: ComponentInfo(
                current_version="3", available=ReleasedBinary("4", "endpoint"), model="Component1"
            ),
            7: ComponentInfo(
                current_version="5", available=ReleasedBinary("6", "endpoint"), model="Component2"
            ),
        }
        updater = FirmwareUpdater(AsyncMock(), None, None, None, None)

        mock = AsyncMock()
        serial_device_instance = Mock()
        serial_device_instance.set_poll = mock.set_poll
        serial_device_instance.slave_id = 1
        serial_device_instance._port_config = ""
        update_notifier_instance = Mock()
        not_needed_mock = Mock()
        not_needed_mock.SerialDevice = Mock(return_value=serial_device_instance)
        not_needed_mock.UpdateStateNotifier = Mock(return_value=update_notifier_instance)

        with patch("wb.device_manager.firmware_update.Device", not_needed_mock.Device), patch(
            "wb.device_manager.firmware_update.UpdateStateNotifier", not_needed_mock.UpdateStateNotifier
        ), patch("wb.device_manager.firmware_update.update_software", mock.update_software), patch(
            "wb.device_manager.firmware_update.restore_firmware", mock.restore_firmware
        ):
            await updater._update_firmware(serial_device_instance, fw_info, components_info)
            expected_calls = [
                call.set_poll(False),
                call.update_software(serial_device_instance, update_notifier_instance, fw_info, None, False),
                call.update_software().__bool__(),
                call.update_software(
                    serial_device_instance, update_notifier_instance, components_info[3], None
                ),
                call.update_software().__bool__(),
                call.update_software(
                    serial_device_instance, update_notifier_instance, components_info[7], None
                ),
                call.update_software().__bool__(),
                call.set_poll(True),
            ]
            mock.assert_has_calls(expected_calls, any_order=False)

    async def test_update_components(self):
        components_info = {
            3: ComponentInfo(
                current_version="3", available=ReleasedBinary("4", "endpoint"), model="Component1"
            ),
            7: ComponentInfo(
                current_version="5", available=ReleasedBinary("6", "endpoint"), model="Component2"
            ),
        }
        updater = FirmwareUpdater(AsyncMock(), None, None, None, None)

        mock = AsyncMock()
        serial_device_instance = Mock()
        serial_device_instance.set_poll = mock.set_poll
        serial_device_instance.slave_id = 1
        serial_device_instance._port_config = ""
        update_notifier_instance = Mock()
        not_needed_mock = Mock()
        not_needed_mock.SerialDevice = Mock(return_value=serial_device_instance)
        not_needed_mock.UpdateStateNotifier = Mock(return_value=update_notifier_instance)

        with patch("wb.device_manager.firmware_update.Device", not_needed_mock.Device), patch(
            "wb.device_manager.firmware_update.UpdateStateNotifier", not_needed_mock.UpdateStateNotifier
        ), patch("wb.device_manager.firmware_update.update_software", mock.update_software), patch(
            "wb.device_manager.firmware_update.restore_firmware", mock.restore_firmware
        ):
            await updater._update_components(serial_device_instance, components_info)
            expected_calls = [
                call.set_poll(False),
                call.update_software(
                    serial_device_instance, update_notifier_instance, components_info[3], None
                ),
                call.update_software().__bool__(),
                call.update_software(
                    serial_device_instance, update_notifier_instance, components_info[7], None
                ),
                call.update_software().__bool__(),
                call.set_poll(True),
            ]
            mock.assert_has_calls(expected_calls, any_order=False)
