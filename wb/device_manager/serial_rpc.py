#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Union


class ModbusExceptionCode(Enum):
    ILLEGAL_FUNCTION = 1
    ILLEGAL_DATA_ADDRESS = 2
    ILLEGAL_DATA_VALUE = 3
    SLAVE_DEVICE_FAILURE = 4
    ACKNOWLEDGE = 5
    SLAVE_DEVICE_BUSY = 6
    NEGATIVE_ACKNOWLEDGE = 7
    MEMORY_PARITY_ERROR = 8
    GATEWAY_PATH_UNAVAILABLE = 10
    GATEWAY_TARGET_DEVICE_FAILED_TO_RESPOND = 11


class WBModbusException(Exception):
    code: int

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class SerialConfig:
    path: str
    baud_rate: int = 9600
    parity: str = "N"
    data_bits: int = 8
    stop_bits: int = 2

    def set_default_settings(self) -> None:
        self.baud_rate = 9600
        self.parity = "N"
        self.data_bits = 8
        self.stop_bits = 2

    def __str__(self) -> str:
        return self.path


@dataclass
class TcpConfig:
    address: str
    port: int

    def __str__(self) -> str:
        return f"{self.address}:{self.port}"


# Modbus function codes
class ModbusFunctionCode(Enum):
    READ_COILS = 0x1
    READ_DISCRETE = 0x2
    READ_HOLDING = 0x3
    READ_INPUT = 0x4
    WRITE_SINGLE_COIL = 0x5
    WRITE_SINGLE_REGISTER = 0x6
    WRITE_MULTIPLE_COILS = 0xF
    WRITE_MULTIPLE_REGISTERS = 0x10


# parameter config data types enum
class DataType(Enum):
    STR = "str"
    UINT = "uint"
    BYTES = "bytes"


@dataclass
class ParameterConfig:
    register_address: int
    register_count: int = 1
    read_fn: ModbusFunctionCode = ModbusFunctionCode.READ_HOLDING
    write_fn: ModbusFunctionCode = ModbusFunctionCode.WRITE_SINGLE_REGISTER
    data_type: DataType = DataType.UINT


WB_DEVICE_PARAMETERS = {
    "fw_signature": ParameterConfig(
        register_address=290,
        register_count=12,
        read_fn=ModbusFunctionCode.READ_HOLDING,
        write_fn=None,
        data_type=DataType.STR,
    ),
    "fw_version": ParameterConfig(
        register_address=250,
        register_count=16,
        read_fn=ModbusFunctionCode.READ_HOLDING,
        write_fn=None,
        data_type=DataType.STR,
    ),
    "bootloader_version": ParameterConfig(
        register_address=300,
        register_count=8,
        read_fn=ModbusFunctionCode.READ_HOLDING,
        data_type=DataType.STR,
    ),
    "reboot_to_bootloader_preserve_port_settings": ParameterConfig(
        register_address=131,
    ),
    "reboot_to_bootloader": ParameterConfig(
        register_address=129,
    ),
    "fw_info_block": ParameterConfig(
        register_address=0x1000,
        register_count=16,
        read_fn=None,
        write_fn=ModbusFunctionCode.WRITE_MULTIPLE_REGISTERS,
        data_type=DataType.BYTES,
    ),
    "fw_data_block": ParameterConfig(
        register_address=0x2000,
        register_count=68,
        read_fn=None,
        write_fn=ModbusFunctionCode.WRITE_MULTIPLE_REGISTERS,
        data_type=DataType.BYTES,
    ),
    "baud_rate": ParameterConfig(
        register_address=110,
    ),
    "parity": ParameterConfig(
        register_address=111,
    ),
    "stop_bits": ParameterConfig(
        register_address=112,
    ),
}


def value_to_bytes(register_data_type: DataType, value: Union[int, bytes]) -> bytes:
    if isinstance(value, bytes):
        return value

    if register_data_type == DataType.UINT and isinstance(value, int):
        return value.to_bytes(2, byteorder="big")

    raise ValueError(
        "Can't write value of type %s to parameter of type %s" % type(value), register_data_type.name
    )


def add_port_config_to_rpc_request(rpc_request, port_config):
    if isinstance(port_config, SerialConfig):
        rpc_request.update(asdict(port_config))
    else:
        rpc_request["ip"] = port_config.address
        rpc_request["port"] = port_config.port


class SerialRPCWrapper:

    def __init__(self, rpc_client):
        self.rpc_client = rpc_client

    def _calculate_default_response_timeout(self) -> float:
        """
        response_timeout (on mb_master side): roundtrip + device_processing + uart_processing
        """
        wb_devices_response_time_s = 8e-3  # devices with old fws
        onebyte_on_1200bd_s = 10e-3
        linux_uart_processing_s = 50e-3  # with huge upper reserve
        return wb_devices_response_time_s

    async def _communicate(
        self,
        port_config: Union[SerialConfig, TcpConfig],
        slave_id: int,
        function_code: int,
        register_address: int,
        register_count: int,
        data: bytes,
        response_timeout_s: float = None,
    ) -> bytes:
        rpc_call_timeout_ms = 10000
        if response_timeout_s is None:
            response_timeout_s = self._calculate_default_response_timeout()
        response_timeout_ms = round(response_timeout_s * 1000)

        rpc_request = {
            "slave_id": slave_id,
            "function": function_code,
            "address": register_address,
            "count": register_count,
            "response_timeout": response_timeout_ms,
            "total_timeout": rpc_call_timeout_ms,
            "protocol": "modbus",
            "format": "HEX",
        }
        add_port_config_to_rpc_request(rpc_request, port_config)
        if data is not None:
            rpc_request["format"] = "HEX"
            rpc_request["msg"] = data.hex()

        response = await self.rpc_client.make_rpc_call(
            driver="wb-mqtt-serial",
            service="port",
            method="Load",
            params=rpc_request,
            timeout=rpc_call_timeout_ms / 1000,
        )

        if "exception" in response:
            raise WBModbusException(response["exception"]["msg"], response["exception"]["code"])

        return bytes.fromhex(str(response.get("response", "")))

    async def read(
        self,
        port_config,
        slave_id,
        param_config: ParameterConfig,
        response_timeout_s: float = None,
    ):
        if param_config.read_fn is None:
            raise ValueError("Register %d is not readable" % param_config.register_address)

        response = await self._communicate(
            port_config,
            slave_id,
            param_config.read_fn.value,
            param_config.register_address,
            param_config.register_count,
            None,
            response_timeout_s,
        )
        if param_config.data_type == DataType.STR:
            return "".join(chr(byte) for byte in response[1::2]).strip("\x00\xFF")
        if param_config.data_type == DataType.UINT:
            return int.from_bytes(response, byteorder="big")
        return response

    async def write(
        self,
        port_config: Union[SerialConfig, TcpConfig],
        slave_id: int,
        param_config: ParameterConfig,
        value: Union[int, bytes],
        response_timeout_s: float = None,
    ) -> None:
        if param_config.write_fn is None:
            raise ValueError("Register %d is not writable" % param_config.register_address)

        data = value_to_bytes(param_config.data_type, value)

        register_count = param_config.register_count

        if (
            param_config.write_fn == ModbusFunctionCode.WRITE_MULTIPLE_REGISTERS
            and len(data) / 2 < param_config.register_count
        ):
            register_count = len(data) / 2
        await self._communicate(
            port_config,
            slave_id,
            param_config.write_fn.value,
            param_config.register_address,
            register_count,
            data,
            response_timeout_s,
        )
