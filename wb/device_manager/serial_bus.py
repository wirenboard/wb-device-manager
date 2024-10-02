#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import enum
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
        payload = minimalmodbus._num_to_twobyte_string(reg) + minimalmodbus._num_to_twobyte_string(
            number_of_regs
        )
        request = minimalmodbus._embed_payload(
            slaveaddress=slaveid, mode=minimalmodbus.MODE_RTU, functioncode=3, payloaddata=payload
        )
        number_of_bytes_to_read = minimalmodbus._predict_response_size(
            mode=minimalmodbus.MODE_RTU, functioncode=3, payload_to_slave=payload
        )
        response = await instrument._communicate(
            request=request, number_of_bytes_to_read=number_of_bytes_to_read
        )
        payloaddata = minimalmodbus._extract_payload(
            response=response,
            slaveaddress=slaveid,
            mode=minimalmodbus.MODE_RTU,
            functioncode=3,
        )
        sn = minimalmodbus._bytestring_to_long(
            bytestring=payloaddata[1:],
            signed=False,
            number_of_registers=2,
            byteorder=minimalmodbus.BYTEORDER_BIG,
        )
        return sn

    async def scan_bus(self, baudrate=9600, parity="N", stopbits=2, cancel_condition=None):
        uart_params = {"baudrate": baudrate, "parity": parity, "stopbits": stopbits}
        logger.debug("Scanning %s %s", self.port, str(uart_params))

        for slaveid in random.sample(range(1, 247), 246):
            if cancel_condition is not None and cancel_condition.should_cancel():
                return
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

    def get_mb_connection(self, addr, port, baudrate=9600, parity="N", stopbits=1):
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
        return bd * 100, get_parity_from_register_value(parity), stopbits


class WBExtendedModbusWrapper:
    ADDR = 0xFD
    MODE = 0x60

    CMDS = enum.IntEnum(
        value="CMDS",
        names=[
            ("scan_init", 0x01),
            ("single_scan", 0x02),
            ("single_reply", 0x03),
            ("scan_end", 0x04),
            ("standart_send", 0x08),
            ("standart_reply", 0x09),
        ],
    )

    def build_request(self, cmd_code, payloaddata=""):
        """
        payloaddata could be scan* cmd or standart modbus pdu
        """
        payload = minimalmodbus._embed_payload(
            slaveaddress=self.ADDR,
            mode=minimalmodbus.MODE_RTU,
            functioncode=self.MODE,
            payloaddata=minimalmodbus._num_to_onebyte_string(cmd_code) + payloaddata,
        )
        return payload

    def parse_response(self, response_bytestr):
        payloaddata = minimalmodbus._extract_payload(
            response=response_bytestr,
            slaveaddress=self.ADDR,
            mode=minimalmodbus.MODE_RTU,
            functioncode=self.MODE,
        )
        return payloaddata


