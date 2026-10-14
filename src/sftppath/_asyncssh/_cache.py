"""The connections of the asyncssh backends: one per backend, not per thread."""

from __future__ import annotations

import collections as _collections
import concurrent.futures as _futures
import threading as _thread
import typing as _ty

import asyncssh as _asyncssh

from . import _loop as _bridge

if _ty.TYPE_CHECKING:
    from ._client import _SyncSFTPClient

# Keyed by (backend,), no thread (one loop). Not `providers.LRU`: an evicted entry is
# closed on the bridge loop, and concurrent misses on one key wait for one connection
# instead of each opening their own.


class _ConnectionEntry(_ty.NamedTuple):
    conn: "_asyncssh.SSHClientConnection"
    client: "_SyncSFTPClient"


async def _aclose_entry(entry: "_ConnectionEntry") -> None:
    try:
        entry.client._aclient.exit()
        await entry.client._aclient.wait_closed()
    except Exception:
        pass
    entry.conn.close()
    try:
        await entry.conn.wait_closed()
    except Exception:
        pass


def _entry_is_alive(entry: "_ConnectionEntry") -> bool:
    if entry.conn.is_closed():
        return False
    # The SFTP channel can close while the SSH connection stays up (server
    # ChannelTimeout, sftp-server crash); asyncssh clears the handler's
    # writer when it does.
    handler = getattr(entry.client._aclient, "_handler", None)
    return handler is None or getattr(handler, "_writer", True) is not None


def _close_entries(entries: "_ty.Iterable[_ConnectionEntry]") -> None:
    for entry in entries:
        try:
            _bridge._run(_aclose_entry(entry))
        except Exception:
            pass


class _ConnectionCache:
    __slots__ = ("_entries", "_pending", "_lock", "maxsize")

    def __init__(self, maxsize: int = 128):
        self._entries: "_collections.OrderedDict[tuple, _ConnectionEntry]" = (
            _collections.OrderedDict()
        )
        self._pending: "dict[tuple, _futures.Future]" = {}
        self._lock = _thread.Lock()
        self.maxsize = maxsize

    def get_or_create(
        self, key, factory: "_ty.Callable[[], _ConnectionEntry]"
    ) -> _ConnectionEntry:
        while True:
            stale = None
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None:
                    if _entry_is_alive(entry):
                        self._entries.move_to_end(key)
                        return entry
                    stale = self._entries.pop(key)
                pending = self._pending.get(key)
                owner = pending is None
                if owner:
                    pending = self._pending[key] = _futures.Future()
            if stale is not None:
                _close_entries([stale])
            if owner:
                break
            # Another thread is connecting this key: share its connection
            # (or its failure) instead of opening a second one.
            pending.result()

        try:
            entry = factory()
        except BaseException as error:
            with self._lock:
                self._pending.pop(key, None)
            pending.set_exception(error)
            raise
        evicted = []
        with self._lock:
            self._entries[key] = entry
            self._pending.pop(key, None)
            while len(self._entries) > self.maxsize:
                evicted.append(self._entries.popitem(last=False)[1])
        pending.set_result(None)
        _close_entries(evicted)
        return entry

    def invalidate(self, key) -> None:
        with self._lock:
            entry = self._entries.pop(key, None)
        if entry is not None:
            _close_entries([entry])

    def close_backend(self, backend) -> None:
        with self._lock:
            keys = [key for key in self._entries if key[0] is backend]
            entries = [self._entries.pop(key) for key in keys]
        _close_entries(entries)

    def reset(self) -> None:
        # Used only by _ensure_loop() on a PID change (forked child): the old loop is
        # dead, so entries are dropped, not closed (no loop is left to run the close).
        with self._lock:
            self._entries.clear()
            self._pending.clear()


_CACHE = _ConnectionCache()
