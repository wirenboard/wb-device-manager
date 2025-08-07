from dataclasses import dataclass
from typing import Optional, Union

from .serial_rpc import (
    ModbusProtocol,
    ParameterConfig,
    SerialConfig,
    SerialRPCWrapper,
    TcpConfig,
)


@dataclass
class SerialDevice:
    port_config: Union[SerialConfig, TcpConfig]
    protocol: ModbusProtocol
    slave_id: int

    _serial_rpc: SerialRPCWrapper

    def __post_init__(self) -> None:
        self._description = f"slave id: {self.slave_id}, {self.port_config}"

    async def set_poll(self, enabled) -> None:
        await self._serial_rpc.set_poll(self._port_config, self._slave_id, enabled)

    async def write(
        self,
        param_config: ParameterConfig,
        value: Union[int, bytes],
        response_timeout_s: Optional[float] = None,
    ) -> None:
        await self._serial_rpc.write(
            self.port_config, self.slave_id, param_config, value, self.protocol, response_timeout_s
        )

    async def read(self, param_config: ParameterConfig) -> Union[str, int, bytes]:
        return await self._serial_rpc.read(self.port_config, self.slave_id, param_config, self.protocol)

    def set_default_port_settings(self) -> None:
        if isinstance(self.port_config, SerialConfig):
            self.port_config.set_default_settings()

    @property
    def description(self) -> str:
        return self._description
