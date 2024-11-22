#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from dataclasses import dataclass
from typing import Optional


@dataclass
class StateError:
    id: str
    message: str
    metadata: Optional[dict] = None


class GenericStateError(StateError):
    ID = "com.wb.device_manager.generic_error"
    MESSAGE = "Internal error. Check logs for more info"

    def __init__(self):
        super().__init__(id=self.ID, message=self.MESSAGE)


class RPCCallTimeoutStateError(GenericStateError):
    ID = "com.wb.device_manager.rpc_call_timeout_error"
    MESSAGE = "RPC call to wb-mqtt-serial timed out. Check, wb-mqtt-serial is running"


class FailedScanStateError(GenericStateError):
    ID = "com.wb.device_manager.failed_to_scan_error"
    MESSAGE = "Some ports failed to scan. Check logs for more info"

    def __init__(self, failed_ports):
        super().__init__()
        self.metadata = {"failed_ports": failed_ports}


class ReadFWVersionDeviceError(GenericStateError):
    ID = "com.wb.device_manager.device.read_fw_version_error"
    MESSAGE = "Failed to read FW version."


class ReadFWSignatureDeviceError(GenericStateError):
    ID = "com.wb.device_manager.device.read_fw_signature_error"
    MESSAGE = "Failed to read FW signature."


class ReadDeviceSignatureDeviceError(GenericStateError):
    ID = "com.wb.device_manager.device.read_device_signature_error"
    MESSAGE = "Failed to read device signature."


class ReadSerialParamsDeviceError(GenericStateError):
    ID = "com.wb.device_manager.device.read_serial_params_error"
    MESSAGE = "Failed to read serial params from device."


class DeviceResponseTimeoutError(GenericStateError):
    ID = "com.wb.device_manager.device.response_timeout_error"
    MESSAGE = "Response timeout error"


class FileDownloadError(GenericStateError):
    ID = "com.wb.device_manager.download_error"
    MESSAGE = "Failed to download file."
