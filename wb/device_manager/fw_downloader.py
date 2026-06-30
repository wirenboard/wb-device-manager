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


def _get_released_binary(
    releases_url: str, fw_signature: str, release_suite: str, binary_downloader: BinaryDownloader
) -> ReleasedBinary:
    """
    Looks up the released firmware/bootloader for a signature and suite in a
    by-signature/release-versions.yaml index (keyed by signature, then suite).

    Args:
        releases_url (str): URL of the release-versions.yaml index.
        fw_signature (str): The firmware signature.
        release_suite (str): The release suite (e.g. "stable"/"testing").
        binary_downloader (BinaryDownloader): The binary downloader object.

    Returns:
        ReleasedBinary: The released binary with its version and endpoint.

    Raises:
        NoReleasedFwError: If nothing is released for the signature/suite.
    """
    logger.debug("Looking to %s (suite: %s)", releases_url, release_suite)
    try:
        contents = binary_downloader.read_text_file(releases_url)
        endpoint = yaml.safe_load(contents).get("releases", {}).get(fw_signature, {}).get(release_suite)
        if endpoint:
            endpoint = f"{FW_RELEASES_BASE_URL}/{endpoint}"
            version = parse_fw_version(endpoint)
            logger.debug(
                "Released binary for %s on release %s: %s (endpoint: %s)",
                fw_signature,
                release_suite,
                version,
                endpoint,
            )
            return ReleasedBinary(version, endpoint)
    except WBRemoteStorageError as e:
        logger.warning('No released binary for "%s" in "%s": %s', fw_signature, releases_url, e)
    except VersionParsingError as e:
        logger.exception(e)
    except yaml.YAMLError as e:
        logger.warning("Failed to parse YAML from %s: %s", releases_url, e)
    raise NoReleasedFwError(f"Released binary not found for {fw_signature}, release: {release_suite}")


# Cache information about released firmware for 10 minutes
@ttl_lru_cache(seconds_to_live=600, maxsize=100)
def get_released_fw(
    fw_signature: str, release_suite: str, binary_downloader: BinaryDownloader
) -> ReleasedBinary:
    """Released firmware for a signature and suite (fw/by-signature/release-versions.yaml)."""
    url = f"{FW_RELEASES_BASE_URL}/fw/by-signature/release-versions.yaml"
    return _get_released_binary(url, fw_signature, release_suite, binary_downloader)


# Bootloader changes rarely, so we can cache it for a longer time
@ttl_lru_cache(seconds_to_live=1800, maxsize=100)
def get_released_bootloader(
    fw_signature: str, release_suite: str, binary_downloader: BinaryDownloader
) -> ReleasedBinary:
    """Released bootloader for a signature and suite (boot/by-signature/release-versions.yaml)."""
    url = f"{FW_RELEASES_BASE_URL}/boot/by-signature/release-versions.yaml"
    return _get_released_binary(url, fw_signature, release_suite, binary_downloader)