class WBExtendedModbusScanner(WBModbusScanner):
    def __init__(self, port, rpc_client):
        super().__init__(port, rpc_client)
        self.extended_modbus_wrapper = WBExtendedModbusWrapper()
        self.instrument_cls = mqtt_rpc.AsyncModbusInstrument
        self.instrument = self.instrument_cls(self.port, self.extended_modbus_wrapper.ADDR, self.rpc_client)
        self.modbus_wrapper = WBAsyncExtendedModbus

    def _parse_device_data(self, device_data_bytestr) -> (int, str):
        sn, slaveid = device_data_bytestr[:4], device_data_bytestr[4:]
        sn = minimalmodbus._bytestring_to_long(  # u32, 4 bytes
            bytestring=sn, signed=False, number_of_registers=2, byteorder=minimalmodbus.BYTEORDER_BIG
        )
        slaveid = ord(slaveid)  # 1 byte
        return slaveid, str(sn)

    def _extract_response(self, plain_response_str):
        """
        Typical plain_response_str looks like: ffffffff<response>00000000
        """
        mat = re.match(
            "^.*?(?P<header>%X%X)(?P<cmd>[0-9A-F][0-9A-F]).+"
            % (self.extended_modbus_wrapper.ADDR, self.extended_modbus_wrapper.MODE),
            plain_response_str,
        )
        if mat:
            fcode = int(mat.group("cmd"), 16)
            response_beginning = mat.span("header")[0]
            payload_beginning = mat.span("cmd")[-1]
            sn_slaveid_len, crc_len = 5, 2
            payload_bytelen = (
                sn_slaveid_len + crc_len
                if fcode == self.extended_modbus_wrapper.CMDS.single_reply
                else crc_len
            )
            return plain_response_str[response_beginning : payload_beginning + payload_bytelen * 2]
        else:
            raise minimalmodbus.InvalidResponseError(
                "Failed to extract correct response! Plain response: %s", plain_response_str
            )

    def _get_arbitration_timeout(self, bd):
        return self.instrument.calculate_minimum_silent_period_s(bd)

    async def _communicate(self, request, uart_params={"baudrate": 9600, "parity": "N", "stopbits": 1}):
        self.instrument.serial.apply_settings(uart_params)  # must(!) be in the same coro as _communicate
        number_of_bytes_to_read = 1000  # we need relatively huge one
        ret = await self.instrument._communicate(
            request=request, number_of_bytes_to_read=number_of_bytes_to_read
        )

        ret = minimalmodbus._hexencode(ret)
        ret = self._extract_response(ret)
        return minimalmodbus._hexdecode(ret)

    async def get_next_device_data(self, cmd_code, uart_params):
        request = self.extended_modbus_wrapper.build_request(cmd_code)
        ret = await self._communicate(request=request, uart_params=uart_params)
        response = self.extended_modbus_wrapper.parse_response(response_bytestr=ret)
        fcode = ord(response[0])
        hex_response = minimalmodbus._hexencode(response)

        if fcode == self.extended_modbus_wrapper.CMDS.single_reply:
            logger.debug("Scanned: %s", str(hex_response))
            return response[1:]
        elif fcode == self.extended_modbus_wrapper.CMDS.scan_end:
            logger.debug("Scan finished: %s", str(hex_response))
            return None
        else:
            raise minimalmodbus.InvalidResponseError(
                "Parsed payload {!r} is incorrect: should begin with one of {}".format(
                    hex_response,
                    [
                        self.extended_modbus_wrapper.CMDS.single_reply,
                        self.extended_modbus_wrapper.CMDS.scan_end,
                    ],
                )
            )

    async def scan_bus(self, baudrate=9600, parity="N", stopbits=2, cancel_condition=None):
        uart_params = {"baudrate": baudrate, "parity": parity, "stopbits": stopbits}

        response_timeout = self._get_arbitration_timeout(baudrate)
        self.instrument.serial.timeout = response_timeout
        logger.debug("Scanning %s %s response timeout: %.2f", self.port, str(uart_params), response_timeout)

        sn_slaveid = await self.get_next_device_data(
            cmd_code=self.extended_modbus_wrapper.CMDS.scan_init, uart_params=uart_params
        )
        while sn_slaveid is not None:
            if cancel_condition is not None and cancel_condition.should_cancel():
                return
            slaveid, sn = self._parse_device_data(sn_slaveid)
            logger.info("Got device: %d %s %s", slaveid, sn, str(uart_params))
            self.uart_params_mapping.update(
                {(slaveid, sn): uart_params}
            )  # we need context-independent uart params for actual device
            yield slaveid, sn
            sn_slaveid = await self.get_next_device_data(
                cmd_code=self.extended_modbus_wrapper.CMDS.single_scan, uart_params=uart_params
            )


class WBExtendedModbusScannerTCP(WBExtendedModbusScanner):
    def __init__(self, port, rpc_client):
        super().__init__(port, rpc_client)
        self.instrument_cls = mqtt_rpc.AsyncModbusInstrumentTCP
        self.instrument = self.instrument_cls(self.port, self.extended_modbus_wrapper.ADDR, self.rpc_client)

    async def get_uart_params(self, slaveid, sn):
        slaveid = int(sn)  # wb-extended modbus
        bd, parity, stopbits = await self.get_mb_connection(slaveid, self.port).read_u16_regs(110, 3)
        return bd * 100, get_parity_from_register_value(parity), stopbits


