#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.NullHandler())


def get_topic_path(*args):
    header = "/rpc/v1/wb-device-manager/"
    path = "/".join([str(arg).replace(" ", "") for arg in args])
    return header + path
