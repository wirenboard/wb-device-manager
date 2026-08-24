#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from dataclasses import dataclass
from typing import Optional


@dataclass
class StateError:
    """
    Represents an error published in the /wb-device-manager/state topic.

    Attributes:
        id (str): The ID of the error.
        message (str): The error message.
        metadata (Optional[dict], optional): Additional metadata associated with the error. Defaults to None.
    """

    id: str
    message: str
    metadata: Optional[dict] = None


class GenericStateError(StateError):  # pylint: disable=too-few-public-methods
    ID = "com.wb.device_manager.generic_error"
    MESSAGE = "Internal error. Check logs for more info"

    def __init__(self):
        super().__init__(id=self.ID, message=self.MESSAGE)


class RPCCallTimeoutStateError(GenericStateError):  # pylint: disable=too-few-public-methods
    ID = "com.wb.device_manager.rpc_call_timeout_error"
    MESSAGE = "RPC call to wb-mqtt-serial timed out. Check, wb-mqtt-serial is running"


class FailedScanStateError(GenericStateError):  # pylint: disable=too-few-public-methods
    ID = "com.wb.device_manager.failed_to_scan_error"
    MESSAGE = "Some ports failed to scan. Check logs for more info"

    def __init__(self, failed_ports):
        super().__init__()
        self.metadata = {"failed_ports": failed_ports}


class ReadFWSignatureDeviceError(GenericStateError):  # pylint: disable=too-few-public-methods
    ID = "com.wb.device_manager.device.read_fw_signature_error"
    MESSAGE = "Failed to read FW signature."
