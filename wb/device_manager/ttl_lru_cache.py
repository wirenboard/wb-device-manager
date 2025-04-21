#!/usr/bin/env python
# -*- coding: utf-8 -*-


import time
from functools import lru_cache


# Taken from https://stackoverflow.com/questions/31771286/python-in-memory-cache-with-time-to-live
def ttl_lru_cache(seconds_to_live: int, maxsize: int = 10):
    """
    Time aware lru caching
    """

    def wrapper(func):

        @lru_cache(maxsize)
        def inner(__ttl, *args, **kwargs):
            # Note that __ttl is not passed down to func,
            # as it's only used to trigger cache miss after some time
            return func(*args, **kwargs)

        return lambda *args, **kwargs: inner(time.time() // seconds_to_live, *args, **kwargs)

    return wrapper
