#!/usr/bin/env python3

from setuptools import setup

setup(name = "wb-device-manager",
      version = "0.1",
      author = "Vladimir Romanov",
      author_email = "v.romanov@wirenboard.ru",
      description = "Wiren Board modbus devices manager",
      url = "https://github.com/wirenboard/wb-device-manager",
      packages = ["wb_device_manager",],
      test_suite = "tests"
)
