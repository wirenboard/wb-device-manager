#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import random
import time
from typing import Iterator, Union

from . import logger
from .bus_scan_state import (
    BusScanStateManager,
    DeviceInfo,
    Port,
    SerialParams,
    get_uart_params_count,
    make_uuid,
)
from .serial_rpc import (
    WB_DEVICE_PARAMETERS,
    SerialConfig,
    SerialExceptionBase,
    SerialRPCWrapper,
    SerialTimeoutException,
    TcpConfig,
)
from .state_error import ReadFWSignatureDeviceError


def allowed_modbus_slave_ids(forbidden_ids: list[int]) -> Iterator[int]:
    for slave_id in random.sample(range(1, 247), 246):
        if slave_id not in forbidden_ids:
            yield slave_id


def partially_ordered_slave_ids(out_of_order_slave_ids: list[int]) -> Iterator[int]:
    yield from out_of_order_slave_ids
    yield from allowed_modbus_slave_ids(out_of_order_slave_ids)


async def is_in_bootloader_mode(
    slave_id: int, serial_rpc: SerialRPCWrapper, port_config: Union[SerialConfig, TcpConfig]
) -> bool:
    # Bootloader allows to read only full version, firmware - any number of registers.
    try:
        await serial_rpc.read(port_config, slave_id, WB_DEVICE_PARAMETERS["bootloader_version_full"])
    except SerialTimeoutException:
        return False
    try:
        await serial_rpc.read(port_config, slave_id, WB_DEVICE_PARAMETERS["bootloader_version"])
        return False
    except SerialTimeoutException:
        return True


def make_sn_for_device_in_bootloader(slave_id: int, port_config: Union[SerialConfig, TcpConfig]) -> str:
    seed = f"bl{slave_id}"
    if isinstance(port_config, TcpConfig):
        return seed + str(port_config)
    return seed + f"{port_config.path}{port_config.baud_rate}{port_config.data_bits}{port_config.parity}"


class BootloaderModeScanner:
    def __init__(
        self,
        serial_rpc: SerialRPCWrapper,
        scanner_state: BusScanStateManager,
        serial_port_configs_generator,
    ) -> None:
        self._serial_rpc = serial_rpc
        self._scanner_state = scanner_state
        self._serial_port_configs_generator = serial_port_configs_generator

    def _fill_serial_params(
        self, device_info: DeviceInfo, port_config: Union[SerialConfig, TcpConfig]
    ) -> None:
        if isinstance(port_config, SerialConfig):
            device_info.cfg.baud_rate = port_config.baud_rate
            device_info.cfg.parity = port_config.parity
            device_info.cfg.stop_bits = port_config.stop_bits
            device_info.cfg.data_bits = port_config.data_bits

    async def _fill_device_info(
        self, device_info: DeviceInfo, port_config: Union[SerialConfig, TcpConfig], slave_id: int
    ) -> None:
        errors = []
        try:
            device_info.fw_signature = await self._serial_rpc.read(
                port_config, slave_id, WB_DEVICE_PARAMETERS["fw_signature"]
            )
            device_info.title = device_info.fw_signature
        except SerialExceptionBase as e:
            logger.debug("Read device model failed: %s", e)
            errors.append(ReadFWSignatureDeviceError())
        device_info.errors.extend(errors)

    async def _do_device_scan(self, slave_id: int, port_config: Union[SerialConfig, TcpConfig]) -> None:
        debug_str = str(port_config)
        try:
            if await is_in_bootloader_mode(slave_id, self._serial_rpc, port_config):
                logger.info("Got device in bootloader: %d %s", slave_id, debug_str)
                sn = make_sn_for_device_in_bootloader(slave_id, port_config)
                if self._scanner_state.is_device_found(sn):
                    logger.debug("Device %d %s already scanned; skipping", slave_id, debug_str)
                    return

                device_info = DeviceInfo(
                    uuid=make_uuid(sn),
                    title="Unknown",
                    last_seen=int(time.time()),
                    port=Port(port_config),
                    cfg=SerialParams(slave_id=slave_id),
                    bootloader_mode=True,
                )

                self._fill_serial_params(device_info, port_config)
                await self._fill_device_info(device_info, port_config, slave_id)

                await self._scanner_state.found_device(sn, device_info)
        except SerialExceptionBase as err:
            logger.debug("Error during device %d in bootloader scan %s: %s", slave_id, debug_str, err)
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.exception(
                "Unhandled exception during device %d in bootloader scan %s: %s", slave_id, debug_str, err
            )
            await self._scanner_state.add_error_port(debug_str)

    async def _do_scan_port(
        self, port_config: Union[SerialConfig, TcpConfig], slave_ids: Iterator[int]
    ) -> None:
        debug_str = str(port_config)
        logger.debug("Scanning %s for devices in bootloader", debug_str)
        await self._scanner_state.add_scanning_port(debug_str, is_ext_scan=False)
        for slave_id in slave_ids:
            await self._do_device_scan(slave_id, port_config)
        await self._scanner_state.remove_scanning_port(debug_str)

    async def _scan_serial_port(self, port, out_of_order_slave_ids: list[int]) -> None:
        port_config = SerialConfig(path=port)

        def setup_port_config(bd, parity, stopbits):
            port_config.baud_rate = bd
            port_config.parity = parity
            port_config.stop_bits = stopbits

        if out_of_order_slave_ids:
            for bd, parity, stopbits in self._serial_port_configs_generator():
                setup_port_config(bd, parity, stopbits)
                await self._do_scan_port(port_config, out_of_order_slave_ids)

        for bd, parity, stopbits in self._serial_port_configs_generator():
            setup_port_config(bd, parity, stopbits)
            await self._do_scan_port(port_config, allowed_modbus_slave_ids(out_of_order_slave_ids))

    async def _scan_tcp_port(self, ip_port, out_of_order_slave_ids: list[int]) -> None:
        components = ip_port.split(":")
        port_config = TcpConfig(address=components[0], port=int(components[1]))
        await self._do_scan_port(port_config, partially_ordered_slave_ids(out_of_order_slave_ids))

    def create_scan_tasks(self, ports, out_of_order_slave_ids: list[int]) -> list:
        tasks = []
        name_template = "Search for devices in bootloader %s"
        for serial_port in ports.serial:
            tasks.append(
                asyncio.create_task(
                    self._scan_serial_port(serial_port, out_of_order_slave_ids),
                    name=name_template % serial_port,
                )
            )
        for tcp_port in ports.tcp:
            tasks.append(
                asyncio.create_task(
                    self._scan_tcp_port(tcp_port, out_of_order_slave_ids), name=name_template % tcp_port
                )
            )

        return tasks

    def get_scan_items_count(self, ports, out_of_order_slave_ids: list[int]) -> int:
        serial_count = len(ports.serial) * get_uart_params_count()
        if out_of_order_slave_ids:
            serial_count *= 2
        return serial_count + len(ports.tcp)
