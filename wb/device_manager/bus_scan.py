#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio

from . import logger, mqtt_rpc
from .bootloader_scan import BootloaderModeScanner
from .bus_scan_state import BusScanStateManager, ParsedPorts, get_all_uart_params
from .fast_modbus_scan import FastModbusCommand, FastModbusScanner
from .one_by_one_scan import OneByOneBusScanner
from .serial_rpc import SerialRPCWrapper
from .state_error import GenericStateError, RPCCallTimeoutStateError


class BusScanner:
    def __init__(self, mqtt_connection, rpc_client, asyncio_loop):
        self._rpc_client = rpc_client
        self._asyncio_loop = asyncio_loop
        self._bus_scanning_task = None
        self._state_manager = BusScanStateManager(mqtt_connection, asyncio_loop)

    @property
    def rpc_client(self):
        return self._rpc_client

    def publish_state(self):
        return self._state_manager.publish_state()

    def clear_state(self):
        return self._state_manager.clear_state()

    @property
    def asyncio_loop(self):
        return self._asyncio_loop

    async def get_ports(self) -> ParsedPorts:
        response = await self.rpc_client.make_rpc_call(
            driver="wb-mqtt-serial",
            service="ports",
            method="Load",
            params={},
            timeout=1.0,  # s; rpc call goes around scheduler queue => relatively small
        )
        serial_ports = []
        tcp_ports = []
        for port_info in response:
            if "path" in port_info:
                serial_ports.append(port_info["path"])
            elif "address" in port_info and "port" in port_info:
                tcp_ports.append(f"{port_info['address']}:{port_info['port']}")
        return ParsedPorts(serial=serial_ports, tcp=tcp_ports)

    async def launch_bus_scan(self, **kwargs):
        if self._bus_scanning_task and not self._bus_scanning_task.done():
            raise mqtt_rpc.MQTTRPCAlreadyProcessingException()
        scan_type = kwargs.get("scan_type")
        preserve_old_results = kwargs.get("preserve_old_results")
        port = kwargs.get("port")
        out_of_order_slave_ids = kwargs.get("out_of_order_slave_ids", [])
        logger.debug(
            "Start %s bus scanning, preserve old results %s, port %s",
            scan_type,
            preserve_old_results,
            port,
        )
        self._bus_scanning_task = self.asyncio_loop.create_task(
            self.scan_serial_bus(scan_type, preserve_old_results, port, out_of_order_slave_ids),
            name="Scan serial bus (long running)",
        )
        return "Ok"

    async def stop_bus_scan(self):
        """
        TODO: check, tasks are actually cancelled before returning "Ok" via rpc
        Check https://docs.python.org/dev/library/asyncio-task.html#asyncio.Task.cancel for more info
        """
        if self._bus_scanning_task and not self._bus_scanning_task.done():
            logger.debug("Stop bus scanning")
            self._bus_scanning_task.cancel()
            try:
                await self._bus_scanning_task
            except asyncio.CancelledError:
                pass
            return "Ok"
        raise mqtt_rpc.MQTTRPCAlreadyProcessingException()

    def _get_parsed_ports_from_request(self, port_config: dict) -> ParsedPorts:
        if "path" not in port_config:
            return ParsedPorts()
        if ":" in port_config["path"]:
            return ParsedPorts(serial=[], tcp=[port_config["path"]])
        return ParsedPorts(serial=[port_config["path"]], tcp=[])

    async def scan_serial_bus(
        self, scan_type, preserve_old_results, port_config, out_of_order_slave_ids: list[int]
    ):
        await self._state_manager.reset(preserve_old_results)

        if isinstance(port_config, dict):
            ports = self._get_parsed_ports_from_request(port_config)
        else:
            try:
                ports = await self.get_ports()
            except mqtt_rpc.MQTTRPCCallTimeoutError:
                logger.exception("No answer from wb-mqtt-serial")
                await self._state_manager.scan_finished(RPCCallTimeoutStateError())
                return
        try:
            fast_modbus_scanner = FastModbusScanner(
                self._rpc_client,
                self._state_manager,
                get_all_uart_params,
            )
            fast_modbus_scan_items_count = fast_modbus_scanner.get_scan_items_count(ports)
            one_by_one_scanner = OneByOneBusScanner(self._state_manager, self._rpc_client)
            one_by_one_scan_items_count = one_by_one_scanner.get_scan_items_count(ports)
            if scan_type == "extended":
                self._state_manager.set_scan_items_count(fast_modbus_scan_items_count)
                # Use 0x60 for scanning as the last device with deprecated command only was sold 18.12.24
                await asyncio.gather(
                    *fast_modbus_scanner.create_scan_tasks(ports, FastModbusCommand.DEPRECATED),
                    return_exceptions=True,
                )
            elif scan_type == "standard":
                self._state_manager.set_scan_items_count(one_by_one_scan_items_count)
                await asyncio.gather(*one_by_one_scanner.create_scan_tasks(ports), return_exceptions=True)
            elif scan_type == "bootloader":
                bootloader_mode_scanner = BootloaderModeScanner(
                    SerialRPCWrapper(self.rpc_client),
                    self._state_manager,
                    get_all_uart_params,
                )
                self._state_manager.set_scan_items_count(
                    bootloader_mode_scanner.get_scan_items_count(ports, out_of_order_slave_ids)
                )
                await asyncio.gather(
                    *bootloader_mode_scanner.create_scan_tasks(ports, out_of_order_slave_ids),
                    return_exceptions=True,
                )
            else:
                self._state_manager.set_scan_items_count(
                    fast_modbus_scan_items_count + one_by_one_scan_items_count
                )
                await asyncio.gather(
                    *fast_modbus_scanner.create_scan_tasks(ports, FastModbusCommand.DEPRECATED),
                    return_exceptions=True,
                )
                await asyncio.gather(*one_by_one_scanner.create_scan_tasks(ports), return_exceptions=True)

            await self._state_manager.scan_complete()
        except asyncio.CancelledError:
            await self._state_manager.scan_finished()
            raise
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception("Unhandled exception during overall scan %s", e)
            await self._state_manager.scan_finished(GenericStateError())
