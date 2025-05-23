#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import random
import re
from binascii import unhexlify

from wb_modbus import bindings, minimalmodbus

from . import logger, mqtt_rpc

WBMAP_MARKER = re.compile(r"\S*MAP\d+\S*")  # *MAP%d* matches


def fix_sn(device_model: str, raw_sn: int) -> int:
    """
    Raw value in registers should be adjusted to get the actual serial number.
    For example, WB-MAP* devices use only 25 bits for serial number and higher bits are set to 1.

    Args:
        device_model (str): The model of the device.
        raw_sn (int): Raw serial number of the device read from registers.

    Returns:
        int: The adjusted serial number.

    """
    # WB-MAP* uses 25 bit for serial number
    if WBMAP_MARKER.match(device_model):
        return raw_sn - 0xFE000000
    return raw_sn


def get_parity_from_register_value(value: int) -> str:
    return {0: "N", 1: "O", 2: "E"}.get(value, "-")


def get_baud_rate_from_register_value(value: int) -> int:
    return value * 100


class WBModbusScanner:
    def __init__(self, port, rpc_client):
        self.port = port
        self.rpc_client = rpc_client
        self.instrument_cls = mqtt_rpc.AsyncModbusInstrument
        self.modbus_wrapper = WBAsyncModbus
        self.uart_params_mapping = {}  # to store at scan-moment

    async def get_serial_number(self, slaveid, uart_params) -> str:
        instrument = self.instrument_cls(self.port, slaveid, self.rpc_client)
        instrument.serial.apply_settings(uart_params)  # must(!) be in the same coro as _communicate

        reg = bindings.WBModbusDeviceBase.COMMON_REGS_MAP["serial_number"]
        number_of_regs = 2
        payload = minimalmodbus._num_to_twobyte_string(  # pylint: disable=protected-access
            reg
        ) + minimalmodbus._num_to_twobyte_string(  # pylint: disable=protected-access
            number_of_regs
        )
        request = minimalmodbus._embed_payload(  # pylint: disable=protected-access
            slaveaddress=slaveid, mode=minimalmodbus.MODE_RTU, functioncode=3, payloaddata=payload
        )
        number_of_bytes_to_read = minimalmodbus._predict_response_size(  # pylint: disable=protected-access
            mode=minimalmodbus.MODE_RTU, functioncode=3, payload_to_slave=payload
        )
        response = await instrument._communicate(  # pylint: disable=protected-access
            request=request, number_of_bytes_to_read=number_of_bytes_to_read
        )
        payloaddata = minimalmodbus._extract_payload(  # pylint: disable=protected-access
            response=response,
            slaveaddress=slaveid,
            mode=minimalmodbus.MODE_RTU,
            functioncode=3,
        )
        sn = minimalmodbus._bytestring_to_long(  # pylint: disable=protected-access
            bytestring=payloaddata[1:],
            signed=False,
            number_of_registers=2,
            byteorder=minimalmodbus.BYTEORDER_BIG,
        )
        return sn

    async def scan_bus(self, baudrate=9600, parity="N", stopbits=2):
        uart_params = {"baudrate": baudrate, "parity": parity, "stopbits": stopbits}
        logger.debug("Scanning %s %s", self.port, str(uart_params))

        for slaveid in random.sample(range(1, 247), 246):
            try:
                sn = await self.get_serial_number(slaveid, uart_params)
                sn = str(sn)
                logger.info("Got device: %d %s %s", slaveid, sn, str(uart_params))
                self.uart_params_mapping.update(
                    {(slaveid, sn): uart_params}
                )  # we need context-independent uart params for actual device
                yield slaveid, sn
            except minimalmodbus.ModbusException as e:
                logger.debug("Modbus error: %s", e)

    def get_mb_connection(  # pylint: disable=too-many-arguments
        self, addr, port, baudrate=9600, parity="N", stopbits=1
    ):
        conn = self.modbus_wrapper(
            addr,
            port=port,
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            rpc_client=self.rpc_client,
            instrument=self.instrument_cls,
        )
        return conn

    async def get_uart_params(self, slaveid, sn):
        ret = self.uart_params_mapping.get((slaveid, sn), {})
        return ret.get("baudrate", "-"), ret.get("parity", "-"), ret.get("stopbits", "-")


