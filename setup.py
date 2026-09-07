#!/usr/bin/env python3

import os

from setuptools import setup


def get_version():
    return os.environ.get("DEB_VERSION", "0.0.0").split("~")[0].replace("-", "+")


setup(
    name="wb-device-manager",
    version=get_version(),
    author="Vladimir Romanov",
    author_email="v.romanov@wirenboard.ru",
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    description="Wiren Board modbus devices manager",
    license="MIT",
    url="https://github.com/wirenboard/wb-device-manager",
    packages=[
        "wb.device_manager",
    ],
    scripts=[
        "wb-device-manager",
    ],
    test_suite="tests",
)
