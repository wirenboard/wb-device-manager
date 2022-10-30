#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import enum
from wb_modbus import minimalmodbus, instruments
# from . import logger


class ExtendedMBusScanner:
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

    def __init__(self, port, instrument=instruments.SerialRPCBackendInstrument):
        self.instrument = instrument(port=port, slaveaddress=self.ADDR)
        self.instrument.serial.timeout = 0.5
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

    def _communicate(self, request):
        number_of_bytes_to_read = 1000  # we need relatively huge one
        ret = self.instrument._communicate(
            request=request,
            number_of_bytes_to_read=number_of_bytes_to_read
        )

        ret = minimalmodbus._hexencode(ret)

        while ret.startswith("FF"):
            ret = ret[2:]
        ret = ret.strip("0")
        return minimalmodbus._hexdecode(ret)

    def init_bus_scan(self):
        # logger.debug("Init bus scan")
        request = self._build_request(cmd_code=self.CMDS.scan_init)
        try:
            self._communicate(request=request)
        except minimalmodbus.MasterReportedException:
            pass

    def get_next_device_data(self):  # TODO: maybe iterator?
        request = self._build_request(cmd_code=self.CMDS.single_scan)
        ret = self._communicate(request=request)
        response = self._parse_response(response_bytestr=ret)
        fcode = ord(response[0])
        hex_response = minimalmodbus._hexencode(response)

        if fcode == self.CMDS.single_reply:
            # logger.debug("Scanned: %s", str(hex_response))
            return response[1:]
        elif fcode == self.CMDS.scan_end:
            # logger.debug("Scan finished: %s", str(hex_response))
            return None
        else:
            raise minimalmodbus.InvalidResponseError(
                "Parsed payload {!r} is incorrect: should begin with one of {}".format(
                    hex_response, [self.CMDS.single_reply, self.CMDS.scan_end]
                )
            )

    def scan_bus(self, baudrate=9600, parity="N", stopbits=2):
        scanned = []

        uart_params = dict(locals())
        # logger.debug("Scanning %s %s", self.port, str(uart_params))
        self.instrument.serial.apply_settings(uart_params)

        self.init_bus_scan()
        sn_slaveid = self.get_next_device_data()
        while sn_slaveid is not None:
            slaveid, sn = self._parse_device_data(sn_slaveid)
            # logger.debug("Got device: %d %d", slaveid, sn)
            scanned.append((slaveid, sn))
            sn_slaveid = self.get_next_device_data()

        return scanned


if __name__ == "__main__":
    ports = ["/dev/ttyRS485-1",]
    for port in ports:
        scanner = ExtendedMBusScanner(port)
        print(scanner.scan_bus())
