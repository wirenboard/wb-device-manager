import unittest

from parameterized import parameterized

from wb.device_manager.version_comparison import component_firmware_is_newer, firmware_is_newer


class TestVersionComparsion(unittest.TestCase):
    @parameterized.expand(
        [
            ("", "M1.01D", True),
            ("M1.03D", "M1.01D", True),
        ]
    )
    def test_component_comparsion(self, current_version, available_version, result):
        assert component_firmware_is_newer(current_version, available_version) == result

    @parameterized.expand(
        [
            ("", "1.2.3", False),
            ("1.2.3", "", False),
            ("1.2.3-rc1", "1.2.3-rc10", True),
            ("1.2.3-rc10", "1.2.3", True),
            ("1.2.3", "1.2.3+wb1", True),
            ("1.2.3+wb1", "1.2.3+wb10", True),
            ("1.2.3-rc1", "1.2.3-rc1", False),
            ("1.2.3+wb10", "1.2.3-rc1", False),
            ("1.2.3", "1.2.4", True),
        ]
    )
    def test_firmware_comparsion(self, current_version, available_version, result):
        assert firmware_is_newer(current_version, available_version) == result
