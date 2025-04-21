#!/usr/bin/env python
# -*- coding: utf-8 -*-


import time
from functools import lru_cache


# Taken from https://stackoverflow.com/questions/31771286/python-in-memory-cache-with-time-to-live
def ttl_lru_cache(seconds_to_live: int, maxsize: int = 10):
    """
    A decorator that provides a time-aware Least Recently Used (LRU) caching mechanism.

    This decorator caches the results of a function call for a specified duration.
    The cache is invalidated and refreshed every `seconds_to_live` seconds.
    It also limits the number of cached items to `maxsize`.

    Args:
        seconds_to_live (int): The duration (in seconds) for which a cached result is valid.
        maxsize (int, optional): The maximum number of items to store in the cache. Defaults to 10.

    Returns:
        Callable: A decorated function with TTL-based LRU caching applied.

    Usage:
        @ttl_lru_cache(seconds_to_live=60, maxsize=100)
        def my_function(arg1, arg2):
            # Function implementation
            pass
    """

    def wrapper(func):

        # lru_cache uses a hash of all the arguments to determine a cache hit.
        # A dummy argument __ttl is used to group calls with the same arguments but at different times.
        # __ttl is the current time divided by the seconds_to_live and changes every seconds_to_live seconds.
        # So cache invalidates every seconds_to_live seconds.
        @lru_cache(maxsize)
        def inner(__ttl, *args, **kwargs):
            return func(*args, **kwargs)

        return lambda *args, **kwargs: inner(time.time() // seconds_to_live, *args, **kwargs)

    return wrapper
