#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import time
from typing import Optional

from mqttrpc import client as rpcclient
from wb_modbus import bindings, minimalmodbus

from . import logger, mqtt_rpc, serial_bus
from .bootloader_scan import BootloaderModeScanner
from .bus_scan_state import (
    BusScanStateManager,
    DeviceInfo,
    GenericStateError,
    ParsedPorts,
    Port,
    ReadDeviceSignatureDeviceError,
    ReadFWSignatureDeviceError,
    ReadFWVersionDeviceError,
    ReadSerialParamsDeviceError,
    RPCCallTimeoutStateError,
    ScanCancelCondition,
    SerialParams,
    get_all_uart_params,
    make_uuid,
)
from .serial_bus import fix_sn
from .serial_rpc import SerialRPCWrapper


class BusScanner:
    def __init__(self, mqtt_connection, rpc_client, asyncio_loop):
        self._rpc_client = rpc_client
        self._asyncio_loop = asyncio_loop
        self._bus_scanning_task = None
        self._bus_scanning_task_cancel_condition = None
        self._state_manager = BusScanStateManager(mqtt_connection, asyncio_loop)

    @property
    def rpc_client(self):
        return self._rpc_client

    @property
    def state_publish_topic(self):
        return self._state_manager.state_publish_topic

    @property
    def asyncio_loop(self):
        return self._asyncio_loop

    async def fill_device_info(self, device_info, mb_conn):
        errors = []

        err_ctx = None
        # Actual firmwares have 20 registers for device model, old ones have only 6, try to read both
        EXTENDED_DEVICE_MODEL_SIZE = 20
        for reg_len in [EXTENDED_DEVICE_MODEL_SIZE, bindings.WBModbusDeviceBase.DEVICE_SIGNATURE_LENGTH]:
            try:
                device_signature = await mb_conn.read_string(
                    first_addr=bindings.WBModbusDeviceBase.COMMON_REGS_MAP["device_signature"],
                    regs_length=reg_len,
                )
                device_info.device_signature = device_signature
                device_info.title = device_signature.strip("\x02")
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

    async def fill_serial_params(self, device_info, scanner):
        parities = {0: "N", 1: "O", 2: "E"}
        bd, parity, stopbits = "-", "-", "-"
        try:
            bd, parity, stopbits = await scanner.get_uart_params(device_info.cfg.slave_id, device_info.sn)
            parity = parities.get(parity, "-")
        except minimalmodbus.ModbusException:
            logger.exception("Failed to read serial params from device")
            device_info.errors.append(ReadSerialParamsDeviceError())

        device_info.cfg.baud_rate = bd
        device_info.cfg.parity = parity
        device_info.cfg.stop_bits = stopbits

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
        self._bus_scanning_task_cancel_condition = ScanCancelCondition()
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
            self._bus_scanning_task_cancel_condition.request_cancel()
            self._bus_scanning_task.cancel()
            try:
                await self._bus_scanning_task
            except asyncio.CancelledError:
                pass
            return "Ok"
        raise mqtt_rpc.MQTTRPCAlreadyProcessingException()

    def _create_scan_tasks(self, ports, is_extended=False):
        tasks = []
        name_template = "Extended scan %s %s" if is_extended else "Ordinary scan %s %s"
        for serial_port in ports.serial:
            tasks.append(
                asyncio.create_task(
                    self.scan_serial_port(serial_port, is_ext_scan=is_extended),
                    name=name_template % (serial_port, "serial"),
                )
            )
        for tcp_port in ports.tcp:
            tasks.append(
                asyncio.create_task(
                    self.scan_tcp_port(tcp_port, is_ext_scan=is_extended),
                    name=name_template % (tcp_port, "tcp"),
                )
            )

        return tasks

    async def scan_serial_bus(
        self, scan_type, preserve_old_results, port_config, out_of_order_slave_ids: list[int]
    ):
        await self._state_manager.reset(preserve_old_results)

        if isinstance(port_config, dict) and "path" in port_config:
            ports = ParsedPorts(serial=[port_config["path"]], tcp=[])
        else:
            try:
                ports = await self.get_ports()
            except mqtt_rpc.MQTTRPCCallTimeoutError:
                logger.exception("No answer from wb-mqtt-serial")
                await self._state_manager.scan_finished(RPCCallTimeoutStateError())
                return
        try:
            if scan_type == "extended":
                await asyncio.gather(
                    *self._create_scan_tasks(ports, is_extended=True), return_exceptions=True
                )
            elif scan_type == "standard":
                await asyncio.gather(
                    *self._create_scan_tasks(ports, is_extended=False), return_exceptions=True
                )
            elif scan_type == "bootloader":
                bootloader_mode_scanner = BootloaderModeScanner(
                    SerialRPCWrapper(self.rpc_client),
                    self._state_manager,
                    self._bus_scanning_task_cancel_condition,
                    get_all_uart_params,
                )
                await asyncio.gather(
                    *bootloader_mode_scanner.create_scan_tasks(ports, out_of_order_slave_ids),
                    return_exceptions=True,
                )
            else:
                await asyncio.gather(
                    *self._create_scan_tasks(ports, is_extended=True), return_exceptions=True
                )
                await asyncio.gather(
                    *self._create_scan_tasks(ports, is_extended=False), return_exceptions=True
                )

            await self._state_manager.scan_finished()
        except Exception as e:
            logger.exception("Unhandled exception during overall scan %s", e)
            await self._state_manager.scan_finished(GenericStateError())

    async def do_scan_port(
        self, path, scanner, is_ext_scan: bool, progress: Optional[int] = None, **scan_kwargs
    ) -> None:
        debug_str = path + " " + "-".join(map(str, scan_kwargs.values()))

        logger.debug(
            "Scanning %s (extended modbus: %r)",
            debug_str,
            is_ext_scan,
        )
        await self._state_manager.add_scanning_port(debug_str, is_ext_scan)

        try:
            async for slave_id, sn in scanner.scan_bus(
                **scan_kwargs, cancel_condition=self._bus_scanning_task_cancel_condition
            ):
                if self._bus_scanning_task_cancel_condition.should_cancel():
                    return
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
                device_info.fw.ext_support = is_ext_scan

                addr = int(device_info.sn) if is_ext_scan else device_info.cfg.slave_id
                mb_conn = scanner.get_mb_connection(addr, path, **scan_kwargs)

                # fill_device_info can modify sn
                # scanner searches port parameters based on cached sn
                # and can return empty values for modified sn
                # so need to call fill_serial_params first
                await self.fill_serial_params(device_info, scanner)
                await self.fill_device_info(device_info, mb_conn)

                await self._state_manager.found_device(sn, device_info)

        except minimalmodbus.NoResponseError:
            logger.debug("No %s-modbus devices on %s", "extended" if is_ext_scan else "ordinary", debug_str)
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
            await self._state_manager.remove_scanning_port(debug_str, progress)

    async def scan_serial_port(self, port, is_ext_scan=True):
        if is_ext_scan:
            scanner = serial_bus.WBExtendedModbusScanner(port, self.rpc_client)
        else:
            scanner = serial_bus.WBModbusScanner(port, self.rpc_client)

        # New firmwares can work with any stopbits, but old ones can't
        # Since it doesn't matter what to use, let's use 2
        allowed_stopbits = (
            [
                2,
            ]
            if is_ext_scan
            else [2, 1]
        )

        for bd, parity, stopbits, progress_percent in get_all_uart_params(stopbits=allowed_stopbits):
            if self._bus_scanning_task_cancel_condition.should_cancel():
                return
            await self.do_scan_port(
                port,
                scanner,
                is_ext_scan,
                baudrate=bd,
                parity=parity,
                stopbits=stopbits,
                progress=progress_percent,
            )

    async def scan_tcp_port(self, ip_port, is_ext_scan=True):
        if is_ext_scan:
            scanner = serial_bus.WBExtendedModbusScannerTCP(ip_port, self.rpc_client)
        else:
            scanner = serial_bus.WBModbusScannerTCP(ip_port, self.rpc_client)

        await self.do_scan_port(ip_port, scanner, is_ext_scan)
