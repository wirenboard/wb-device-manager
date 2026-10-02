#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import signal
import time
from enum import Enum
from functools import partial
from pathlib import PurePosixPath

import paho.mqtt.client as mqtt
from jsonrpc.exceptions import JSONRPCDispatchException, JSONRPCServerError
from mqttrpc import client as rpcclient
from mqttrpc.manager import AMQTTRPCResponseManager
from mqttrpc.protocol import MQTTRPC10Response

from . import TOPIC_HEADER, logger

EXIT_SUCCESS = 0
EXIT_INVALIDARGUMENT = 2
# one deadline for confirming all the retained clears at stop; well below systemd's TimeoutStopSec
CLEAR_RETAINED_TIMEOUT_S = 5.0
# CONNACK codes for a rejected login: bad user name or password, not authorized
MQTT_AUTH_ERRORS = (4, 5)


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


class SRPCClient(rpcclient.TMQTTRPCClient):  # pylint:disable=too-few-public-methods
    """
    Stores internal future-like objs (with rpc-call result), filled from outer on_mqtt_message callback
    """

    def __init__(self, client):
        super().__init__(client)
        self._counter = 0

    async def make_rpc_call(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, driver, service, method, params, timeout
    ):
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
                message=f"rpc call to {driver}/{service}/{method} -> {timeout:.2f}s: no answer",
                data=f"rpc call params: {str(params)}",
            ) from e
        except rpcclient.MQTTRPCError as e:
            logger.debug("RPC Client %d <- error %s", call_id, e)
            if e.code == MQTTRPCErrorCode.REQUEST_TIMEOUT_ERROR.value:
                raise MQTTRPCRequestTimeoutError(e.rpc_message, e.data) from e
            raise e


class MQTTRPCCallTimeoutError(rpcclient.MQTTRPCError):  # pylint:disable=too-few-public-methods
    CODE = MQTTRPCErrorCode.RPC_CALL_TIMEOUT.value

    def __init__(self, message, code=None, data=""):
        super().__init__(message, code or self.CODE, data)


class MQTTRPCRequestTimeoutError(rpcclient.MQTTRPCError):  # pylint:disable=too-few-public-methods
    def __init__(self, message, data=""):
        super().__init__(message, MQTTRPCErrorCode.REQUEST_TIMEOUT_ERROR.value, data)


class MQTTRPCAlreadyProcessingError(JSONRPCServerError):  # pylint:disable=too-few-public-methods
    CODE = -33100
    MESSAGE = "Task is already executing."


class MQTTRPCAlreadyProcessingException(JSONRPCDispatchException):
    """
    Compatible with mqttrpc.TMQTTRPCResponseManager
    """

    CODE = -33100

    def __init__(  # pylint:disable=keyword-arg-before-vararg, unused-argument
        self, code=None, message=None, data=None, *args, **kwargs
    ):
        self.error = MQTTRPCAlreadyProcessingError(code=code, data=data, message=message)
        super().__init__(code=self.error.code, message=self.error.message, data=self.error.data)


