from abc import ABC, abstractmethod
from typing import Optional, Union, cast

from .serial_rpc import (
    DEFAULT_BAUD_RATE,
    DEFAULT_PARITY,
    WB_DEVICE_PARAMETERS,
    ModbusProtocol,
    ParameterConfig,
    SerialConfig,
    SerialRPCWrapper,
    TcpConfig,
    get_baud_rate_from_register_value,
    get_parity_from_register_value,
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

    async def set_poll(self, enabled) -> None:
        await self._serial_rpc.set_poll(self.get_port_config(), self.slave_id, enabled)

    async def write(
        self,
        param_config: ParameterConfig,
        value: Union[int, bytes],
        response_timeout_s: Optional[float] = None,
    ) -> None:
        await self._serial_rpc.write(
            self.get_port_config(), self.slave_id, param_config, value, self.protocol, response_timeout_s
        )

    async def read(self, param_config: ParameterConfig) -> Union[str, int, bytes]:
        return await self._serial_rpc.read(self.get_port_config(), self.slave_id, param_config, self.protocol)

    @property
    def description(self) -> str:
        return self._description

    @abstractmethod
    def get_port_config(self) -> Union[SerialConfig, TcpConfig]:
        pass

    @abstractmethod
    def set_default_port_settings(self) -> None:
        pass

    @abstractmethod
    async def check_updatable(self, bootloader_can_preserve_port_settings: bool) -> bool:
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

    def set_default_port_settings(self) -> None:
        pass

    async def check_updatable(self, bootloader_can_preserve_port_settings: bool) -> bool:
        if bootloader_can_preserve_port_settings or self.protocol == ModbusProtocol.MODBUS_TCP:
            return True

        # For Modbus RTU over TCP with old bootloader
        # we need to check baud rate and parity
        # as these bootloaders support only 9600 and None respectively
        baud_rate = get_baud_rate_from_register_value(
            cast(int, await self.read(WB_DEVICE_PARAMETERS["baud_rate"]))
        )
        parity = get_parity_from_register_value(cast(int, await self.read(WB_DEVICE_PARAMETERS["parity"])))
        return baud_rate == DEFAULT_BAUD_RATE and parity == DEFAULT_PARITY


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

    def set_default_port_settings(self) -> None:
        self._port_config.set_default_settings()

    async def check_updatable(self, bootloader_can_preserve_port_settings: bool) -> bool:
        return True


def create_device(
    port_config: Union[SerialConfig, TcpConfig],
    protocol: ModbusProtocol,
    slave_id: int,
    serial_rpc: SerialRPCWrapper,
) -> Device:
    if isinstance(port_config, SerialConfig):
        return SerialDevice(port_config, protocol, slave_id, serial_rpc)
    return TcpDevice(port_config, protocol, slave_id, serial_rpc)


def create_device_from_json(data: dict, serial_rpc: SerialRPCWrapper) -> Device:
    """
    Creates a Device instance from a JSON-like dictionary.

    Depending on the presence of the "address" key in the "port" dictionary, this function
    returns either a TcpDevice or a SerialDevice. The device is configured using the provided
    data and serial_rpc wrapper.

    Args:
        data (dict): Dictionary containing device configuration. Expected keys include
            slave_id (int): Modbus slave ID.
            port (dict): The port configuration.
            protocol (str): The Modbus protocol to use.
        serial_rpc (SerialRPCWrapper): Wrapper for serial RPC communication.

    Returns:
        Device: An instance of TcpDevice or SerialDevice configured according to the input data.
    """
    slave_id = data.get("slave_id", 0)
    protocol = ModbusProtocol(data.get("protocol", ModbusProtocol.MODBUS_RTU.value))
    port = data.get("port", {})
    if "address" in port:
        return TcpDevice(TcpConfig(**port), protocol, slave_id, serial_rpc)
    return SerialDevice(SerialConfig(**port), protocol, slave_id, serial_rpc)