class WBAsyncModbus:
    def __init__(
        self, addr, port, baudrate, parity, stopbits, rpc_client, instrument=mqtt_rpc.AsyncModbusInstrument
    ):
        self.device = instrument(port, addr, rpc_client)
        self.addr = addr
        self.port = port
        self.uart_params = {"baudrate": baudrate, "parity": parity, "stopbits": stopbits}

    def _make_payload(self, reg, number_of_regs):
        return minimalmodbus._num_to_twobyte_string(reg) + minimalmodbus._num_to_twobyte_string(
            number_of_regs
        )

    def _build_request(self, funcode, reg, number_of_regs=1):
        payload = self._make_payload(reg, number_of_regs)
        request = minimalmodbus._embed_payload(
            slaveaddress=self.addr, mode=minimalmodbus.MODE_RTU, functioncode=funcode, payloaddata=payload
        )
        return request

    def _predict_response_length(self, funcode, payload):
        return minimalmodbus._predict_response_size(
            mode=minimalmodbus.MODE_RTU, functioncode=funcode, payload_to_slave=payload
        )

    def _parse_response(self, funcode, reg, number_of_regs, response_bytestr, payloadformat):
        payloaddata = minimalmodbus._extract_payload(
            response=response_bytestr,
            slaveaddress=self.addr,
            mode=minimalmodbus.MODE_RTU,
            functioncode=funcode,
        )

        return minimalmodbus._parse_payload(
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
        ret = minimalmodbus._hexencode(string, insert_spaces=True)
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
        response = await self.device._communicate(request, number_of_bytes_to_read)

        ret = self._parse_response(
            funcode=funcode,
            reg=first_addr,
            number_of_regs=regs_length,
            response_bytestr=response,
            payloadformat=payloadformat,
        )
        return ret

    async def read_string(self, first_addr, regs_length):
        ret = await self._do_read(minimalmodbus._PAYLOADFORMAT_STRING, first_addr, regs_length)
        return self._str_to_wb(ret)

    async def read_u16_regs(self, first_addr, regs_length):
        return await self._do_read(minimalmodbus._PAYLOADFORMAT_REGISTERS, first_addr, regs_length)


class WBAsyncExtendedModbus(WBAsyncModbus):
    """
    Standart modbus frame is wrapped into wb-extension
    Look at https://github.com/wirenboard/libwbmcu-modbus/blob/master/wbm_ext.md for more info
    """

    def __init__(
        self, sn, port, baudrate, parity, stopbits, rpc_client, instrument=mqtt_rpc.AsyncModbusInstrument
    ):
        dummy_slaveid = 0  # for compatibility with minimalmodbus checks
        super().__init__(dummy_slaveid, port, baudrate, parity, stopbits, rpc_client, instrument)
        self.serial_number = sn
        self.extended_modbus_wrapper = WBExtendedModbusWrapper()

    def _build_request(self, funcode, reg, number_of_regs=1):
        """
        Wrapping standart modbus frame into a wb modbus extension
        """
        standart_frame = minimalmodbus._hexencode(super()._build_request(funcode, reg, number_of_regs))
        standart_pdu = minimalmodbus._hexdecode(standart_frame[2:-4])

        sn = minimalmodbus._long_to_bytestring(
            value=self.serial_number,
            byteorder=minimalmodbus.BYTEORDER_BIG,
            number_of_registers=2,
            signed=False,
        )

        return self.extended_modbus_wrapper.build_request(
            cmd_code=self.extended_modbus_wrapper.CMDS.standart_send, payloaddata=sn + standart_pdu
        )

    def _predict_response_length(self, funcode, payload):
        """
        WB-extended modbus frame is always fixed-bytes longer, than a standart modbus frame

        Byte-sizes:
        broadcast_addr, extended_modbus_cmd, standart_modbus_emulation_cmd, serial_number, standart_modbus_slaveid
        """
        wb_extension_additional_bytes = 1 + 1 + 1 + 4 - 1  # (4-byte sn is like slaveid (1-byte) => 4-1)
        return super()._predict_response_length(funcode, payload) + wb_extension_additional_bytes

    def _parse_response(self, funcode, reg, number_of_regs, response_bytestr, payloadformat):
        """
        Extracting standart modbus payload from extended-modbus frame
        """
        extended_modbus_pdu = self.extended_modbus_wrapper.parse_response(response_bytestr)
        sn, pdu = extended_modbus_pdu[1:5], extended_modbus_pdu[5:]
        fcode, payload = pdu[:1], pdu[1:]  # standart modbus

        sn = minimalmodbus._bytestring_to_long(
            bytestring=sn, byteorder=minimalmodbus.BYTEORDER_BIG, number_of_registers=2, signed=False
        )

        if self.serial_number != sn:
            raise minimalmodbus.InvalidResponseError(
                "Parsed sn (%d) != assumed (%d). Plain response: %s"
                % (sn, self.serial_number, minimalmodbus._hexencode(response_bytestr))
            )

        minimalmodbus._check_response_slaveerrorcode(minimalmodbus._num_to_onebyte_string(0) + pdu)

        return minimalmodbus._parse_payload(
            payload=payload,
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
