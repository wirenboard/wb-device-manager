#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from dataclasses import asdict, dataclass
from enum import Enum, IntEnum
from typing import Optional, Union

from mqttrpc import client as rpcclient

from .mqtt_rpc import MQTTRPCErrorCode, SRPCClient


class ModbusExceptionCode(IntEnum):
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


class ModbusProtocol(Enum):
    MODBUS_RTU = "modbus"
    MODBUS_TCP = "modbus-tcp"


class SerialExceptionBase(Exception):
    pass


class WBModbusException(SerialExceptionBase):
    code: int

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


class SerialCommunicationException(SerialExceptionBase):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class SerialTimeoutException(SerialCommunicationException):
    pass


class SerialRPCTimeoutException(SerialTimeoutException):
    pass


class ForbiddenOperationException(SerialExceptionBase):
    def __init__(self, message: str) -> None:
        super().__init__(message)


DEFAULT_BAUD_RATE = 9600
DEFAULT_PARITY = "N"
DEFAULT_DATA_BITS = 8
DEFAULT_STOP_BITS = 2
DEFAULT_RPC_CALL_TIMEOUT_MS = 10000


@dataclass
class SerialConfig:
    path: str
    baud_rate: int = DEFAULT_BAUD_RATE
    parity: str = DEFAULT_PARITY
    data_bits: int = DEFAULT_DATA_BITS
    stop_bits: int = DEFAULT_STOP_BITS

    def __str__(self) -> str:
        return f"{self.path} {self.baud_rate} {self.data_bits}{self.parity}{self.stop_bits}"


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
    read_fn: Optional[ModbusFunctionCode] = ModbusFunctionCode.READ_HOLDING
    data_type: DataType = DataType.UINT


WB_DEVICE_PARAMETERS = {
    "fw_signature": ParameterConfig(
        register_address=290,
        register_count=12,
        read_fn=ModbusFunctionCode.READ_HOLDING,
        data_type=DataType.STR,
    ),
    "bootloader_version_full": ParameterConfig(
        register_address=330,
        register_count=8,
        read_fn=ModbusFunctionCode.READ_HOLDING,
        data_type=DataType.BYTES,
    ),
    "bootloader_version": ParameterConfig(
        register_address=330,
        register_count=7,
        read_fn=ModbusFunctionCode.READ_HOLDING,
        data_type=DataType.STR,
    ),
}


def add_port_config_to_rpc_request(rpc_request: dict, port_config: Union[SerialConfig, TcpConfig]) -> None:
    if isinstance(port_config, SerialConfig):
        rpc_request.update(asdict(port_config))
    else:
        rpc_request["ip"] = port_config.address
        rpc_request["port"] = port_config.port


class SerialRPCWrapper:  # pylint:disable=too-few-public-methods

    def __init__(self, rpc_client: SRPCClient) -> None:
        self.rpc_client = rpc_client

    def _calculate_default_response_timeout(self) -> float:
        """
        response_timeout (on mb_master side): roundtrip + device_processing + uart_processing
        """
        wb_devices_response_time_s = 8e-3  # devices with old fws
        return wb_devices_response_time_s

    async def _communicate(self, service: str, method: str, parms: dict) -> bytes:
        try:
            response = await self.rpc_client.make_rpc_call(
                driver="wb-mqtt-serial",
                service=service,
                method=method,
                params=parms,
                timeout=DEFAULT_RPC_CALL_TIMEOUT_MS / 1000,
            )
        except rpcclient.MQTTRPCError as err:
            if err.code == MQTTRPCErrorCode.REQUEST_TIMEOUT_ERROR.value:
                raise SerialTimeoutException(str(err)) from err
            if err.code == MQTTRPCErrorCode.RPC_CALL_TIMEOUT.value:
                raise SerialRPCTimeoutException(str(err)) from err
            raise SerialCommunicationException(str(err)) from err

        if "exception" in response:
            raise WBModbusException(response["exception"]["msg"], response["exception"]["code"])

        return bytes.fromhex(str(response.get("response", "")))

    async def _send_request_to_port(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        port_config: Union[SerialConfig, TcpConfig],
        slave_id: int,
        function_code: int,
        register_address: int,
        register_count: int,
        protocol: ModbusProtocol,
        response_timeout_s: Optional[float] = None,
    ) -> bytes:
        if response_timeout_s is None:
            response_timeout_s = self._calculate_default_response_timeout()
        response_timeout_ms = round(response_timeout_s * 1000)

        rpc_request = {
            "slave_id": slave_id,
            "function": function_code,
            "address": register_address,
            "count": register_count,
            "response_timeout": response_timeout_ms,
            "total_timeout": DEFAULT_RPC_CALL_TIMEOUT_MS,
            "protocol": "modbus-tcp" if protocol == ModbusProtocol.MODBUS_TCP else "modbus",
            "format": "HEX",
        }
        add_port_config_to_rpc_request(rpc_request, port_config)

        return await self._communicate("port", "Load", rpc_request)

    async def read(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        port_config: Union[SerialConfig, TcpConfig],
        slave_id: int,
        param_config: ParameterConfig,
        protocol: ModbusProtocol,
        response_timeout_s: Optional[float] = None,
    ) -> Union[str, int, bytes]:
        if param_config.read_fn is None:
            raise ForbiddenOperationException(f"Register {param_config.register_address} is not readable")

        response = await self._send_request_to_port(
            port_config,
            slave_id,
            param_config.read_fn.value,
            param_config.register_address,
            param_config.register_count,
            protocol,
            response_timeout_s,
        )
        if param_config.data_type == DataType.STR:
            return "".join(chr(byte) for byte in response[1::2]).strip("\x00\xff")
        if param_config.data_type == DataType.UINT:
            return int.from_bytes(response, byteorder="big")
        return response