class WBModbusScannerTCP(WBModbusScanner):
    def __init__(self, port, rpc_client):
        super().__init__(port, rpc_client)
        self.instrument_cls = mqtt_rpc.AsyncModbusInstrumentTCP

    async def get_uart_params(self, slaveid, sn):
        bd, parity, stopbits = await self.get_mb_connection(slaveid, self.port).read_u16_regs(110, 3)
        return get_baud_rate_from_register_value(bd), get_parity_from_register_value(parity), stopbits


class WBAsyncModbus:
    def __init__(  # pylint: disable=too-many-arguments
        self, addr, port, baudrate, parity, stopbits, rpc_client, instrument=mqtt_rpc.AsyncModbusInstrument
    ):
        self.device = instrument(port, addr, rpc_client)
        self.addr = addr
        self.port = port
        self.uart_params = {"baudrate": baudrate, "parity": parity, "stopbits": stopbits}

    def _make_payload(self, reg, number_of_regs):
        return minimalmodbus._num_to_twobyte_string(  # pylint: disable=protected-access
            reg
        ) + minimalmodbus._num_to_twobyte_string(  # pylint: disable=protected-access
            number_of_regs
        )

    def _build_request(self, funcode, reg, number_of_regs=1):
        payload = self._make_payload(reg, number_of_regs)
        request = minimalmodbus._embed_payload(  # pylint: disable=protected-access
            slaveaddress=self.addr, mode=minimalmodbus.MODE_RTU, functioncode=funcode, payloaddata=payload
        )
        return request

    def _predict_response_length(self, funcode, payload):
        return minimalmodbus._predict_response_size(  # pylint: disable=protected-access
            mode=minimalmodbus.MODE_RTU, functioncode=funcode, payload_to_slave=payload
        )

    def _parse_response(  # pylint: disable=too-many-arguments
        self, funcode, reg, number_of_regs, response_bytestr, payloadformat
    ):
        payloaddata = minimalmodbus._extract_payload(  # pylint: disable=protected-access
            response=response_bytestr,
            slaveaddress=self.addr,
            mode=minimalmodbus.MODE_RTU,
            functioncode=funcode,
        )

        return minimalmodbus._parse_payload(  # pylint: disable=protected-access
            payload=payloaddata,
            functioncode=funcode,
            registeraddress=reg,
            value=None,
            number_of_decimals=None,
            number_of_registers=number_of_regs,
            number_of_bits=number_of_regs,
            signed=None,
            byteorder=None,
            payloadformat=payloadformat,
        )

    def _str_to_wb(self, string):
        ret = minimalmodbus._hexencode(string, insert_spaces=True)  # pylint: disable=protected-access
        for placeholder in ("00", "FF", " "):  # Clearing a string to only meaningful bytes
            ret = ret.replace(placeholder, "")  # 'A1B2C3' bytes-only string
        return str(unhexlify(ret).decode(encoding="utf-8", errors="backslashreplace")).strip()

    async def _do_read(self, payloadformat, first_addr, regs_length=1):
        funcode = 3

        request = self._build_request(funcode=funcode, reg=first_addr, number_of_regs=regs_length)

        number_of_bytes_to_read = self._predict_response_length(
            funcode=funcode, payload=self._make_payload(first_addr, regs_length)
        )

        self.device.serial.apply_settings(self.uart_params)  # must(!) be in the same coro as _communicate
        response = await self.device._communicate(  # pylint: disable=protected-access
            request, number_of_bytes_to_read
        )

        ret = self._parse_response(
            funcode=funcode,
            reg=first_addr,
            number_of_regs=regs_length,
            response_bytestr=response,
            payloadformat=payloadformat,
        )
        return ret

    async def read_string(self, first_addr, regs_length):
        ret = await self._do_read(  # pylint: disable=protected-access
            minimalmodbus._PAYLOADFORMAT_STRING, first_addr, regs_length  # pylint: disable=protected-access
        )
        return self._str_to_wb(ret)

    async def read_u16_regs(self, first_addr, regs_length):
        return await self._do_read(  # pylint: disable=protected-access
            minimalmodbus._PAYLOADFORMAT_REGISTERS,  # pylint: disable=protected-access
            first_addr,
            regs_length,
        )
