#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import random
import time
from typing import Union

from . import logger
from .bus_scan_state import (
    BusScanStateManager,
    DeviceInfo,
    Firmware,
    Port,
    SerialParams,
    StateError,
    get_all_uart_params,
    get_uart_params_count,
    make_uuid,
)
from .mqtt_rpc import SRPCClient
from .serial_rpc import (
    DEFAULT_RPC_CALL_TIMEOUT_MS,
    SerialConfig,
    TcpConfig,
    add_port_config_to_rpc_request,
)


class OneByOneBusScanner:
    """
    A class that searches for Wiren Board Modbus devices by attempting
    to read serial number using all possible slave ids.
    """

    def __init__(self, state_manager: BusScanStateManager, rpc_client: SRPCClient):
        self._rpc_client = rpc_client
        self._state_manager = state_manager

    async def _device_probe(self, port_config: Union[SerialConfig, TcpConfig], slave_id: int) -> dict:
        rpc_request = {
            "total_timeout": DEFAULT_RPC_CALL_TIMEOUT_MS,
            "slave_id": slave_id,
        }
        add_port_config_to_rpc_request(rpc_request, port_config)

        return await self._rpc_client.make_rpc_call(
            driver="wb-mqtt-serial",
            service="device",
            method="Probe",
            params=rpc_request,
            timeout=DEFAULT_RPC_CALL_TIMEOUT_MS / 1000,
        )

    async def _do_scan_port(self, port_config: Union[SerialConfig, TcpConfig]) -> None:
        debug_str = str(port_config)
        logger.debug("Scanning %s", debug_str)

        try:
            await self._state_manager.add_scanning_port(debug_str, False)
            for slave_id in random.sample(range(1, 247), 246):
                device = await self._device_probe(port_config, slave_id)
                sn = device.get("sn", "")
                if not sn:
                    # nothing found
                    continue
                if self._state_manager.is_device_found(sn):
                    logger.debug("Device %s on %s is already scanned; skipping", sn, debug_str)
                    continue

                fw = Firmware(**device.get("fw", {}))
                fw.ext_support = False
                fw_signature = device.get("fw_signature", "")
                device_model = device.get("device_signature", "")
                if not device_model:
                    device_model = fw_signature
                device_info = DeviceInfo(
                    uuid=make_uuid(sn),
                    port=Port(port_config),
                    title=device_model,
                    sn=sn,
                    device_signature=device_model,
                    fw_signature=fw_signature,
                    configured_device_type=device.get("configured_device_type", ""),
                    last_seen=int(time.time()),
                    cfg=SerialParams(**device.get("cfg", {})),
                    fw=fw,
                )
                errors = device.get("errors", [])
                for error in errors:
                    device_info.errors.append(StateError(**error))

                await self._state_manager.found_device(sn, device_info)

        except Exception as err:
            logger.exception("Unhandled exception during scan %s: %s", debug_str, err)
            await self._state_manager.add_error_port(debug_str)
            raise
        finally:
            await self._state_manager.remove_scanning_port(debug_str)

    async def _scan_serial_port(self, port):
        port_config = SerialConfig(path=port)

        for bd, parity, stopbits in get_all_uart_params():
            port_config.baud_rate = bd
            port_config.parity = parity
            port_config.stop_bits = stopbits
            await self._do_scan_port(port_config)

    async def _scan_tcp_port(self, ip_port):
        components = ip_port.split(":")
        port_config = TcpConfig(address=components[0], port=int(components[1]))
        await self._do_scan_port(port_config)

    def create_scan_tasks(self, ports) -> list:
        tasks = []
        name_template = "Ordinary scan %s %s"
        for serial_port in ports.serial:
            tasks.append(
                asyncio.create_task(
                    self._scan_serial_port(serial_port),
                    name=name_template % (serial_port, "serial"),
                )
            )
        for tcp_port in ports.tcp:
            tasks.append(
                asyncio.create_task(
                    self._scan_tcp_port(tcp_port),
                    name=name_template % (tcp_port, "tcp"),
                )
            )

        return tasks

    def get_scan_items_count(self, ports) -> int:
        return len(ports.serial) * get_uart_params_count() + len(ports.tcp)
