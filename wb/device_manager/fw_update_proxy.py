#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Proxy for fw-update RPCs.

Forwards all firmware update RPC calls to wb-mqtt-serial and logs a deprecation warning.
Clients should call wb-mqtt-serial/fw-update directly.
"""

from jsonrpc.exceptions import JSONRPCDispatchException
from mqttrpc import client as rpcclient

from . import logger
from .mqtt_rpc import MQTTRPCErrorCode, SRPCClient, wait_for_publish

DEPRECATION_WARNING = "wb-device-manager/fw-update is deprecated, use wb-mqtt-serial/fw-update directly"

# Generous timeout for RPC proxy calls (seconds).
# GetFirmwareInfo may take several seconds for serial reads;
# Update/Restore reply immediately with "Ok" before the actual flash.
RPC_PROXY_TIMEOUT_S = 30


class FirmwareUpdateProxy:
    """Proxies fw-update RPCs to wb-mqtt-serial with a deprecation warning."""

    OLD_STATE_TOPIC = "/wb-device-manager/firmware_update/state"

    def __init__(self, rpc_client: SRPCClient, mqtt_connection) -> None:
        self._rpc_client = rpc_client
        self._mqtt_connection = mqtt_connection

    async def _proxy_call(self, method: str, kwargs: dict):
        logger.warning("%s (method: %s)", DEPRECATION_WARNING, method)
        try:
            return await self._rpc_client.make_rpc_call(
                driver="wb-mqtt-serial",
                service="fw-update",
                method=method,
                params=kwargs,
                timeout=RPC_PROXY_TIMEOUT_S,
            )
        except rpcclient.MQTTRPCError as e:
            # JSONRPCError requires an int code and a str message: a reply without
            # them would make the setters raise ValueError right here, so the client
            # would get an opaque internal error instead of the proxied one
            code = e.code if isinstance(e.code, int) else MQTTRPCErrorCode.REQUEST_HANDLING_ERROR.value
            message = e.rpc_message
            if not isinstance(message, str):
                message = f"invalid error reply from wb-mqtt-serial: {e}"
            raise JSONRPCDispatchException(code=code, message=message, data=e.data) from e

    async def get_firmware_info(self, **kwargs) -> dict:
        return await self._proxy_call("GetFirmwareInfo", kwargs)

    async def update_software(self, **kwargs):
        return await self._proxy_call("Update", kwargs)

    async def clear_error(self, **kwargs):
        return await self._proxy_call("ClearError", kwargs)

    async def restore_firmware(self, **kwargs):
        return await self._proxy_call("Restore", kwargs)

    def start(self) -> None:
        """No-op. State is managed by wb-mqtt-serial now."""

    def publish_state(self) -> None:
        """No-op. State is managed by wb-mqtt-serial now."""

    def clear_state(self) -> None:
        """Clear the old retained state topic on shutdown."""
        m_info = self._mqtt_connection.publish(self.OLD_STATE_TOPIC, payload=None, retain=True, qos=1)
        wait_for_publish(m_info, self.OLD_STATE_TOPIC)
