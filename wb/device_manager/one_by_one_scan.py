#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import time

from mqttrpc import client as rpcclient
from wb_modbus import bindings, minimalmodbus

from . import logger, serial_bus
from .bus_scan_state import (
    BusScanStateManager,
    DeviceInfo,
    Port,
    SerialParams,
    get_all_uart_params,
    get_uart_params_count,
    make_uuid,
)
from .firmware_update import get_human_readable_device_model
from .mqtt_rpc import SRPCClient
from .serial_bus import fix_sn
from .state_error import (
    ReadDeviceSignatureDeviceError,
    ReadFWSignatureDeviceError,
    ReadFWVersionDeviceError,
    ReadSerialParamsDeviceError,
)


class OneByOneBusScanner:
    """
    A class that searches for Wiren Board Modbus devices by attempting
    to read serial number using all possible slave ids.
    """

    def __init__(self, state_manager: BusScanStateManager, rpc_client: SRPCClient):
        self._rpc_client = rpc_client
        self._state_manager = state_manager

    async def _fill_device_info(self, device_info, mb_conn):
        errors = []

        err_ctx = None
        # Actual firmwares have 20 registers for device model, old ones have only 6, try to read both
        EXTENDED_DEVICE_MODEL_SIZE = 20  # pylint: disable=invalid-name
        for reg_len in [EXTENDED_DEVICE_MODEL_SIZE, bindings.WBModbusDeviceBase.DEVICE_SIGNATURE_LENGTH]:
            try:
                device_signature = await mb_conn.read_string(
                    first_addr=bindings.WBModbusDeviceBase.COMMON_REGS_MAP["device_signature"],
                    regs_length=reg_len,
                )
                device_info.device_signature = get_human_readable_device_model(device_signature)
                device_info.title = device_info.device_signature
                err_ctx = None
                device_info.sn = str(fix_sn(device_info.device_signature, int(device_info.sn)))
                break
            except minimalmodbus.ModbusException as e:
                err_ctx = e
        if err_ctx:
            logger.error("Failed to read device signature", exc_info=err_ctx)
            errors.append(ReadDeviceSignatureDeviceError())

        try:
            device_info.fw_signature = await mb_conn.read_string(
                first_addr=bindings.WBModbusDeviceBase.COMMON_REGS_MAP["fw_signature"],
                regs_length=bindings.WBModbusDeviceBase.FIRMWARE_SIGNATURE_LENGTH,
            )
        except minimalmodbus.ModbusException:
            logger.exception("Failed to read fw_signature")
            errors.append(ReadFWSignatureDeviceError())

        try:
            device_info.fw.version = await mb_conn.read_string(
                first_addr=bindings.WBModbusDeviceBase.COMMON_REGS_MAP["fw_version"],
                regs_length=bindings.WBModbusDeviceBase.FIRMWARE_VERSION_LENGTH,
            )
        except minimalmodbus.ModbusException:
            logger.exception("Failed to read fw_version")
            errors.append(ReadFWVersionDeviceError())

        device_info.errors.extend(errors)

    async def _fill_serial_params(self, device_info, scanner):
        bd, parity, stopbits = "-", "-", "-"
        try:
            bd, parity, stopbits = await scanner.get_uart_params(device_info.cfg.slave_id, device_info.sn)
        except minimalmodbus.ModbusException:
            logger.exception("Failed to read serial params from device")
            device_info.errors.append(ReadSerialParamsDeviceError())

        device_info.cfg.baud_rate = bd
        device_info.cfg.parity = parity
        device_info.cfg.stop_bits = stopbits

    async def _do_scan_port(self, path, scanner, **scan_kwargs) -> None:
        debug_str = path + " " + "-".join(map(str, scan_kwargs.values()))
        logger.debug("Scanning %s", debug_str)

        try:
            await self._state_manager.add_scanning_port(debug_str, False)
            async for slave_id, sn in scanner.scan_bus(**scan_kwargs):
                if self._state_manager.is_device_found(sn):
                    logger.debug("Device %s already scanned; skipping", str(sn))
                    continue

                device_info = DeviceInfo(
                    uuid=make_uuid(sn),
                    title="Unknown",
                    sn=str(sn),
                    last_seen=int(time.time()),
                    port=Port(path),
                    cfg=SerialParams(slave_id=slave_id),
                )

                addr = device_info.cfg.slave_id
                mb_conn = scanner.get_mb_connection(addr, path, **scan_kwargs)

                # fill_device_info can modify sn
                # scanner searches port parameters based on cached sn
                # and can return empty values for modified sn
                # so need to call fill_serial_params first
                await self._fill_serial_params(device_info, scanner)
                await self._fill_device_info(device_info, mb_conn)

                await self._state_manager.found_device(sn, device_info)

        except minimalmodbus.NoResponseError:
            logger.debug("No modbus devices on %s", debug_str)
        except minimalmodbus.InvalidResponseError as err:
            logger.error("Invalid response during scan %s: %s", debug_str, err)
        except Exception as err:
            if isinstance(err, rpcclient.MQTTRPCError):
                logger.error("MQTT RPC error during scan %s: %s", debug_str, err)
            else:
                logger.exception("Unhandled exception during scan %s", debug_str)
            await self._state_manager.add_error_port(debug_str)
            raise
        finally:
            await self._state_manager.remove_scanning_port(debug_str)

    async def _scan_serial_port(self, port):
        scanner = serial_bus.WBModbusScanner(port, self._rpc_client)

        for bd, parity, stopbits in get_all_uart_params():
            await self._do_scan_port(port, scanner, baudrate=bd, parity=parity, stopbits=stopbits)

    async def _scan_tcp_port(self, ip_port):
        scanner = serial_bus.WBModbusScannerTCP(ip_port, self._rpc_client)
        await self._do_scan_port(ip_port, scanner)

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
