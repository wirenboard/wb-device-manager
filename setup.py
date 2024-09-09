#!/usr/bin/env python3

from setuptools import setup


def get_version():
    with open("debian/changelog", "r", encoding="utf-8") as f:
        return f.readline().split()[1][1:-1]


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
    test_suite="tests",
)
