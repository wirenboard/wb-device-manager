#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import atexit
from contextlib import contextmanager
from concurrent import futures
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

    def __init__(self, methods_dispatcher, hostport_str=""):
        self.hostport_str = hostport_str
        self.methods_dispatcher = methods_dispatcher
        self.executor = futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="worker_")
        with MQTTConnManager().get_mqtt_connection(self.hostport_str) as connection:
            self.connection = connection

    def _subscribe(self):
        logger.debug("Subscribing to: %s", str(self.methods_dispatcher.keys()))
        for service, method in self.methods_dispatcher.keys():
            topic_str = get_topic_path(service or "_", method or "_")
            self.connection.publish(topic_str, "1", retain=True)
            topic_str += "/+"
            self.connection.subscribe(topic_str)
            logger.debug("Subscribed: %s", topic_str)

    def _on_mqtt_message(self, _client, _userdata, message):
        logger.debug("Topic: %s", message.topic)
        parts = message.topic.split("/")  # TODO: re
        service_id = parts[4]
        method_id = parts[5]
        client_id = parts[6]
        logger.debug("service: %s method: %s client: %s", service_id, method_id, client_id)

        # TODO: a queue with "already executing" ?
        _future = self.executor.submit(
            MQTTRPCResponseManager.handle,
            message.payload,
            service_id,
            method_id,
            self.methods_dispatcher
            )
        _future.add_done_callback(
            lambda fut: self.connection.publish(
                get_topic_path(service_id, method_id, client_id, "reply"),
                fut.result().json,
                False
            )
        )

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
