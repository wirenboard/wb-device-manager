#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
from argparse import ArgumentParser
from sys import argv, stderr, stdout

from mqttrpc import Dispatcher
from wb_common.mqtt_client import DEFAULT_BROKER_URL, MQTTClient

from . import logger, mqtt_rpc
from .bus_scan import BusScanner
from .fw_update_proxy import FirmwareUpdateProxy

EXIT_INVALIDARGUMENT = 2
EXIT_FAILURE = 1

MQTT_CLIENT_NAME = "wb-device-manager"


class RetcodeArgParser(ArgumentParser):
    def error(self, message):
        self.print_usage(stderr)
        self.exit(EXIT_INVALIDARGUMENT, f"{self.prog}: error: {message}\n")


def main(args=argv):  # pylint: disable=dangerous-default-value, too-many-locals

    parser = RetcodeArgParser(description="Wiren Board serial devices manager")
    parser.add_argument(
        "-d",
        "--debug",
        dest="log_level",
        action="store_const",
        default=logging.INFO,
        const=logging.DEBUG,
        help="Set log_level to debug",
    )
    parser.add_argument(
        "-b",
        "--broker",
        "--broker_url",
        dest="broker_url",
        type=str,
        help="MQTT broker url",
        default=DEFAULT_BROKER_URL,
    )
    args = parser.parse_args(argv[1:])

    # setup systemd logger
    formatter = logging.Formatter("[%(levelname)s] %(message)s")
    handler = logging.StreamHandler(stream=stdout)
    handler.setFormatter(formatter)
    handler.setLevel(args.log_level)
    logger.addHandler(handler)

    mqtt_connection = MQTTClient(MQTT_CLIENT_NAME, args.broker_url)
    rpc_client = mqtt_rpc.SRPCClient(mqtt_connection)
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)

    if args.log_level == logging.DEBUG:
        event_loop.set_debug(True)

    bus_scanner = BusScanner(mqtt_connection, rpc_client, event_loop)
    fw_updater = FirmwareUpdateProxy(rpc_client, mqtt_connection)

    async_callables_mapping = {
        ("bus-scan", "Start"): bus_scanner.launch_bus_scan,
        ("bus-scan", "Stop"): bus_scanner.stop_bus_scan,
        ("fw-update", "GetFirmwareInfo"): fw_updater.get_firmware_info,
        ("fw-update", "Update"): fw_updater.update_software,
        ("fw-update", "ClearError"): fw_updater.clear_error,
        ("fw-update", "Restore"): fw_updater.restore_firmware,
    }

    server = mqtt_rpc.AsyncMQTTServer(
        methods_dispatcher=Dispatcher(async_callables_mapping),
        mqtt_connection=mqtt_connection,
        mqtt_url_str=args.broker_url,
        rpc_client=rpc_client,
        bus_scanner=bus_scanner,
        fw_updater=fw_updater,
        asyncio_loop=event_loop,
    )

    try:
        server.setup()
        fw_updater.start()
    except Exception:  # pylint: disable=broad-exception-caught
        ec = EXIT_FAILURE
        logger.exception("Exiting with %d", ec)
        return ec
    return server.run()
