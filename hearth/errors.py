"""One decorator for the background work that must never take the server down.

`@if_errors` logs the exception with its traceback and returns None, so a
failed poll, a bad feed or a Jellyfin hiccup is a line in the journal
(`journalctl -u hearth`), not a dead task. Callers treat None as "didn't work".
"""
import asyncio
import functools
import logging

log = logging.getLogger('hearth')


def if_errors(func):
    if asyncio.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('%s failed', func.__qualname__)
                return None
        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            log.exception('%s failed', func.__qualname__)
            return None
    return sync_wrapper
