#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import atexit
import time
from contextlib import contextmanager
from concurrent import futures
from threading import current_thread, Lock
import paho.mqtt.client as mosquitto
from mqttrpc import MQTTRPCResponseManager, client as rpcclient
from wb_modbus import minimalmodbus
from . import logger, get_topic_path


class RPCError(minimalmodbus.MasterReportedException):
    pass

class RPCConnectionError(RPCError):
    pass

class RPCCommunicationError(RPCError):
    pass


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
            try:
                client = mosquitto.Client(self.client_name)
                client.enable_logger(logger)
                _host, _port = self.parse_mqtt_addr(hostport_str)
                logger.debug("New mqtt connection; host: %s; port: %d", _host, _port)
                client.connect(_host, _port)
                client.loop_start()
                self.mqtt_connections.update({hostport_str : client})
                yield client
            except (rpcclient.TimeoutError, OSError) as e:
                raise RPCConnectionError from e
            finally:
                logger.debug("Registered to atexit hook: close %s", hostport_str)
                atexit.register(lambda: self.close_mqtt(hostport_str))


class MQTTServer:
    _NOW_PROCESSING = []

    def __init__(self, methods_dispatcher, hostport_str=""):
        self.hostport_str = hostport_str
        self.methods_dispatcher = methods_dispatcher
        self.mutex = Lock()
        self.executor = futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="Worker")
        atexit.register(lambda: self.executor.shutdown(wait=False, cancel_futures=True))
        with MQTTConnManager().get_mqtt_connection(self.hostport_str) as connection:
            self.connection = connection

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
            topic_str = get_topic_path(service or "_", method or "_")
            self.connection.publish(topic_str, "1", retain=True)
            topic_str += "/+"
            self.connection.subscribe(topic_str)
            logger.debug("Subscribed: %s", topic_str)

    def _on_mqtt_message(self, _client, _userdata, message):
        if not self.is_processing(message):
            self.run_async(message)
        else:
            logger.warning("'%s' is already processing!", message.topic)  # TODO: send to mqtt

    def run_async(self, message):
        parts = message.topic.split("/")  # TODO: re?
        service_id = parts[4]
        method_id = parts[5]
        client_id = parts[6]
        logger.debug("service: %s method: %s client: %s", service_id, method_id, client_id)

        def _execute():  # Runs on worker's thread
            thread_name = current_thread().name
            logger.debug("Processing '%s' started on %s...", message.topic, thread_name)
            self.add_to_processing(message.topic)
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
            self.remove_from_processing(message.topic)
            self.connection.publish(
                get_topic_path(service_id, method_id, client_id, "reply"),
                fut.result().json,
                False
            )

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
