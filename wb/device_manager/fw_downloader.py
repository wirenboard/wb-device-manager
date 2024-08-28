#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import socket
import urllib
from dataclasses import dataclass
from functools import lru_cache

import yaml

from . import logger
from .releases import VersionParsingError, get_release_file_url, parse_fw_version


class WBRemoteStorageError(Exception):
    pass


class RemoteFileReadingError(WBRemoteStorageError):
    pass


class RemoteFileDownloadingError(WBRemoteStorageError):
    pass


class NoReleasedFwError(Exception):
    pass


@dataclass
class ReleasedFirmware:
    version: str
    endpoint: str


def get_request(url_path, tries=3):
    """
    Sending GET request to url; returning response's content.

    :param url_path: url, request will be sent to
    :type url_path: str
    :return: response's content
    :rtype: bytestring
    """
    logger.debug("GET: %s", url_path)
    for _ in range(tries):
        try:
            return urllib.request.urlopen(url_path, timeout=1.5)
        except (urllib.error.URLError, urllib.error.HTTPError, socket.error):
            continue
    raise WBRemoteStorageError(url_path)


@lru_cache(maxsize=10)
def read_remote_file(url_path, coding="utf-8"):
    ret = ""
    try:
        ret = get_request(url_path)
        ret = str(ret.read().decode(coding)).strip()
    except Exception as e:
        raise RemoteFileReadingError from e
    if ret:
        return ret
    raise RemoteFileReadingError(f"{url_path} is empty!")


@lru_cache(maxsize=3)
def download_remote_file(url_path, saving_dir):
    """
    Downloading a file from direct url
    """
    try:
        ret = get_request(url_path)
        content = ret.read()
    except Exception as e:
        logger.debug("%s", e)
        raise RemoteFileDownloadingError from e

    os.makedirs(saving_dir, exist_ok=True)

    logger.debug("Trying to get fname from content-disposition")
    default_fname = ret.info().get("Content-Disposition")
    fname = default_fname.split("filename=")[1].strip("\"'") if default_fname else "tmp.wbfw"
    logger.debug("Got fname: %s", str(fname))
    if fname:
        file_path = os.path.join(saving_dir, fname)
        logger.debug("%s => %s", url_path, file_path)
    else:
        raise RemoteFileDownloadingError(
            "Could not construct fpath, where to save fw. Fname should be specified!"
        )

    try:
        with open(file_path, "wb+") as fh:
            fh.write(content)
            return file_path
    except Exception as e:
        raise RemoteFileDownloadingError from e


def get_released_fw(fw_signature: str, release_suite: str) -> ReleasedFirmware:
    url = get_release_file_url()
    logger.debug("Looking to %s (suite: %s)", url, release_suite)
    try:
        contents = read_remote_file(url)
        fw_endpoint = str(
            yaml.safe_load(contents).get("releases", {}).get(fw_signature, {}).get(release_suite)
        )
        if fw_endpoint:
            fw_endpoint = "http://fw-releases.wirenboard.com/" + fw_endpoint
            fw_version = parse_fw_version(fw_endpoint)
            logger.debug(
                "FW version for %s on release %s: %s (endpoint: %s)",
                fw_signature,
                release_suite,
                fw_version,
                fw_endpoint,
            )
            return ReleasedFirmware(fw_version, fw_endpoint)
    except RemoteFileReadingError as e:
        logger.warning('No released fw for "%s" in "%s": %s', fw_signature, url, e)
    except VersionParsingError as e:
        logger.exception(e)
    raise NoReleasedFwError('Released FW not found for "%s", release: %s' % (fw_signature, release_suite))
