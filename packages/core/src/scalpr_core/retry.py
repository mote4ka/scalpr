import time
import functools
import asyncio
from scalpr_core.logger import logger


def sync_retry(max_retries=4, delay=0.5, backoff=6, exceptions=(Exception,)):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            current_delay = delay
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    logger.error(f"{repr(e)} at func {func}!")
                    if attempt == max_retries:
                        raise RuntimeError(f"max retries exceeded ({max_retries})")
                    logger.debug(f"{func} failed, retrying... remain attempts {max_retries-attempt}")
                    time.sleep(current_delay)
                    current_delay *= backoff
        return wrapper
    return decorator

def retry(max_retries=3, delay=0, backoff=0, exceptions=(Exception,)):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            current_delay = delay
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    logger.error(f"{repr(e)} at func {func.__name__}!")
                    if attempt == max_retries:
                        raise RuntimeError(f"max retries exceeded ({max_retries})")
                    logger.debug(f"{func.__name__} failed, retrying... remain attempts {max_retries-attempt}")
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff
        return wrapper
    return decorator