class AsyncMQTTServer:  # pylint:disable=too-many-instance-attributes
    _NOW_PROCESSING = []

    def __init__(  # pylint:disable=too-many-arguments,too-many-positional-arguments
        self,
        methods_dispatcher,
        mqtt_connection,
        mqtt_url_str,
        rpc_client,
        bus_scanner,
        fw_updater,
        asyncio_loop,
    ):
        self.methods_dispatcher = methods_dispatcher
        self.mqtt_connection = mqtt_connection
        self.rpc_client = rpc_client
        self.asyncio_loop = asyncio_loop
        self.mqtt_url_str = mqtt_url_str
        self.bus_scanner = bus_scanner
        self.fw_updater = fw_updater
        self._exit_code = EXIT_SUCCESS

    @property
    def now_processing(self):
        return type(self)._NOW_PROCESSING

    def _delete_retained(self):
        infos = []
        for service, method in self.methods_dispatcher.keys():
            topic = get_topic_path(service, method)
            logger.debug("Delete retained from: %s", topic)
            infos.append(self.mqtt_connection.publish(topic, payload=None, retain=True, qos=1))
        return infos

    def _close_mqtt_connection(self):
        if self.mqtt_connection.is_connected():
            infos = [self.bus_scanner.clear_state(), self.fw_updater.clear_state(), *self._delete_retained()]
            self._wait_published(infos)
        else:
            logger.error("MQTT broker is not connected, retained topics cannot be removed")
        self.mqtt_connection.stop()
        logger.info("Mqtt: close %s", self.mqtt_url_str)

    @staticmethod
    def _wait_published(infos):
        """
        Wait for the retained clears within one shared deadline; a failure is logged, never raised.
        """
        deadline = time.monotonic() + CLEAR_RETAINED_TIMEOUT_S
        try:
            for info in infos:
                info.wait_for_publish(max(0.0, deadline - time.monotonic()))
        except (RuntimeError, ValueError) as exc:  # paho: the client is not connected / queue full
            logger.error("Removal of the retained topics is not confirmed: %s", exc)
            return
        if not all(info.is_published() for info in infos):
            logger.error(
                "Removal of the retained topics is not confirmed within %.0f s", CLEAR_RETAINED_TIMEOUT_S
            )

    def _cancel_pending_tasks(self):
        pending = [task for task in asyncio.all_tasks(self.asyncio_loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            logger.debug("Waiting for %d cancelled task(s)", len(pending))
            self.asyncio_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

    def _setup_event_loop(self):
        signals = [signal.SIGINT, signal.SIGTERM]
        for sig in signals:
            self.asyncio_loop.add_signal_handler(sig, self.asyncio_loop.stop)
        logger.debug("Add handler for: %s; event loop: %s", str(signals), str(self.asyncio_loop))

    def _setup_mqtt_connection(self):
        self.mqtt_connection.on_connect = self._on_mqtt_connect
        self.mqtt_connection.on_disconnect = self._on_mqtt_disconnect
        self.mqtt_connection.on_message = self._on_mqtt_message
        # an unavailable broker is retried by paho's network thread; the signal handlers
        # installed before still stop the loop meanwhile
        self.mqtt_connection.start(retry_first_connection=True)

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

    def _on_mqtt_connect(self, client, userdata, flags, rc):  # pylint:disable=unused-argument
        logger.info("Mqtt: reconnect to %s -> %d", self.mqtt_url_str, rc)
        if rc == 0:
            self.bus_scanner.publish_state()
            self.fw_updater.publish_state()
            self._subscribe()
        elif rc in MQTT_AUTH_ERRORS:
            # a rejected login is a configuration problem, paho would retry it forever: exit with 2
            logger.error("Mqtt: login rejected (rc %d); shutting down", rc)
            self._exit_code = EXIT_INVALIDARGUMENT
            self.asyncio_loop.call_soon_threadsafe(self.asyncio_loop.stop)
        else:
            logger.warning("Mqtt: connection refused (rc %d), retrying", rc)

    def _on_mqtt_disconnect(self, client, userdata, rc):  # pylint:disable=unused-argument
        logger.warning("Mqtt: disconnect from %s -> %d", self.mqtt_url_str, rc)
        self.rpc_client.subscribes = set()  # rpc_client re-subscribes if not subscribed
        logger.debug("Clear rpc_client subscribes")

    def _on_mqtt_message(self, _client, _userdata, message):
        if mqtt.topic_matches_sub(f"/rpc/v1/+/+/+/{self.rpc_client.rpc_client_id}/reply", message.topic):
            self.rpc_client.on_mqtt_message(None, None, message)  # reply from mqtt client; filling payload

        else:  # requests to a server
            if self.is_processing(message):
                logger.warning("'%s' is already processing!", message.topic)
                response = MQTTRPC10Response(
                    error=MQTTRPCAlreadyProcessingError()._data  # pylint: disable=protected-access
                )
                self.reply(message, response.json)
            else:
                self.add_to_processing(message)
                asyncio.run_coroutine_threadsafe(self.run_async(message), self.asyncio_loop)

    def reply(self, message, payload):
        topic = message.topic + "/reply"
        self.mqtt_connection.publish(topic, payload, qos=2, retain=False)

    async def run_async(self, message):
        parts = message.topic.split("/")  # re?
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
        """
        Serve until SIGINT/SIGTERM or a rejected MQTT login; returns the exit code.
        """
        self.asyncio_loop.run_forever()
        self._cancel_pending_tasks()
        self._close_mqtt_connection()
        return self._exit_code
