#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import enum
from wb_modbus import minimalmodbus, instruments
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

    def __init__(self, port, instrument=mqtt_rpc.AsyncModbusInstrument):
        self.instrument = instrument(port=port, slaveaddress=self.ADDR)
        self.port = port

    def _build_request(self, cmd_code):
        payload = minimalmodbus.embed_payload(
            slaveaddress=self.ADDR,
            mode=minimalmodbus.MODE_RTU,
            functioncode=self.MODE,
            payloaddata=minimalmodbus.num_to_onebyte_string(cmd_code)
        )
        return payload

    def _parse_response(self, response_bytestr):
        payloaddata = minimalmodbus.extract_payload(
            response=response_bytestr,
            slaveaddress=self.ADDR,
            mode=minimalmodbus.MODE_RTU,
            functioncode=self.MODE
        )
        return payloaddata

    def _parse_device_data(self, device_data_bytestr):
        sn, slaveid = device_data_bytestr[:4], device_data_bytestr[4:]
        sn = minimalmodbus.bytestring_to_long(  # u32, 4 bytes
            bytestring=sn,
            signed=False,
            number_of_registers=2,
            byteorder=minimalmodbus.BYTEORDER_BIG
        )
        slaveid = ord(slaveid)  # 1 byte
        return slaveid, sn

    async def _communicate(self, request):
        number_of_bytes_to_read = 1000  # we need relatively huge one
        ret = await self.instrument._communicate(
            request=request,
            number_of_bytes_to_read=number_of_bytes_to_read
        )

        ret = minimalmodbus.hexencode(ret)

        while ret.startswith("FF"):  # TODO: we don't know actual beginning of response; maybe search by RE?
            ret = ret[2:]
        ret = ret.strip("0")  # TODO: needs fixup in wb-mqtt-serial
        return minimalmodbus.hexdecode(ret)

    async def init_bus_scan(self):
        logger.debug("Init bus scan")
        request = self._build_request(cmd_code=self.CMDS.scan_init)
        try:
            await self._communicate(request=request)
        except minimalmodbus.MasterReportedException:
            pass  # devices not answer to broadcast init-scan command

    async def get_next_device_data(self):
        request = self._build_request(cmd_code=self.CMDS.single_scan)
        ret = await self._communicate(request=request)
        response = self._parse_response(response_bytestr=ret)
        fcode = ord(response[0])
        hex_response = minimalmodbus.hexencode(response)

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

    async def scan_bus(self, baudrate=9600, parity="N", stopbits=2, response_timeout=0.5):
        uart_params = {
            "baudrate" : baudrate,
            "parity" : parity,
            "stopbits" : stopbits
        }

        logger.debug("Scanning %s %s response timeout: %.2f", self.port, str(uart_params), response_timeout)
        self.instrument.serial.apply_settings(uart_params)
        self.instrument.serial.timeout = response_timeout

        await self.init_bus_scan()

        sn_slaveid = await self.get_next_device_data()
        while sn_slaveid is not None:
            slaveid, sn = self._parse_device_data(sn_slaveid)
            logger.debug("Got device: %d %d", slaveid, sn)
            yield slaveid, sn
            sn_slaveid = await self.get_next_device_data()


if __name__ == "__main__":
    ports = ["/dev/ttyRS485-1",]
    for port in ports:
        scanner = WBExtendedModbusScanner(port)
        for device in scanner.scan_bus():
            print(device)
