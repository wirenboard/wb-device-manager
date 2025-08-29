from packaging.version import Version


def component_firmware_is_newer(current_version: str, available_version: str) -> bool:
    return available_version and current_version != available_version


def firmware_is_newer(current_version: str, available_version: str) -> bool:
    if not current_version or not available_version:
        return False

    return Version(available_version) > Version(current_version)
