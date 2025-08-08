from typing import cast
from unittest.mock import AsyncMock

import mqttrpc
import pytest

from wb.device_manager import mqtt_rpc
from wb.device_manager.serial_rpc import (
    DataType,
    ForbiddenOperationException,
    ModbusFunctionCode,
    ModbusProtocol,
    ParameterConfig,
    SerialConfig,
    SerialRPCTimeoutException,
    SerialRPCWrapper,
    SerialTimeoutException,
    TcpConfig,
    WBModbusException,
    add_port_config_to_rpc_request,
    get_baud_rate_from_register_value,
    get_parity_from_register_value,
    value_to_bytes,
)


class DummySRPCClient:
    def __init__(self):
        self.make_rpc_call = AsyncMock()


@pytest.mark.asyncio
async def test_read_str_parameter():
    client = DummySRPCClient()
    # Simulate response: 0x00 0x41 0x00 0x42 -> "AB"
    client.make_rpc_call.return_value = {"response": "00410042"}
    wrapper = SerialRPCWrapper(cast(mqtt_rpc.SRPCClient, client))
    param = ParameterConfig(
        register_address=100,
        register_count=2,
        read_fn=ModbusFunctionCode.READ_HOLDING,
        data_type=DataType.STR,
    )
    result = await wrapper.read(
        SerialConfig("/dev/ttyUSB0"),
        1,
        param,
        ModbusProtocol.MODBUS_RTU,
    )
    assert result == "AB"
    client.make_rpc_call.assert_called_once_with(
        driver="wb-mqtt-serial",
        service="port",
        method="Load",
        params={
            "path": "/dev/ttyUSB0",
            "baud_rate": 9600,
            "parity": "N",
            "data_bits": 8,
            "stop_bits": 2,
            "slave_id": 1,
            "function": 3,
            "address": 100,
            "count": 2,
            "response_timeout": 8,
            "total_timeout": 10000,
            "format": "HEX",
            "protocol": "modbus",
        },
        timeout=10,
    )


@pytest.mark.asyncio
async def test_read_uint_parameter():
    client = DummySRPCClient()
    # Simulate response: 0x01 0x02 -> 258
    client.make_rpc_call.return_value = {"response": "0102"}
    wrapper = SerialRPCWrapper(cast(mqtt_rpc.SRPCClient, client))
    param = ParameterConfig(
        register_address=101,
        register_count=1,
        read_fn=ModbusFunctionCode.READ_HOLDING,
        data_type=DataType.UINT,
    )
    result = await wrapper.read(
        SerialConfig("/dev/ttyUSB0"),
        2,
        param,
        ModbusProtocol.MODBUS_RTU,
    )
    assert result == 258
    client.make_rpc_call.assert_called_once_with(
        driver="wb-mqtt-serial",
        service="port",
        method="Load",
        params={
            "path": "/dev/ttyUSB0",
            "baud_rate": 9600,
            "parity": "N",
            "data_bits": 8,
            "stop_bits": 2,
            "slave_id": 2,
            "function": 3,
            "address": 101,
            "count": 1,
            "response_timeout": 8,
            "total_timeout": 10000,
            "format": "HEX",
            "protocol": "modbus",
        },
        timeout=10,
    )


@pytest.mark.asyncio
async def test_read_uint_parameter_modbus_tcp():
    client = DummySRPCClient()
    # Simulate response: 0x01 0x02 -> 258
    client.make_rpc_call.return_value = {"response": "0102"}
    wrapper = SerialRPCWrapper(cast(mqtt_rpc.SRPCClient, client))
    param = ParameterConfig(
        register_address=101,
        register_count=1,
        read_fn=ModbusFunctionCode.READ_HOLDING,
        data_type=DataType.UINT,
    )
    result = await wrapper.read(
        SerialConfig("/dev/ttyUSB0"),
        2,
        param,
        ModbusProtocol.MODBUS_TCP,
    )
    assert result == 258
    client.make_rpc_call.assert_called_once_with(
        driver="wb-mqtt-serial",
        service="port",
        method="Load",
        params={
            "path": "/dev/ttyUSB0",
            "baud_rate": 9600,
            "parity": "N",
            "data_bits": 8,
            "stop_bits": 2,
            "slave_id": 2,
            "function": 3,
            "address": 101,
            "count": 1,
            "response_timeout": 8,
            "total_timeout": 10000,
            "format": "HEX",
            "protocol": "modbus-tcp",
        },
        timeout=10,
    )


@pytest.mark.asyncio
async def test_read_bytes_parameter():
    client = DummySRPCClient()
    client.make_rpc_call.return_value = {"response": "abcd"}
    wrapper = SerialRPCWrapper(cast(mqtt_rpc.SRPCClient, client))
    param = ParameterConfig(
        register_address=102,
        register_count=2,
        read_fn=ModbusFunctionCode.READ_HOLDING,
        data_type=DataType.BYTES,
    )
    result = await wrapper.read(
        SerialConfig("/dev/ttyUSB0"),
        3,
        param,
        ModbusProtocol.MODBUS_RTU,
    )
    assert result == b"\xab\xcd"
    client.make_rpc_call.assert_called_once_with(
        driver="wb-mqtt-serial",
        service="port",
        method="Load",
        params={
            "path": "/dev/ttyUSB0",
            "baud_rate": 9600,
            "parity": "N",
            "data_bits": 8,
            "stop_bits": 2,
            "slave_id": 3,
            "function": 3,
            "address": 102,
            "count": 2,
            "response_timeout": 8,
            "total_timeout": 10000,
            "format": "HEX",
            "protocol": "modbus",
        },
        timeout=10,
    )


