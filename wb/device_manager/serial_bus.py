#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import enum
from binascii import unhexlify
from wb_modbus import minimalmodbus
from . import logger, mqtt_rpc


class WBExtendedModbusScanner:
    ADDR = 0xfd
    MODE = 0x60

    CMDS = enum.IntEnum(
        value="CMDS",
        names=[
            ("scan_init", 0x01),
            ("single_scan", 0x02),
            ("single_reply", 0x03),
            ("scan_end", 0x04)
        ]
    )

    def __init__(self, port, rpc_client):
        self.instrument = mqtt_rpc.AsyncModbusInstrument(port, self.ADDR, rpc_client)
        self.port = port

    def _build_request(self, cmd_code):
        payload = minimalmodbus._embed_payload(
            slaveaddress=self.ADDR,
            mode=minimalmodbus.MODE_RTU,
            functioncode=self.MODE,
            payloaddata=minimalmodbus._num_to_onebyte_string(cmd_code)
        )
        return payload

    def _parse_response(self, response_bytestr):
        payloaddata = minimalmodbus._extract_payload(
            response=response_bytestr,
            slaveaddress=self.ADDR,
            mode=minimalmodbus.MODE_RTU,
            functioncode=self.MODE
        )
        return payloaddata

    def _parse_device_data(self, device_data_bytestr):
        sn, slaveid = device_data_bytestr[:4], device_data_bytestr[4:]
        sn = minimalmodbus._bytestring_to_long(  # u32, 4 bytes
            bytestring=sn,
            signed=False,
            number_of_registers=2,
            byteorder=minimalmodbus.BYTEORDER_BIG
        )
        slaveid = ord(slaveid)  # 1 byte
        return slaveid, sn

    def _extract_response(self, plain_response_str):
        """
        Typical plain_response_str looks like: ffffffff<response>00000000
        """
        mat = re.match(
            "^.*(FF)*(?P<header>%X%X)(?P<cmd>[0-9A-F][0-9A-F]).+" % (self.ADDR, self.MODE),
            plain_response_str
            )
        if mat:
            fcode = int(mat.group("cmd"), 16)
            response_beginning = mat.span("header")[0]
            payload_beginning = mat.span("cmd")[-1]
            sn_slaveid_len, crc_len = 5, 2
            payload_bytelen = sn_slaveid_len + crc_len if fcode == self.CMDS.single_reply else crc_len
            return plain_response_str[response_beginning : payload_beginning + payload_bytelen * 2]
        else:
            raise minimalmodbus.InvalidResponseError(
                "Failed to extract correct response! Plain response: %s", plain_response_str
                )

    def _get_arbitration_timeout(self, bd):
        return minimalmodbus._calculate_minimum_silent_period(bd)

    async def _communicate(
        self,
        request,
        uart_params={
            "baudrate" : 9600,
            "parity" : "N",
            "stopbits" : 1
        }
        ):
        self.instrument.serial.apply_settings(uart_params)
        number_of_bytes_to_read = 1000  # we need relatively huge one
        ret = await self.instrument._communicate(
            request=request,
            number_of_bytes_to_read=number_of_bytes_to_read
        )

        ret = minimalmodbus._hexencode(ret)
        ret = self._extract_response(ret)
        return minimalmodbus._hexdecode(ret)

    async def get_next_device_data(self, cmd_code, uart_params):
        request = self._build_request(cmd_code)
        ret = await self._communicate(request=request, uart_params=uart_params)
        response = self._parse_response(response_bytestr=ret)
        fcode = ord(response[0])
        hex_response = minimalmodbus._hexencode(response)

        if fcode == self.CMDS.single_reply:
            logger.debug("Scanned: %s", str(hex_response))
            return response[1:]
        elif fcode == self.CMDS.scan_end:
            logger.debug("Scan finished: %s", str(hex_response))
            return None
        else:
            raise minimalmodbus.InvalidResponseError(
                "Parsed payload {!r} is incorrect: should begin with one of {}".format(
                    hex_response, [self.CMDS.single_reply, self.CMDS.scan_end]
                )
            )

    async def scan_bus(self, baudrate=9600, parity="N", stopbits=2):
        uart_params = {
            "baudrate" : baudrate,
            "parity" : parity,
            "stopbits" : stopbits
        }

        response_timeout = self._get_arbitration_timeout(baudrate)
        self.instrument.serial.timeout = response_timeout
        logger.debug("Scanning %s %s response timeout: %.2f", self.port, str(uart_params), response_timeout)

        sn_slaveid = await self.get_next_device_data(cmd_code=self.CMDS.scan_init, uart_params=uart_params)
        while sn_slaveid is not None:
            slaveid, sn = self._parse_device_data(sn_slaveid)
            logger.debug("Got device: %d %d", slaveid, sn)
            yield slaveid, sn
            sn_slaveid = await self.get_next_device_data(cmd_code=self.CMDS.single_scan, uart_params=uart_params)


class WBAsyncModbus:
    def __init__(self, addr, port, baudrate, parity, stopbits, rpc_client, **kwargs):
        self.device = mqtt_rpc.AsyncModbusInstrument(port, addr, rpc_client)
        self.addr = addr
        self.port = port
        self.device.serial.apply_settings(
            {
                "baudrate" : baudrate,
                "parity" : parity,
                "stopbits" : stopbits
            }
        )

    def _make_payload(self, reg, number_of_regs):
        return minimalmodbus._num_to_twobyte_string(reg) + minimalmodbus._num_to_twobyte_string(
            number_of_regs
            )

    def _build_request(self, funcode, reg, number_of_regs=1):
        payload = self._make_payload(reg, number_of_regs)
        request = minimalmodbus._embed_payload(
            slaveaddress=self.addr,
            mode=minimalmodbus.MODE_RTU,
            functioncode=funcode,
            payloaddata=payload
            )
        return request

    def _parse_response(self, funcode, reg, number_of_regs, response_bytestr, payloadformat):
        payloaddata = minimalmodbus._extract_payload(
            response=response_bytestr,
            slaveaddress=self.addr,
            mode=minimalmodbus.MODE_RTU,
            functioncode=funcode
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
            payloadformat=payloadformat
            )

    def _str_to_wb(self, string):
        ret = minimalmodbus._hexencode(string, insert_spaces=True)
        for placeholder in ('00', 'FF', ' '):  # Clearing a string to only meaningful bytes
            ret = ret.replace(placeholder, '')  # 'A1B2C3' bytes-only string
        return str(unhexlify(ret).decode(encoding="utf-8", errors="backslashreplace")).strip()

    async def read_string(self, first_addr, regs_length):
        funcode = 3
        payloadformat = minimalmodbus._PAYLOADFORMAT_STRING

        request = self._build_request(
            funcode=funcode,
            reg=first_addr,
            number_of_regs=regs_length
            )

        number_of_bytes_to_read = minimalmodbus._predict_response_size(
            mode=minimalmodbus.MODE_RTU,
            functioncode=funcode,
            payload_to_slave=self._make_payload(first_addr, regs_length)
            )

        response = await self.device._communicate(request, number_of_bytes_to_read)

        ret = self._parse_response(
            funcode=funcode,
            reg=first_addr,
            number_of_regs=regs_length,
            response_bytestr=response,
            payloadformat=payloadformat
            )
        return self._str_to_wb(ret)
