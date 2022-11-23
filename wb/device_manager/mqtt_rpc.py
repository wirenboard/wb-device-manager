#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import atexit
import time
import signal
import asyncio
from contextlib import contextmanager
from pathlib import PurePosixPath
from concurrent import futures
from threading import current_thread, Lock
import paho.mqtt.client as mosquitto
from mqttrpc import MQTTRPCResponseManager, client as rpcclient
from mqttrpc.protocol import MQTTRPC10Response
from wb_modbus import minimalmodbus, instruments
from . import logger, TOPIC_HEADER, make_async


"""
Copy-paste of mqttrpc.manager.MQTTRPCResponseManager with slight changes to make it async
"""
import json
from jsonrpc.utils import is_invalid_params
from jsonrpc.exceptions import (
    JSONRPCInvalidParams,
    JSONRPCInvalidRequest,
    JSONRPCInvalidRequestException,
    JSONRPCMethodNotFound,
    JSONRPCParseError,
    JSONRPCServerError,
    JSONRPCDispatchException,
)
from mqttrpc.protocol import MQTTRPC10Request, MQTTRPC10Response


class AsyncMQTTRPCResponseManager:
    """ MQTT-RPC response manager.

    Method brings syntactic sugar into library. Given dispatcher it handles
    request (both single and batch) and handles errors.
    Request could be handled in parallel, it is server responsibility.

    :param str request_str: json string. Will be converted into
        MQTTRPC10Request

    :param dict dispather: dict<function_name:function>.

    """

    @classmethod
    async def handle(cls, request_str, service_id, method_id, dispatcher):
        if isinstance(request_str, bytes):
            request_str = request_str.decode("utf-8")

        try:
            json.loads(request_str)
        except (TypeError, ValueError):
            return MQTTRPC10Response(error=JSONRPCParseError()._data)

        try:
            request = MQTTRPC10Request.from_json(request_str)
        except JSONRPCInvalidRequestException:
            return MQTTRPC10Response(error=JSONRPCInvalidRequest()._data)

        return await cls.handle_request(request, service_id, method_id, dispatcher)

    @classmethod
    async def handle_request(cls, request, service_id, method_id, dispatcher):
        """ Handle request data.

        At this moment request has correct jsonrpc format.

        :param dict request: data parsed from request_str.
        :param jsonrpc.dispatcher.Dispatcher dispatcher:

        .. versionadded: 1.8.0

        """

        def response(**kwargs):
            return MQTTRPC10Response(
                _id=request._id, **kwargs)

        try:
            method = dispatcher[(service_id, method_id)]
        except KeyError:
            output = response(error=JSONRPCMethodNotFound()._data)
        else:
            try:
                result = await method(*request.args, **request.kwargs)
            except JSONRPCDispatchException as e:
                output = response(error=e.error._data)
            except Exception as e:
                data = {
                    "type": e.__class__.__name__,
                    "args": e.args,
                    "message": str(e),
                }
                if isinstance(e, TypeError) and is_invalid_params(
                        method, *request.args, **request.kwargs):
                    output = response(
                        error=JSONRPCInvalidParams(data=data)._data)
                else:
                    logger.exception("API Exception: {0}".format(data))
                    output = response(
                        error=JSONRPCServerError(data=data)._data)
            else:
                output = response(result=result)
        finally:
            if not request.is_notification:
                return output
            else:
                return []
"""
End of copy-pasted mqttrpc.manager.MQTTRPCResponseManager
"""


def get_topic_path(*args):
    ret = PurePosixPath(TOPIC_HEADER, *[str(arg) for arg in args])
    return str(ret)


class MQTTConnManager:  # TODO: split to common lib
    _MQTT_CONNECTIONS = {}
    _CLIENT_NAME = "wb-device-manager"

    DEFAULT_MQTT_HOST = "127.0.0.1"
    DEFAULT_MQTT_PORT_STR = "1883"

    @property
    def mqtt_connections(self):
        return type(self)._MQTT_CONNECTIONS

    @property
    def client_name(self):
        return type(self)._CLIENT_NAME

    def parse_mqtt_addr(self, hostport_str=""):
        host, port = hostport_str.split(":", 1)
        return host or self.DEFAULT_MQTT_HOST, int(port or self.DEFAULT_MQTT_PORT_STR, 0)

    def close_mqtt(self, hostport_str):
        client = self.mqtt_connections.get(hostport_str)

        if client:
            client.loop_stop()
            client.disconnect()
            self.mqtt_connections.pop(hostport_str)
            logger.info("Mqtt: close %s", hostport_str)
        else:
            logger.warning("Mqtt connection %s not found in active ones!", hostport_str)

    @contextmanager
    def get_mqtt_connection(self, hostport_str=""):
        hostport_str = hostport_str or "%s:%s" % (self.DEFAULT_MQTT_HOST, self.DEFAULT_MQTT_PORT_STR)
        logger.debug("Looking for open mqtt connection for: %s", hostport_str)
        client = self.mqtt_connections.get(hostport_str)

        if client:
            logger.debug("Found")
            yield client
        else:
            _host, _port = self.parse_mqtt_addr(hostport_str)
            try:
                client = mosquitto.Client(self.client_name)
                # client.enable_logger(logger)
                logger.info("New mqtt connection; host: %s; port: %d", _host, _port)
                client.connect(_host, _port)
                client.loop_start()
                self.mqtt_connections.update({hostport_str : client})
                yield client
            except mosquitto.MQTT_ERR_INVAL as e:
                logger.exception("Loop for '%s' is already running" % hostport_str)
                yield client
            finally:
                logger.info("Registered to atexit hook: close %s", hostport_str)
                atexit.register(lambda: self.close_mqtt(hostport_str))


class Singleton(type):
    _instances = {}
    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


