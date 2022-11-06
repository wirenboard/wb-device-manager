#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import atexit
from contextlib import contextmanager
import paho.mqtt.client as mosquitto
from wb_modbus import minimalmodbus
from . import logger


class RPCError(minimalmodbus.MasterReportedException):
    pass

class RPCConnectionError(RPCError):
    pass

class RPCCommunicationError(RPCError):
    pass


class MQTTClient:  # TODO: split to common lib
    _MQTT_CONNECTIONS = {}
    _CLIENT_NAME = "wb-device-manager.client"

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
        logger.debug("Get mqtt connection: %s", hostport_str)
        client = self.mqtt_connections.get(hostport_str)

        if client:
            yield client
        else:
            try:
                client = mosquitto.Client(self.client_name)
                _host, _port = self.parse_mqtt_addr(hostport_str)
                logger.debug("New mqtt connection; host: %s; port: %d", _host, _port)
                client.connect(_host, _port)
                client.loop_start()
                self.mqtt_connections.update({hostport_str : client})
                yield client
            except (rpcclient.TimeoutError, OSError) as e:
                raise RPCConnectionError from e
            finally:
                atexit.register(lambda: self.close_mqtt(hostport_str))


if __name__ == "__main__":
    client = MQTTClient()
    with client.get_mqtt_connection() as client:
        client.publish("/test_topic", "test_payload")
