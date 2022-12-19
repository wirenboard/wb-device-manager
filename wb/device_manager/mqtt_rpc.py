#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import atexit
import signal
import asyncio
from pathlib import PurePosixPath
from functools import partial
import paho.mqtt.client as mosquitto
from mqttrpc import client as rpcclient
from mqttrpc.manager import AMQTTRPCResponseManager
from mqttrpc.protocol import MQTTRPC10Response
from jsonrpc.exceptions import JSONRPCServerError, JSONRPCDispatchException
from wb_modbus import minimalmodbus, instruments
from . import logger, TOPIC_HEADER


def get_topic_path(*args):
    ret = PurePosixPath(TOPIC_HEADER, *[str(arg) for arg in args])
    return str(ret)


class RPCResultFuture(asyncio.Future):
    """
    an rpc-call-result obj:
        - is future;
        - supposed to be filled from another thread (on_message callback)
        - compatible with mqttrpc api
    """

    def set_result(self, result):
        if result is not None:
            self._loop.call_soon_threadsafe(partial(super().set_result, result))

    def set_exception(self, exception):
        self._loop.call_soon_threadsafe(partial(super().set_exception, exception))


class SRPCClient(rpcclient.TMQTTRPCClient):
    """
    Stores internal future-like objs (with rpc-call result), filled from outer on_mqtt_message callback
    """
    async def make_rpc_call(self, driver, service, method, params, timeout):
        logger.debug("RPC Client -> %s (rpc timeout: %.2fs)", params, timeout)
        response_f = self.call_async(
            driver,
            service,
            method,
            params,
            result_future=RPCResultFuture
            )
        try:
            response = await asyncio.wait_for(response_f, timeout)
            logger.debug("RPC Client <- %s", response)
            return response
        except asyncio.exceptions.TimeoutError as e:
            raise MQTTRPCInternalServerError(
                message="rpc call to %s/%s/%s -> %.2fs: no answer" % (
                    driver, service, method, timeout
                ),
                data="rpc call params: %s" % str(params)
                )


class AsyncModbusInstrument(instruments.SerialRPCBackendInstrument):
    """
    Generic minimalmodbus instrument's logic with mqtt-rpc to wb-mqtt-serial as transport
    (instead of pyserial)
    """

    def __init__(self, port, slaveaddress, rpc_client, **kwargs):
        super().__init__(port, slaveaddress, **kwargs)
        self.rpc_client = rpc_client
        self.serial.timeout = kwargs.get("response_timeout", self._calculate_default_response_timeout())

    def _calculate_default_response_timeout(self):
        """
        response_timeout (on mb_master side): roundtrip + device_processing + uart_processing
        """
        wb_devices_response_time_s = 8E-3  # devices with old fws
        onebyte_on_1200bd_s = 10E-3
        linux_uart_processing_s = 50E-3  # with huge upper reserve
        return wb_devices_response_time_s + onebyte_on_1200bd_s + linux_uart_processing_s

    async def _communicate(self, request, number_of_bytes_to_read):
        minimalmodbus._check_string(request, minlength=1, description="request")
        minimalmodbus._check_int(number_of_bytes_to_read)

        """
        overall rpc-request action timeout:
            - device is supposed to be alive (small modbus response_timeout inside)
            - depends on wb-mqtt-serial's poll scheduler => overall val is relatively huge
        """
        rpc_call_timeout = 10

        rpc_request = {
            "response_size": number_of_bytes_to_read,
            "format": "HEX",
            "msg": minimalmodbus._hexencode(request),
            "response_timeout": round(self.serial.timeout * 1000),
            "path": self.serial.port,  # TODO: support modbus tcp in minimalmodbus
            "baud_rate" : self.serial.SERIAL_SETTINGS["baudrate"],
            "parity" : self.serial.SERIAL_SETTINGS["parity"],
            "stop_bits" : self.serial.SERIAL_SETTINGS["stopbits"],
            "data_bits" : 8,
            "total_timeout": round(rpc_call_timeout * 1000),
        }

        try:
            response = await self.rpc_client.make_rpc_call(
                driver="wb-mqtt-serial",
                service="port",
                method="Load",
                params=rpc_request,
                timeout=rpc_call_timeout
                )

        except rpcclient.MQTTRPCError as e:
            reraise_err = minimalmodbus.NoResponseError(
                "RPC: no response with %.2fs timeout: server returned code %d; rpc call: %s" % (
                    rpc_call_timeout, e.code, str(rpc_request)
                    )
                ) if "request timed out" in e.data else e  # TODO: fix rpc errcodes in wb-mqtt-serial
            raise reraise_err from e

        else:
            return minimalmodbus._hexdecode(str(response.get("response", "")))


class MQTTRPCInternalServerError(rpcclient.MQTTRPCError):
    CODE = -33000

    def __init__(self, message, code=None, data=""):
        super().__init__(message, code or self.CODE, data)


class MQTTRPCAlreadyProcessingError(JSONRPCServerError):
    CODE = -33100
    MESSAGE = "Task is already executing."


class MQTTRPCAlreadyProcessingException(JSONRPCDispatchException):
    """
    Compatible with mqttrpc.TMQTTRPCResponseManager
    """
    CODE = -33100

    def __init__(self, code=None, message=None, data=None, *args, **kwargs):
        self.error = MQTTRPCAlreadyProcessingError(code=code, data=data, message=message)
        super().__init__(code=self.error.code, message=self.error.message, data=self.error.data)


