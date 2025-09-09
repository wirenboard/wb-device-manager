#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint: disable=duplicate-code
import asyncio
import time
from enum import Enum
from typing import Union

from . import logger
from .bus_scan_state import (
    BusScanStateManager,
    DeviceInfo,
    Firmware,
    ParsedPorts,
    Port,
    SerialParams,
    get_uart_params_count,
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


class FastModbusCommand(Enum):
    ACTUAL = 0x46
    DEPRECATED = 0x60


async def port_scan(
    rpc_client: SRPCClient,
    port_config: Union[SerialConfig, TcpConfig],
    fast_modbus_command: FastModbusCommand,
    start: bool = True,
) -> dict:
    rpc_request = {
        "total_timeout": DEFAULT_RPC_CALL_TIMEOUT_MS,
        "command": fast_modbus_command.value,
        "mode": "start" if start else "next",
    }
    add_port_config_to_rpc_request(rpc_request, port_config)

    return await rpc_client.make_rpc_call(
        driver="wb-mqtt-serial",
        service="port",
        method="Scan",
        params=rpc_request,
        timeout=DEFAULT_RPC_CALL_TIMEOUT_MS / 1000,
    )


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

    async def _do_scan(
        self, port_config: Union[SerialConfig, TcpConfig], fast_modbus_command: FastModbusCommand
    ) -> None:  # pylint: too-many-locals
        debug_str = str(port_config)
        try:
            start = True
            while True:
                res = await port_scan(self._rpc_client, port_config, fast_modbus_command, start)
                devices = res.get("devices", [])
                if not devices:
                    break
                for device in devices:
                    sn = device.get("sn")
                    fw = Firmware(**device.get("fw", {}))
                    fw.ext_support = True
                    fw.fast_modbus_command = fast_modbus_command.value
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
                        configured_device_type=device.get("configured_device_type"),
                        last_seen=int(time.time()),
                        cfg=SerialParams(**device.get("cfg", {})),
                        fw=fw,
                    )
                    errors = device.get("errors", [])
                    for error in errors:
                        device_info.errors.append(StateError(**error))

                    await self._scanner_state.found_device(sn, device_info)
                if "error" in res:
                    logger.error("Fast Modbus search error %s: %s", debug_str, res["error"])
                    await self._scanner_state.add_error_port(debug_str)
                    break
                start = False
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.exception("Unhandled exception during Fast Modbus search %s: %s", debug_str, err)
            await self._scanner_state.add_error_port(debug_str)

    async def _do_scan_port(
        self, port_config: Union[SerialConfig, TcpConfig], fast_modbus_command: FastModbusCommand
    ) -> None:
        debug_str = str(port_config)
        logger.debug("Searching %s for devices using Fast Modbus", debug_str)
        await self._scanner_state.add_scanning_port(debug_str, is_ext_scan=True)
        await self._do_scan(port_config, fast_modbus_command)
        await self._scanner_state.remove_scanning_port(debug_str)

    async def _scan_serial_port(self, port, fast_modbus_command: FastModbusCommand) -> None:
        port_config = SerialConfig(path=port)

        # New firmwares can work with any stopbits, but old ones can't
        # Since it doesn't matter what to use, let's use 2
        for bd, parity, stopbits in self._serial_port_configs_generator(stopbits=[2]):
            port_config.baud_rate = bd
            port_config.parity = parity
            port_config.stop_bits = stopbits
            await self._do_scan_port(port_config, fast_modbus_command)

    async def _scan_tcp_port(self, ip_port, fast_modbus_command: FastModbusCommand) -> None:
        components = ip_port.split(":")
        port_config = TcpConfig(address=components[0], port=int(components[1]))
        await self._do_scan_port(port_config, fast_modbus_command)

    def create_scan_tasks(self, ports: ParsedPorts, fast_modbus_command: FastModbusCommand) -> list:
        tasks = []
        name_template = "Search for devices using Fast Modbus %s"
        for serial_port in ports.serial:
            tasks.append(
                asyncio.create_task(
                    self._scan_serial_port(serial_port, fast_modbus_command),
                    name=name_template % serial_port,
                )
            )
        for tcp_port in ports.tcp:
            tasks.append(
                asyncio.create_task(
                    self._scan_tcp_port(tcp_port, fast_modbus_command), name=name_template % tcp_port
                )
            )
        # Fast modbus scan for modbus_tcp ports is not supported
        return tasks

    def get_scan_items_count(self, ports: ParsedPorts) -> int:
        return len(ports.serial) * get_uart_params_count(stopbits=[2]) + len(ports.tcp)
