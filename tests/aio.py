"""Running a coroutine to completion for the tests of `AsyncSFTPPath`."""

from __future__ import annotations

import asyncio
import sys


def run(coroutine):
    """`asyncio.run()`, on the selector loop on Windows: the proactor loop logs
    from its pipe transports when a connection is closed at exit."""
    if sys.platform != "win32":
        return asyncio.run(coroutine)
    loop = asyncio.SelectorEventLoop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(_settle(pending))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


async def _settle(tasks):
    await asyncio.gather(*tasks, return_exceptions=True)