class AsyncMQTTServer:
    _NOW_PROCESSING = []
    _EXITCODE = 0

    def __init__(self, methods_dispatcher, mqtt_connection, rpc_client, additional_topics_to_clear=[],
            asyncio_loop=asyncio.get_event_loop(), mqtt_hostport_str="127.0.0.1:1883"):
        self.methods_dispatcher = methods_dispatcher
        self.mqtt_connection = mqtt_connection
        self.rpc_client = rpc_client
        self.asyncio_loop = asyncio_loop
        self.mqtt_hostport_str = mqtt_hostport_str
        self.additional_topics_to_clear = additional_topics_to_clear

    @property
    def now_processing(self):
        return type(self)._NOW_PROCESSING

    def _parse_mqtt_addr(self):
        default_host, default_port = "127.0.0.1", "1883"
        host, port = self.mqtt_hostport_str.split(":", 1)
        return host or default_host, int(port or default_port, 0)

    def _delete_retained(self):
        to_clear = [get_topic_path(service, method) for service, method in self.methods_dispatcher.keys()]
        to_clear.extend(self.additional_topics_to_clear)
        for topic in to_clear:
            logger.debug("Delete retained from: %s", topic)
            m_info = self.mqtt_connection.publish(topic, payload=None, retain=True)
            m_info.wait_for_publish()

    def _close_mqtt_connection(self):
        self._delete_retained()
        self.mqtt_connection.disconnect()
        self.mqtt_connection.loop_stop()
        logger.info("Mqtt: close %s", self.mqtt_hostport_str)

    def _setup_event_loop(self):
        signals = [signal.SIGINT, signal.SIGTERM]
        for sig in signals:
            self.asyncio_loop.add_signal_handler(sig, lambda: self.asyncio_loop.stop())
        logger.debug("Add handler for: %s; event loop: %s", str(signals), str(self.asyncio_loop))
        self.asyncio_loop.set_debug(True)

    def _setup_mqtt_connection(self):
        host, port = self._parse_mqtt_addr()
        self.mqtt_connection.on_connect = self._on_mqtt_connect
        self.mqtt_connection.on_disconnect = self._on_mqtt_disconnect
        self.mqtt_connection.on_message = self._on_mqtt_message

        try:
            self.mqtt_connection.connect(host, port)
            self.mqtt_connection.loop_start()
        finally:
            logger.info("Registered to atexit hook: close %s", self.mqtt_hostport_str)
            atexit.register(lambda: self._close_mqtt_connection())

    def add_to_processing(self, mqtt_message):
        self.now_processing.append((mqtt_message.topic, mqtt_message.payload))

    def remove_from_processing(self, mqtt_message):
        self.now_processing.remove((mqtt_message.topic, mqtt_message.payload))

    def is_processing(self, mqtt_message):
        return (mqtt_message.topic, mqtt_message.payload) in self.now_processing

    def _subscribe(self):
        logger.debug("Subscribing to: %s", str(self.methods_dispatcher.keys()))
        for service, method in self.methods_dispatcher.keys():
            topic_str = get_topic_path(service, method)
            self.mqtt_connection.publish(topic_str, "1", retain=True)
            topic_str += "/+"
            self.mqtt_connection.subscribe(topic_str)
            logger.debug("Subscribed: %s", topic_str)

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        logger.info("Mqtt: reconnect to %s -> %d", self.mqtt_hostport_str, rc)
        if rc == 0:
            self._subscribe()
        else:
            logger.warning("Got rc %d; shutting down...", rc)
            self._EXITCODE = rc
            self.asyncio_loop.stop()

    def _on_mqtt_disconnect(self, client, userdata, rc):
        logger.warning("Mqtt: disconnect from %s -> %d", self.mqtt_hostport_str, rc)
        self.rpc_client.subscribes = set()  # rpc_client re-subscribes if not subscribed
        logger.debug("Clear rpc_client subscribes")

    def _on_mqtt_message(self, _client, _userdata, message):
        if mosquitto.topic_matches_sub('/rpc/v1/+/+/+/%s/reply' % self.rpc_client.rpc_client_id, message.topic):
            self.rpc_client.on_mqtt_message(None, None, message)  # reply from mqtt client; filling payload

        else:  # requests to a server
            if self.is_processing(message):
                logger.warning("'%s' is already processing!", message.topic)
                response = MQTTRPC10Response(error=MQTTRPCAlreadyProcessingError()._data)
                self.reply(message, response.json)
            else:
                self.add_to_processing(message)
                asyncio.run_coroutine_threadsafe(self.run_async(message), self.asyncio_loop)

    def reply(self, message, payload):
        topic = message.topic + "/reply"
        self.mqtt_connection.publish(topic, payload, False)

    async def run_async(self, message):
        parts = message.topic.split("/")  # TODO: re?
        service_id, method_id = parts[4], parts[5]

        try:
            ret = await AMQTTRPCResponseManager.handle(  # wraps any exception into json-rpc
                message.payload,
                service_id,
                method_id,
                self.methods_dispatcher
                )

            self.reply(message, ret.json)
        finally:
            self.remove_from_processing(message)

    def setup(self):
        self._setup_event_loop()
        self._setup_mqtt_connection()

    def run(self):
        self.asyncio_loop.run_forever()
        return self._EXITCODE
