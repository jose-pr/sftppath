"""The shared background event loop the sync API runs asyncssh on.

asyncssh is asyncio-only end to end (connect() and every SFTPClient and
SFTPClientFile method are coroutines) while `SFTPPath` is sync. One shared loop
in a daemon thread, started lazily on first use, lets calls from any thread
share one persistent connection. `asyncio.run()` per call would create and
destroy a loop every time and could not hold a connection open; a loop per
calling thread would need connections shared across loops, which asyncssh does
not support, and would only reimplement paramiko's thread-keyed cache.
"""

from __future__ import annotations

import asyncio as _asyncio
import concurrent.futures as _futures
import os as _os
import sys as _sys
import threading as _thread
import typing as _ty

_loop: "_asyncio.AbstractEventLoop | None" = None
_loop_thread: "_thread.Thread | None" = None
_loop_pid: "int | None" = None
_loop_lock = _thread.Lock()

#: Default wall-clock bound, in seconds, on one SFTP request (a stat, an
#: open, a connect) run through `_run()`. Whole-tree operations and
#: streaming file reads/writes are not bounded by it.
_DEFAULT_TIMEOUT = 60.0


def _new_loop() -> "_asyncio.AbstractEventLoop":
    if _sys.platform == "win32":
        # The Proactor loop is only needed for subprocess pipes, which this bridge never
        # uses, and its pipe transports log a benign "Exception ignored in ... __del__"
        # when collected after loop shutdown; the selector loop does not.
        return _asyncio.SelectorEventLoop()
    return _asyncio.new_event_loop()


def _ensure_loop() -> "_asyncio.AbstractEventLoop":
    global _loop, _loop_thread, _loop_pid
    pid = _os.getpid()
    with _loop_lock:
        if _loop is not None and _loop_pid == pid:
            return _loop
        # First call, or a fork()'d child (the Linux multiprocessing default) that
        # inherited a dead loop thread and unusable cached connections; paramiko's
        # per-thread connections have the same problem, this is easier to hit.
        loop = _new_loop()
        thread = _thread.Thread(
            target=loop.run_forever, name="sftppath-asyncssh-loop", daemon=True
        )
        thread.start()
        _loop, _loop_thread, _loop_pid = loop, thread, pid
        # Entries cached on the old (dead, in a forked child) loop cannot be closed
        # through it, so the references are dropped. Fires only on a PID change, never
        # on the lazy first call (the cache is empty then).
        from ._cache import _CACHE

        _CACHE.reset()
        return loop


def _on_loop_thread() -> bool:
    thread = _loop_thread
    return thread is not None and thread is _thread.current_thread()


_UNSET_TIMEOUT: _ty.Any = object()

#: Longest, in seconds, `_run()` waits for a coroutine it has cancelled to
#: finish unwinding (its cleanup requests included) before it raises anyway.
_CANCEL_GRACE = 5.0


def _settle(outcome: "_futures.Future", task: "_asyncio.Task") -> None:
    """Hand a finished bridge-loop task's outcome to the thread waiting on
    `outcome`."""
    if task.cancelled():
        outcome.cancel()
    elif task.exception() is not None:
        outcome.set_exception(task.exception())
    else:
        outcome.set_result(task.result())


def _run(coro, timeout: "float | None" = _UNSET_TIMEOUT):
    """Run `coro` on the bridge loop and block the calling thread for its
    result.

    `timeout` (default `_DEFAULT_TIMEOUT`, read at call time) is meant for a
    single request -- without one, a half-dead TCP connection or a server
    that never answers blocks the caller forever. Pass `None` for anything
    whose duration grows with the data (tree operations, streaming
    reads/writes). On timeout the builtin `TimeoutError` is raised on every
    Python version.

    Whatever stops the wait before `coro` is done -- the timeout, or an
    exception delivered to the calling thread such as `KeyboardInterrupt` --
    cancels `coro` on the loop and waits (up to `_CANCEL_GRACE` seconds) for
    it to finish unwinding, so a call that has raised has stopped.

    Raises `RuntimeError` immediately when called on the bridge-loop thread
    itself (a sync `Path` method inside a coroutine or callback running
    there): blocking would deadlock the loop the coroutine needs. Also
    raised once the interpreter is finalizing: the daemon loop thread can no
    longer run, so waiting would only hang shutdown (for `timeout`, or for
    ever without one) -- e.g. a file left open at module scope.
    """
    if timeout is _UNSET_TIMEOUT:
        timeout = _DEFAULT_TIMEOUT
    if _sys.is_finalizing():
        if _asyncio.iscoroutine(coro):
            coro.close()
        raise RuntimeError(
            "SFTP request made while the interpreter is shutting down -- the "
            "asyncssh bridge loop can no longer run"
        )
    loop = _ensure_loop()
    if _on_loop_thread():
        if _asyncio.iscoroutine(coro):
            coro.close()
        raise RuntimeError(
            "synchronous SFTP call made on the asyncssh bridge-loop thread -- "
            "it would deadlock the loop; await the asyncssh client directly "
            "or run the call in a worker thread (asyncio.to_thread)"
        )
    outcome: "_futures.Future" = _futures.Future()
    started: "list[_asyncio.Task]" = []

    def start() -> None:
        try:
            task = loop.create_task(coro)
        except Exception as error:
            outcome.set_exception(error)
            return
        started.append(task)
        task.add_done_callback(lambda done: _settle(outcome, done))

    loop.call_soon_threadsafe(start)
    try:
        return outcome.result(timeout)
    except BaseException as error:
        if outcome.done():
            raise
        # Queued behind start(), so the task exists by the time it runs.
        loop.call_soon_threadsafe(lambda: started[0].cancel())
        try:
            outcome.exception(_CANCEL_GRACE)
        except (_futures.CancelledError, _futures.TimeoutError):
            pass
        if isinstance(error, _futures.TimeoutError):
            if (
                outcome.done()
                and not outcome.cancelled()
                and outcome.exception() is None
            ):
                # Finished between the timeout and the cancel: keep the result.
                return outcome.result()
            raise TimeoutError(
                f"SFTP request did not complete within {timeout} seconds"
            ) from None
        raise
