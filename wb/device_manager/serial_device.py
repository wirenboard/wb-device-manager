from abc import ABC, abstractmethod
from typing import Union

from .serial_rpc import (
    ModbusProtocol,
    ParameterConfig,
    SerialConfig,
    SerialRPCWrapper,
    TcpConfig,
)


class Device(ABC):
    protocol: ModbusProtocol
    slave_id: int

    _serial_rpc: SerialRPCWrapper

    def __init__(
        self,
        protocol: ModbusProtocol,
        slave_id: int,
        serial_rpc: SerialRPCWrapper,
    ) -> None:
        self._serial_rpc = serial_rpc
        self.protocol = protocol
        self.slave_id = slave_id
        self._description = f"slave id: {self.slave_id}, {self.get_port_config()}"

    async def read(self, param_config: ParameterConfig) -> Union[str, int, bytes]:
        return await self._serial_rpc.read(self.get_port_config(), self.slave_id, param_config, self.protocol)

    @property
    def description(self) -> str:
        return self._description

    @abstractmethod
    def get_port_config(self) -> Union[SerialConfig, TcpConfig]:
        pass


class TcpDevice(Device):
    _port_config: TcpConfig

    def __init__(
        self,
        port_config: TcpConfig,
        protocol: ModbusProtocol,
        slave_id: int,
        serial_rpc: SerialRPCWrapper,
    ) -> None:
        self._port_config = port_config
        super().__init__(protocol, slave_id, serial_rpc)

    def get_port_config(self) -> TcpConfig:
        return self._port_config


class SerialDevice(Device):
    _port_config: SerialConfig

    def __init__(
        self,
        port_config: SerialConfig,
        protocol: ModbusProtocol,
        slave_id: int,
        serial_rpc: SerialRPCWrapper,
    ) -> None:
        self._port_config = port_config
        super().__init__(protocol, slave_id, serial_rpc)

    def get_port_config(self) -> SerialConfig:
        return self._port_config


def create_device(
    port_config: Union[SerialConfig, TcpConfig],
    protocol: ModbusProtocol,
    slave_id: int,
    serial_rpc: SerialRPCWrapper,
) -> Device:
    if isinstance(port_config, SerialConfig):
        return SerialDevice(port_config, protocol, slave_id, serial_rpc)
    return TcpDevice(port_config, protocol, slave_id, serial_rpc)
