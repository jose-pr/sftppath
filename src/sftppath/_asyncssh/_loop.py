"""The shared background event loop the sync API runs asyncssh on.

asyncssh is asyncio-only end to end (connect() and every SFTPClient and
SFTPClientFile method are coroutines) while `SFTPPath` is sync. One shared loop
in a daemon thread, started lazily on first use, lets calls from any thread
share one persistent connection. `asyncio.run()` per call would create and
destroy a loop every time and could not hold a connection open; a loop per
calling thread would need connections shared across loops, which asyncssh does
not support, and would only reimplement paramiko's thread-keyed cache.

A backend built over a caller's connection runs its requests on the loop that
connection is bound to instead (`_CallerLoop`), with the same timeout, cancel
grace and finalization guard.
"""

from __future__ import annotations

import asyncio as _asyncio
import concurrent.futures as _futures
import os as _os
import sys as _sys
import threading as _thread
import time as _time
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


class _CallerLoop:
    """An event loop the caller runs and owns, and the process it was handed
    over in: after `fork()` the child holds a copy whose thread does not
    exist, so a request sent to it would never be answered."""

    __slots__ = ("loop", "pid")

    def __init__(self, loop: "_asyncio.AbstractEventLoop"):
        self.loop = loop
        self.pid = _os.getpid()


def _running_loop() -> "_asyncio.AbstractEventLoop | None":
    try:
        return _asyncio.get_running_loop()
    except RuntimeError:
        return None


def _on_loop(caller: "_CallerLoop | None") -> bool:
    """Whether the calling thread is the one running the loop `caller` names
    (the bridge loop for None): a blocking wait there would deadlock it."""
    if caller is None:
        return _on_loop_thread()
    return _running_loop() is caller.loop


def _loop_of(caller: "_CallerLoop | None") -> "_asyncio.AbstractEventLoop":
    """The loop a request goes to: the caller's, checked, or the bridge loop."""
    if caller is None:
        return _ensure_loop()
    if caller.pid != _os.getpid():
        raise RuntimeError(
            "an SFTP backend over a caller's connection cannot be used after "
            "fork(): build one over a connection opened in this process"
        )
    if caller.loop.is_closed() or not caller.loop.is_running():
        raise _LoopStopped()
    return caller.loop


class _LoopStopped(RuntimeError):
    def __init__(self):
        super().__init__(
            "the event loop of the SSH connection this SFTP backend runs over "
            "is not running"
        )


_UNSET_TIMEOUT: _ty.Any = object()

#: Longest, in seconds, `_run()` waits for a coroutine it has cancelled to
#: finish unwinding (its cleanup requests included) before it raises anyway.
_CANCEL_GRACE = 5.0

#: How often, in seconds, a wait on a caller's loop checks that the loop still
#: runs: a request queued on a loop that has stopped is never answered.
_CALLER_LOOP_POLL = 0.5


def _wait(
    outcome: "_futures.Future",
    timeout: "float | None",
    caller: "_CallerLoop | None",
):
    if caller is None:
        return outcome.result(timeout)
    deadline = None if timeout is None else _time.monotonic() + timeout
    while True:
        step = _CALLER_LOOP_POLL
        if deadline is not None:
            step = max(0.0, min(step, deadline - _time.monotonic()))
        try:
            return outcome.result(step)
        except _futures.TimeoutError:
            if deadline is not None and _time.monotonic() >= deadline:
                raise
            if caller.loop.is_closed() or not caller.loop.is_running():
                raise _LoopStopped() from None


def _settle(outcome: "_futures.Future", task: "_asyncio.Task") -> None:
    """Hand a finished bridge-loop task's outcome to the thread waiting on
    `outcome`."""
    if task.cancelled():
        outcome.cancel()
    elif task.exception() is not None:
        outcome.set_exception(task.exception())
    else:
        outcome.set_result(task.result())


def _run(
    coro,
    timeout: "float | None" = _UNSET_TIMEOUT,
    caller: "_CallerLoop | None" = None,
):
    """Run `coro` on the bridge loop, or on the loop `caller` names, and block
    the calling thread for its result.

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

    On a caller's loop, also `RuntimeError` when the loop is closed or not
    running, or stops while the call waits (nothing would answer it), and in
    a `fork()`ed child of the process that built `caller`.
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
    try:
        loop = _loop_of(caller)
    except RuntimeError:
        if _asyncio.iscoroutine(coro):
            coro.close()
        raise
    if _on_loop(caller):
        if _asyncio.iscoroutine(coro):
            coro.close()
        raise RuntimeError(
            "synchronous SFTP call made on the thread running the asyncssh "
            "event loop -- it would deadlock the loop; await the asyncssh "
            "client directly or run the call in a worker thread "
            "(asyncio.to_thread)"
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

    try:
        loop.call_soon_threadsafe(start)
    except RuntimeError:
        # Closed between the check and the call.
        if _asyncio.iscoroutine(coro):
            coro.close()
        if caller is None:
            raise
        raise _LoopStopped() from None
    try:
        return _wait(outcome, timeout, caller)
    except BaseException as error:
        if outcome.done() or isinstance(error, _LoopStopped):
            raise
        try:
            # Queued behind start(), so the task exists by the time it runs.
            loop.call_soon_threadsafe(lambda: started[0].cancel())
        except RuntimeError:
            # A caller's loop that has closed runs nothing, so nothing is waited for.
            pass
        else:
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
