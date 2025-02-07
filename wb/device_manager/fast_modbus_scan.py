#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import time
from typing import Optional, Union

from . import logger
from .bus_scan_state import (
    BusScanStateManager,
    DeviceInfo,
    Firmware,
    Port,
    SerialParams,
    make_uuid,
)
from .firmware_update import get_human_readable_device_model
from .mqtt_rpc import SRPCClient
from .serial_rpc import (
    DEFAULT_RPC_CALL_TIMEOUT_MS,
    SerialConfig,
    TcpConfig,
    add_port_config_to_rpc_request,
)
from .state_error import StateError


async def port_scan(rpc_client: SRPCClient, port_config: Union[SerialConfig, TcpConfig]) -> dict:
    rpc_request = {
        "total_timeout": DEFAULT_RPC_CALL_TIMEOUT_MS,
    }
    add_port_config_to_rpc_request(rpc_request, port_config)

    response = await rpc_client.make_rpc_call(
        driver="wb-mqtt-serial",
        service="port",
        method="Scan",
        params=rpc_request,
        timeout=DEFAULT_RPC_CALL_TIMEOUT_MS / 1000,
    )

    return response.get("devices", [])


class FastModbusScanner:
    def __init__(
        self,
        rpc_client: SRPCClient,
        scanner_state: BusScanStateManager,
        serial_port_configs_generator,
    ) -> None:
        self._rpc_client = rpc_client
        self._scanner_state = scanner_state
        self._serial_port_configs_generator = serial_port_configs_generator

    async def _do_scan(self, port_config: Union[SerialConfig, TcpConfig]) -> None:
        debug_str = str(port_config)
        try:
            for device in await port_scan(self._rpc_client, port_config):
                sn = device.get("sn")
                fw = Firmware(**device.get("fw", {}))
                fw.ext_support = True
                fw_signature = device.get("fw_signature", "")
                device_model = device.get("device_signature", "")
                if not device_model:
                    device_model = fw_signature
                device_info = DeviceInfo(
                    uuid=make_uuid(sn),
                    port=Port(port_config),
                    title=get_human_readable_device_model(device_model),
                    sn=sn,
                    device_signature=device_model,
                    fw_signature=fw_signature,
                    last_seen=int(time.time()),
                    cfg=SerialParams(**device.get("cfg", {})),
                    fw=fw,
                )
                errors = device.get("errors", [])
                for error in errors:
                    device_info.errors.append(StateError(**error))

                await self._scanner_state.found_device(sn, device_info)
        except Exception as err:
            logger.exception("Unhandled exception during Fast Modbus search %s: %s", debug_str, err)
            await self._scanner_state.add_error_port(debug_str)

    async def _do_scan_port(
        self,
        port_config: Union[SerialConfig, TcpConfig],
        progress: Optional[int] = None,
    ) -> None:
        debug_str = str(port_config)
        logger.debug("Searching %s for devices using Fast Modbus", debug_str)
        await self._scanner_state.add_scanning_port(debug_str, is_ext_scan=True)
        await self._do_scan(port_config)
        await self._scanner_state.remove_scanning_port(debug_str, progress)

    async def _scan_serial_port(self, port) -> None:
        port_config = SerialConfig(path=port)

        # New firmwares can work with any stopbits, but old ones can't
        # Since it doesn't matter what to use, let's use 2
        for bd, parity, stopbits, progress_percent in self._serial_port_configs_generator(stopbits=[2]):
            port_config.baud_rate = bd
            port_config.parity = parity
            port_config.stop_bits = stopbits
            await self._do_scan_port(port_config, progress_percent)

    async def _scan_tcp_port(self, ip_port) -> None:
        components = ip_port.split(":")
        port_config = TcpConfig(address=components[0], port=int(components[1]))
        await self._do_scan_port(port_config)

    def create_scan_tasks(self, ports) -> list:
        tasks = []
        name_template = "Search for devices using Fast Modbus %s"
        for serial_port in ports.serial:
            tasks.append(
                asyncio.create_task(
                    self._scan_serial_port(serial_port),
                    name=name_template % serial_port,
                )
            )
        for tcp_port in ports.tcp:
            tasks.append(asyncio.create_task(self._scan_tcp_port(tcp_port), name=name_template % tcp_port))

        return tasks
