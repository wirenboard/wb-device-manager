#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import atexit
import time
import signal
from contextlib import contextmanager
from pathlib import PurePosixPath
from concurrent import futures
from threading import current_thread, Lock
import paho.mqtt.client as mosquitto
from mqttrpc import MQTTRPCResponseManager, client as rpcclient
from mqttrpc.protocol import MQTTRPC10Response
from jsonrpc.exceptions import JSONRPCServerError
from wb_modbus import minimalmodbus
from . import logger, shutdown_event, TOPIC_HEADER


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
            logger.debug("Mqtt: close %s", hostport_str)
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
                logger.debug("New mqtt connection; host: %s; port: %d", _host, _port)
                client.connect(_host, _port)
                client.loop_start()
                self.mqtt_connections.update({hostport_str : client})
                yield client
            finally:
                logger.debug("Registered to atexit hook: close %s", hostport_str)
                atexit.register(lambda: self.close_mqtt(hostport_str))


class MQTTRPCAlreadyProcessingError(JSONRPCServerError):
    CODE = -33100
    MESSAGE = "Task is already executing"


class MQTTServer:
    _NOW_PROCESSING = []

    def __init__(self, methods_dispatcher, hostport_str=""):
        self.hostport_str = hostport_str
        self.methods_dispatcher = methods_dispatcher
        self.mutex = Lock()
        self.executor = futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="Worker")
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
        with MQTTConnManager().get_mqtt_connection(self.hostport_str) as connection:
            self.connection = connection

    def shutdown(self, *args):
        shutdown_event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.connection.loop(timeout=1.0)  # to publish shutdown
        self.connection.disconnect()

    @property
    def now_processing(self):
        return type(self)._NOW_PROCESSING

    def add_to_processing(self, mqtt_message):
        with self.mutex:
            self.now_processing.append((mqtt_message.topic, mqtt_message.payload))

    def remove_from_processing(self, mqtt_message):
        with self.mutex:
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
        if self.is_processing(message):
            logger.debug("'%s' is already processing!", message.topic)
            response = MQTTRPC10Response(error=MQTTRPCAlreadyProcessingError()._data)
            self.reply(message, response.json)
        else:
            self.run_async(message)

    def reply(self, message, payload):
        topic = message.topic + "/reply"
        self.connection.publish(topic, payload, False)

    def run_async(self, message):
        parts = message.topic.split("/")  # TODO: re?
        service_id, method_id = parts[4], parts[5]

        def _execute():  # Runs on worker's thread
            thread_name = current_thread().name
            logger.debug("Processing '%s' started on %s...", message.topic, thread_name)
            self.add_to_processing(message)
            _now = time.time()
            ret = MQTTRPCResponseManager.handle(  # wraps any exception into json-rpc
                message.payload,
                service_id,
                method_id,
                self.methods_dispatcher
                )
            _done = time.time()
            logger.debug("Processing '%s' took %.2fs on %s", message.topic, _done - _now, thread_name)
            return ret

        def _publish(fut):
            self.remove_from_processing(message)
            self.reply(message, fut.result().json)

        # assign task to free worker
        _future = self.executor.submit(_execute)
        _future.add_done_callback(_publish)

    def setup(self):
        self._subscribe()
        self.connection.on_message = self._on_mqtt_message
        logger.debug("Binded 'on_message' callback")

    def loop(self):
        self.connection.loop_forever()


if __name__ == "__main__":
    client = MQTTConnManager()
    with client.get_mqtt_connection() as client:
        client.publish("/test_topic", "test_payload")
