#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re
from collections import defaultdict

from . import logger


class VersionParsingError(Exception):
    pass


def parse_releases(fname: str) -> dict:
    """
    WirenBoard controllers have releases info, stored in file <CONFIG['RELEASES_FNAME']>
    Releases info file usually contains:
        RELEASE_NAME
        SUITE
        TARGET
        REPO_PREFIX
    """
    ret = defaultdict(lambda: None)

    logger.debug("Reading %s for releases info", fname)
    with open(fname, "r", encoding="utf-8") as fp:
        ret.update({k.strip(): v.strip() for k, v in (l.split("=", 1) for l in fp)})
        logger.debug("Got releases info:")
        logger.debug("\t%s", str(ret))
        return ret


def get_release_file_url() -> str:
    return "http://fw-releases.wirenboard.com/fw/by-signature/release-versions.yaml"


def parse_fw_version(endpoint_url: str) -> str:
    """
    Parsing fw version from endpoint url, stored in releases file
    """
    re_str = r".+/(.+)\.wbfw"
    mat = re.match(re_str, endpoint_url)  # matches .../*.wbfw
    if mat:
        return str(mat.group(1))
    raise VersionParsingError("Could not parse fw version from %s by regexp %s" % (endpoint_url, re_str))
