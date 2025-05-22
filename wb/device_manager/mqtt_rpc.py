#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import atexit
import signal
from enum import Enum
from functools import partial
from pathlib import PurePosixPath

import paho.mqtt.client as mqtt
from jsonrpc.exceptions import JSONRPCDispatchException, JSONRPCServerError
from mqttrpc import client as rpcclient
from mqttrpc.manager import AMQTTRPCResponseManager
from mqttrpc.protocol import MQTTRPC10Response

from . import TOPIC_HEADER, logger


def get_topic_path(*args):
    ret = PurePosixPath(TOPIC_HEADER, *[str(arg) for arg in args])
    return str(ret)


class MQTTRPCErrorCode(Enum):
    JSON_PARSE_ERROR = -32700
    REQUEST_HANDLING_ERROR = -32000
    REQUEST_TIMEOUT_ERROR = -32600
    RPC_CALL_TIMEOUT = -33000


class RPCResultFuture(asyncio.Future):
    """
    an rpc-call-result obj:
        - is future;
        - supposed to be filled from another thread (on_message callback)
        - compatible with mqttrpc api
    """

    def _set_result(self, result):
        if not self.done():
            super().set_result(result)

    def _set_exception(self, exception):
        if not self.done():
            super().set_exception(exception)

    def set_result(self, result):
        if result is not None:
            self._loop.call_soon_threadsafe(partial(self._set_result, result))

    def set_exception(self, exception):
        self._loop.call_soon_threadsafe(partial(self._set_exception, exception))


class SRPCClient(rpcclient.TMQTTRPCClient):
    """
    Stores internal future-like objs (with rpc-call result), filled from outer on_mqtt_message callback
    """

    def __init__(self, client):
        super().__init__(client)
        self._counter = 0

    async def make_rpc_call(self, driver, service, method, params, timeout):
        self._counter += 1
        call_id = self._counter
        logger.debug("RPC Client %d -> %s (rpc timeout: %.2fs)", call_id, params, timeout)
        response_f = self.call_async(driver, service, method, params, result_future=RPCResultFuture)
        try:
            response = await asyncio.wait_for(response_f, timeout)
            logger.debug("RPC Client %d <- %s", call_id, response)
            return response
        except asyncio.exceptions.TimeoutError as e:
            logger.debug("RPC Client %d <- no answer", call_id)
            raise MQTTRPCCallTimeoutError(
                message="rpc call to %s/%s/%s -> %.2fs: no answer" % (driver, service, method, timeout),
                data="rpc call params: %s" % str(params),
            ) from e
        except rpcclient.MQTTRPCError as e:
            logger.debug("RPC Client %d <- error %s", call_id, e)
            if e.code == MQTTRPCErrorCode.REQUEST_TIMEOUT_ERROR.value:
                raise MQTTRPCRequestTimeoutError(e.rpc_message, e.data) from e
            raise e


class MQTTRPCCallTimeoutError(rpcclient.MQTTRPCError):
    CODE = MQTTRPCErrorCode.RPC_CALL_TIMEOUT.value

    def __init__(self, message, code=None, data=""):
        super().__init__(message, code or self.CODE, data)


class MQTTRPCRequestTimeoutError(rpcclient.MQTTRPCError):
    def __init__(self, message, data=""):
        super().__init__(message, MQTTRPCErrorCode.REQUEST_TIMEOUT_ERROR.value, data)


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

    def __init__(
        self,
        methods_dispatcher,
        mqtt_connection,
        mqtt_url_str,
        rpc_client,
        bus_scanner,
        fw_updater,
        asyncio_loop=asyncio.get_event_loop(),
    ):
        self.methods_dispatcher = methods_dispatcher
        self.mqtt_connection = mqtt_connection
        self.rpc_client = rpc_client
        self.asyncio_loop = asyncio_loop
        self.mqtt_url_str = mqtt_url_str
        self.bus_scanner = bus_scanner
        self.fw_updater = fw_updater

    @property
    def now_processing(self):
        return type(self)._NOW_PROCESSING

    def _delete_retained(self):
        to_clear = [get_topic_path(service, method) for service, method in self.methods_dispatcher.keys()]
        for topic in to_clear:
            logger.debug("Delete retained from: %s", topic)
            m_info = self.mqtt_connection.publish(topic, payload=None, retain=True, qos=1)
            m_info.wait_for_publish()

    def _close_mqtt_connection(self):
        self.bus_scanner.clear_state()
        self.fw_updater.clear_state()
        self._delete_retained()
        self.mqtt_connection.stop()
        logger.info("Mqtt: close %s", self.mqtt_url_str)

    def _setup_event_loop(self):
        signals = [signal.SIGINT, signal.SIGTERM]
        for sig in signals:
            self.asyncio_loop.add_signal_handler(sig, lambda: self.asyncio_loop.stop())
        logger.debug("Add handler for: %s; event loop: %s", str(signals), str(self.asyncio_loop))

    def _setup_mqtt_connection(self):
        self.mqtt_connection.on_connect = self._on_mqtt_connect
        self.mqtt_connection.on_disconnect = self._on_mqtt_disconnect
        self.mqtt_connection.on_message = self._on_mqtt_message

        try:
            self.mqtt_connection.start()
        finally:
            logger.info("Registered to atexit hook: close %s", self.mqtt_url_str)
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
            self.mqtt_connection.publish(topic_str, "1", retain=True, qos=1)
            topic_str += "/+"
            self.mqtt_connection.subscribe(topic_str)
            logger.debug("Subscribed: %s", topic_str)

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        logger.info("Mqtt: reconnect to %s -> %d", self.mqtt_url_str, rc)
        if rc == 0:
            self.bus_scanner.publish_state()
            self.fw_updater.publish_state()
            self._subscribe()
        else:
            logger.warning("Got rc %d; shutting down...", rc)
            self._EXITCODE = rc
            self.asyncio_loop.stop()

    def _on_mqtt_disconnect(self, client, userdata, rc):
        logger.warning("Mqtt: disconnect from %s -> %d", self.mqtt_url_str, rc)
        self.rpc_client.subscribes = set()  # rpc_client re-subscribes if not subscribed
        logger.debug("Clear rpc_client subscribes")

    def _on_mqtt_message(self, _client, _userdata, message):
        if mqtt.topic_matches_sub("/rpc/v1/+/+/+/%s/reply" % self.rpc_client.rpc_client_id, message.topic):
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
        self.mqtt_connection.publish(topic, payload, qos=2, retain=False)

    async def run_async(self, message):
        parts = message.topic.split("/")  # TODO: re?
        service_id, method_id = parts[4], parts[5]

        try:
            ret = await AMQTTRPCResponseManager.handle(  # wraps any exception into json-rpc
                message.payload, service_id, method_id, self.methods_dispatcher
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
