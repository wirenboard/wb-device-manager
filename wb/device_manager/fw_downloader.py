#!/usr/bin/env python
# -*- coding: utf-8 -*-


from dataclasses import dataclass

import httplib2
import yaml

from . import logger
from .releases import VersionParsingError, parse_fw_version
from .ttl_lru_cache import ttl_lru_cache


class WBRemoteStorageError(Exception):
    pass


class RemoteFileDownloadingError(WBRemoteStorageError):
    pass


class RemoteFileReadingError(WBRemoteStorageError):
    pass


class NoReleasedFwError(Exception):
    pass


@dataclass
class ReleasedBinary:
    version: str
    endpoint: str


FW_RELEASES_BASE_URL = "https://fw-releases.wirenboard.com"


class BinaryDownloader:
    def __init__(self, http: httplib2.Http) -> None:
        self._http = http

    def read_text_file(self, url: str) -> str:
        """
        Reads the content of a text file from the given URL.

        Args:
            url (str): The URL of the text file.

        Returns:
            str: The content of the text file.

        Raises:
            RemoteFileReadingError: If the file is empty or cannot be decoded.
            RemoteFileDownloadingError: If there is an error downloading the remote file.
        """
        try:
            content = self.download_file(url).decode("utf-8").strip()
        except UnicodeDecodeError as err:
            raise RemoteFileReadingError(f"Failed to read {url}: {err}") from err
        if content:
            return content
        raise RemoteFileReadingError(f"{url} is empty!")

    def download_file(self, url: str) -> bytes:
        """
        Downloads a file from the specified URL.

        Args:
            url (str): The URL of the file to download.

        Returns:
            bytes: The content of the downloaded file.

        Raises:
            RemoteFileDownloadingError: If the file fails to download.

        """
        try:
            (_headers, content) = self._http.request(url, "GET")
            return content
        except Exception as err:
            raise RemoteFileDownloadingError(f"Failed to download {url}: {err}") from err


# Cache information about released firmware for 10 minutes
@ttl_lru_cache(seconds_to_live=600, maxsize=100)
def get_released_fw(
    fw_signature: str, release_suite: str, binary_downloader: BinaryDownloader
) -> ReleasedBinary:
    """
    Retrieves the released firmware for a given firmware signature and release suite.

    Args:
        fw_signature (str): The firmware signature.
        release_suite (str): The release suite.
        binary_downloader (BinaryDownloader): The binary downloader object.

    Returns:
        ReleasedBinary: The released binary object containing the firmware version and endpoint.

    Raises:
        NoReleasedFwError: If the firmware is not found.
    """

    url = f"{FW_RELEASES_BASE_URL}/fw/by-signature/release-versions.yaml"
    logger.debug("Looking to %s (suite: %s)", url, release_suite)
    try:
        contents = binary_downloader.read_text_file(url)
        fw_endpoint = yaml.safe_load(contents).get("releases", {}).get(fw_signature, {}).get(release_suite)
        if fw_endpoint:
            fw_endpoint = f"{FW_RELEASES_BASE_URL}/{fw_endpoint}"
            fw_version = parse_fw_version(fw_endpoint)
            logger.debug(
                "FW version for %s on release %s: %s (endpoint: %s)",
                fw_signature,
                release_suite,
                fw_version,
                fw_endpoint,
            )
            return ReleasedBinary(fw_version, fw_endpoint)
    except WBRemoteStorageError as e:
        logger.warning('No released fw for "%s" in "%s": %s', fw_signature, url, e)
    except VersionParsingError as e:
        logger.exception(e)
    except yaml.YAMLError as e:
        message = f"Failed to parse YAML from {url}: {e}"
        logger.warning(message)
    raise NoReleasedFwError(f"Released FW not found for {fw_signature}, release: {release_suite}")


# Bootloader changes rarely, so we can cache it for a longer time
@ttl_lru_cache(seconds_to_live=1800, maxsize=100)
def get_bootloader_info(fw_signature: str, binary_downloader: BinaryDownloader) -> ReleasedBinary:
    bootloader_url_prefix = f"{FW_RELEASES_BASE_URL}/bootloader/by-signature/{fw_signature}/main"
    bootloader_latest_txt_url = f"{bootloader_url_prefix}/latest.txt"
    version = binary_downloader.read_text_file(bootloader_latest_txt_url)
    endpoint = f"{bootloader_url_prefix}/{version}.wbfw"
    logger.debug("Bootloader for %s: %s (endpoint: %s)", fw_signature, version, endpoint)
    return ReleasedBinary(version, endpoint)
