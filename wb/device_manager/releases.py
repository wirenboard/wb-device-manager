#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re

from . import logger


class VersionParsingError(Exception):
    pass


def parse_releases(fname: str) -> dict[str, str]:
    """
    WirenBoard controllers have releases info, stored in file <CONFIG['RELEASES_FNAME']>
    Releases info file usually contains:
        RELEASE_NAME
        SUITE
        TARGET
        REPO_PREFIX
    """
    logger.debug("Reading %s for releases info", fname)
    with open(fname, "r", encoding="utf-8") as fp:
        ret = {k.strip(): v.strip() for k, v in (l.split("=", 1) for l in fp)}
        logger.debug("Got releases info:")
        logger.debug("\t%s", str(ret))
        return ret


def parse_fw_version(endpoint_url: str) -> str:
    """
    Parsing fw version from endpoint url, stored in releases file
    """
    re_str = r".+/(.+)\.wbfw"
    mat = re.match(re_str, endpoint_url)  # matches .../*.wbfw
    if mat:
        return str(mat.group(1))
    raise VersionParsingError(f"Could not parse fw version from {endpoint_url} by regexp {re_str}")