@pytest.mark.asyncio
async def test_read_forbidden():
    client = DummySRPCClient()
    wrapper = SerialRPCWrapper(cast(mqtt_rpc.SRPCClient, client))
    param = ParameterConfig(
        register_address=1,
        register_count=1,
        read_fn=None,
        data_type=DataType.UINT,
    )
    with pytest.raises(ForbiddenOperationException):
        await wrapper.read(
            SerialConfig("/dev/ttyUSB0"),
            1,
            param,
            ModbusProtocol.MODBUS_RTU,
        )


@pytest.mark.asyncio
async def test_write_forbidden():
    client = DummySRPCClient()
    wrapper = SerialRPCWrapper(cast(mqtt_rpc.SRPCClient, client))
    param = ParameterConfig(
        register_address=1,
        register_count=1,
        write_fn=None,
        data_type=DataType.UINT,
    )
    with pytest.raises(ForbiddenOperationException):
        await wrapper.write(
            SerialConfig("/dev/ttyUSB0"),
            1,
            param,
            123,
            ModbusProtocol.MODBUS_RTU,
        )


@pytest.mark.asyncio
async def test_write_multiple_registers_short_data():
    client = DummySRPCClient()
    wrapper = SerialRPCWrapper(cast(mqtt_rpc.SRPCClient, client))
    param = ParameterConfig(
        register_address=1,
        register_count=4,
        write_fn=ModbusFunctionCode.WRITE_MULTIPLE_REGISTERS,
        data_type=DataType.BYTES,
    )
    # Data is 2 bytes, so register_count should be 1
    client.make_rpc_call.return_value = {"response": ""}
    await wrapper.write(
        SerialConfig("/dev/ttyUSB0"),
        1,
        param,
        b"\x01\x02",
        ModbusProtocol.MODBUS_RTU,
    )
    args, kwargs = client.make_rpc_call.call_args
    assert kwargs["params"]["count"] == 1


@pytest.mark.asyncio
async def test_communicate_timeout():
    client = DummySRPCClient()
    err = mqttrpc.client.MQTTRPCError("timeout", mqtt_rpc.MQTTRPCErrorCode.REQUEST_TIMEOUT_ERROR.value, "")
    client.make_rpc_call.side_effect = err
    wrapper = SerialRPCWrapper(cast(mqtt_rpc.SRPCClient, client))
    param = ParameterConfig(
        register_address=1,
        register_count=1,
        read_fn=ModbusFunctionCode.READ_HOLDING,
        data_type=DataType.UINT,
    )
    with pytest.raises(SerialTimeoutException):
        await wrapper.read(
            SerialConfig("/dev/ttyUSB0"),
            1,
            param,
            ModbusProtocol.MODBUS_RTU,
        )


@pytest.mark.asyncio
async def test_communicate_rpc_timeout():
    client = DummySRPCClient()
    err = mqttrpc.client.MQTTRPCError("timeout", mqtt_rpc.MQTTRPCErrorCode.RPC_CALL_TIMEOUT.value, "")
    client.make_rpc_call.side_effect = err
    wrapper = SerialRPCWrapper(cast(mqtt_rpc.SRPCClient, client))
    param = ParameterConfig(
        register_address=1,
        register_count=1,
        read_fn=ModbusFunctionCode.READ_HOLDING,
        data_type=DataType.UINT,
    )
    with pytest.raises(SerialRPCTimeoutException):
        await wrapper.read(
            SerialConfig("/dev/ttyUSB0"),
            1,
            param,
            ModbusProtocol.MODBUS_RTU,
        )


@pytest.mark.asyncio
async def test_communicate_modbus_exception():
    client = DummySRPCClient()
    client.make_rpc_call.return_value = {"exception": {"msg": "Modbus error", "code": 2}}
    wrapper = SerialRPCWrapper(cast(mqtt_rpc.SRPCClient, client))
    param = ParameterConfig(
        register_address=1,
        register_count=1,
        read_fn=ModbusFunctionCode.READ_HOLDING,
        data_type=DataType.UINT,
    )
    with pytest.raises(WBModbusException) as exc:
        await wrapper.read(
            SerialConfig("/dev/ttyUSB0"),
            1,
            param,
            ModbusProtocol.MODBUS_RTU,
        )
        assert exc.value.code == 2
        assert str(exc.value) == "Modbus error"


def test_value_to_bytes_uint():
    assert value_to_bytes(DataType.UINT, 258) == b"\x01\x02"


def test_value_to_bytes_bytes():
    assert value_to_bytes(DataType.BYTES, b"abc") == b"abc"


def test_value_to_bytes_forbidden():
    with pytest.raises(ForbiddenOperationException):
        value_to_bytes(DataType.STR, 123)


def test_add_port_config_to_rpc_request_serial():
    req = {}
    cfg = SerialConfig("/dev/ttyUSB0", 19200, "E", 7, 1)
    add_port_config_to_rpc_request(req, cfg)
    assert req["path"] == "/dev/ttyUSB0"
    assert req["baud_rate"] == 19200
    assert req["parity"] == "E"
    assert req["data_bits"] == 7
    assert req["stop_bits"] == 1


def test_add_port_config_to_rpc_request_tcp():
    req = {}
    cfg = TcpConfig("192.168.1.1", 502)
    add_port_config_to_rpc_request(req, cfg)
    assert req["ip"] == "192.168.1.1"
    assert req["port"] == 502


def test_get_parity_from_register_value():
    assert get_parity_from_register_value(0) == "N"
    assert get_parity_from_register_value(1) == "O"
    assert get_parity_from_register_value(2) == "E"
    assert get_parity_from_register_value(3) == "-"


def test_get_baud_rate_from_register_value():
    assert get_baud_rate_from_register_value(96) == 9600