class SRPCClient(rpcclient.TMQTTRPCClient, metaclass=Singleton):
    """
    Stores internal future-like objs (with rpc-call result), filled from outer on_mqtt_message callback
    """
    async def make_rpc_call(self, driver, service, method, params, timeout=10):
        logger.debug("RPC Client -> %s (rpc timeout: %.2fs)", params, timeout)
        response_futurelike = self.call_async(  # not a real future-obj :c
            driver,
            service,
            method,
            params
            )
        response = await make_async(response_futurelike.result)(timeout)  # TODO: fair async rpcclient?
        logger.debug("RPC Client <- %s", response)
        return response


class AsyncModbusInstrument(instruments.SerialRPCBackendInstrument):
    """
    Generic minimalmodbus instrument's logic with mqtt-rpc to wb-mqtt-serial as transport
    (instead of pyserial)
    """

    def __init__(self, port, slaveaddress, **kwargs):
        super().__init__(port, slaveaddress, **kwargs)
        with MQTTConnManager().get_mqtt_connection(hostport_str=self.broker_addr) as conn:
            self.rpc_client = SRPCClient(conn)

    async def _communicate(self, request, number_of_bytes_to_read):
        minimalmodbus._check_string(request, minlength=1, description="request")
        minimalmodbus._check_int(number_of_bytes_to_read)

        min_response_timeout = 0.5  # hardcoded in wb-mqtt-serial's validation

        rpc_request = {
            "response_size": number_of_bytes_to_read,
            "format": "HEX",
            "msg": minimalmodbus._hexencode(request),
            "response_timeout": round(max(self.serial.timeout, min_response_timeout) * 1E3),
            "path": self.serial.port,  # TODO: support modbus tcp in minimalmodbus
            "baud_rate" : self.serial.SERIAL_SETTINGS["baudrate"],
            "parity" : self.serial.SERIAL_SETTINGS["parity"],
            "stop_bits" : self.serial.SERIAL_SETTINGS["stopbits"],
            "data_bits" : 8,
        }

        rpc_call_timeout = 10
        try:
            response = await self.rpc_client.make_rpc_call(
                driver="wb-mqtt-serial",
                service="port",
                method="Load",
                params=rpc_request,
                timeout=rpc_call_timeout
                )
        except rpcclient.MQTTRPCError as e:
            reraise_err = minimalmodbus.NoResponseError if e.code == self.RPC_ERR_STATES["REQUEST_HANDLING"] else RPCCommunicationError
            raise reraise_err from e
        else:
            return minimalmodbus._hexdecode(str(response.get("response", "")))


class MQTTRPCAlreadyProcessingError(JSONRPCServerError):
    CODE = -33100
    MESSAGE = "Task is already executing."


class MQTTRPCMaxTasksProcessingError(JSONRPCServerError):
    CODE = -33200
    MESSAGE = "Max number of tasks are processing! Try again later."


class MQTTServer:
    _NOW_PROCESSING = []
    MAX_TASKS = 4

    def __init__(self, methods_dispatcher, hostport_str=""):
        self.hostport_str = hostport_str
        self.methods_dispatcher = methods_dispatcher

        with MQTTConnManager().get_mqtt_connection(self.hostport_str) as connection:
            self.connection = connection

        self.asyncio_loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.asyncio_loop.add_signal_handler(sig, lambda: self.asyncio_loop.stop())
        self.asyncio_loop.set_debug(True)

        self.rpc_client = SRPCClient(self.connection)

    @property
    def now_processing(self):
        return type(self)._NOW_PROCESSING

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
            self.connection.publish(topic_str, "1", retain=True)
            topic_str += "/+"
            self.connection.subscribe(topic_str)
            logger.debug("Subscribed: %s", topic_str)

    def _on_mqtt_message(self, _client, _userdata, message):
        if mosquitto.topic_matches_sub('/rpc/v1/+/+/+/%s/reply' % self.rpc_client.rpc_client_id, message.topic):
            self.rpc_client.on_mqtt_message(None, None, message)  # reply from mqtt client; filling payload

        else:  # requests to a server
            if self.is_processing(message):
                logger.warning("'%s' is already processing!", message.topic)
                response = MQTTRPC10Response(error=MQTTRPCAlreadyProcessingError()._data)
                self.reply(message, response.json)
            elif len(self.now_processing) < self.MAX_TASKS:
                self.add_to_processing(message)
                asyncio.run_coroutine_threadsafe(self.run_async(message), self.asyncio_loop)
            else:
                logger.warning("Max number of tasks (%d) is running already", len(self.now_processing))
                logger.warning("Doing nothing for '%s'", message.topic)
                response = MQTTRPC10Response(error=MQTTRPCMaxTasksProcessingError()._data)
                self.reply(message, response.json)

    def reply(self, message, payload):
        topic = message.topic + "/reply"
        self.connection.publish(topic, payload, False)

    async def run_async(self, message):
        parts = message.topic.split("/")  # TODO: re?
        service_id, method_id = parts[4], parts[5]

        _now = time.time()
        ret = await AsyncMQTTRPCResponseManager.handle(  # wraps any exception into json-rpc
            message.payload,
            service_id,
            method_id,
            self.methods_dispatcher
            )
        _done = time.time()
        logger.info("Processing '%s' took %.2fs", message.topic, _done - _now)
        self.reply(message, ret.json)
        self.remove_from_processing(message)

    async def loop_forever(self):
        while True:
            await asyncio.sleep(1)

    def setup(self):
        self._subscribe()
        self.connection.on_message = self._on_mqtt_message
        logger.debug("Binded 'on_message' callback")
        self.asyncio_loop.create_task(self.loop_forever())

    def loop(self):
        self.asyncio_loop.run_forever()
