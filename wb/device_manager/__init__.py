import asyncio
import logging
from functools import wraps, partial

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.NullHandler())

TOPIC_HEADER = "/rpc/v1/wb-device-manager/"

def make_async(func):
    @wraps(func)
    async def schedule(*args, loop=asyncio.get_event_loop(), executor=None, **kwargs):
        return await loop.run_in_executor(executor, partial(func, *args, **kwargs))
    return schedule
