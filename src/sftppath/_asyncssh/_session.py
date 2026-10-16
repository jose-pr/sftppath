"""The connection `AsyncSFTPPath` uses: one per backend and event loop.

An asyncssh connection belongs to the loop that opened it, so the connection a
path uses is the one its backend has on the running loop, opened by the first
request. It is closed by `aclose()`, or, when a program never calls it, by the
task that holds it when the loop cancels its tasks on the way out.
"""

from __future__ import annotations

import asyncio as _asyncio
import typing as _ty
import weakref as _weakref

import asyncssh as _asyncssh

from ._cache import _aclose_entry, _ConnectionEntry, _entry_is_alive
from ._connect import _aconnect
from ._translate import _transport_error

#: Tasks that hold an open connection, kept so that a pending one is not
#: collected before the loop cancels it.
_HOLDERS: "set[_asyncio.Task]" = set()


class _Link(_ty.NamedTuple):
    """What a request needs: the asyncssh client, and the object the rename
    bookkeeping of `_rename` is keyed on."""

    aclient: "_asyncssh.SFTPClient"
    token: object


class _Slot:
    """The connection of one backend on one loop."""

    __slots__ = ("entry", "holder", "lock", "__weakref__")

    def __init__(self) -> None:
        self.entry: "_ConnectionEntry | None" = None
        self.holder: "_asyncio.Task | None" = None
        # Made on first use, inside the loop: a lock made earlier binds to
        # whichever loop is current on Python 3.9.
        self.lock: "_asyncio.Lock | None" = None


async def _hold(entry: "_ConnectionEntry") -> None:
    try:
        await _asyncio.get_running_loop().create_future()
    finally:
        await _aclose_entry(entry)


class _Session:
    """The connections a backend has, one per event loop that used it."""

    __slots__ = ("backend", "_slots", "__weakref__")

    def __init__(self, backend) -> None:
        self.backend = backend
        self._slots: "_weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()

    async def link(self) -> _Link:
        loop = _asyncio.get_running_loop()
        slot = self._slots.get(loop)
        if slot is None:
            slot = self._slots[loop] = _Slot()
        entry = slot.entry
        if entry is not None and _entry_is_alive(entry):
            return _Link(entry.client._aclient, entry.client)
        if slot.lock is None:
            slot.lock = _asyncio.Lock()
        async with slot.lock:
            entry = slot.entry
            if entry is not None and _entry_is_alive(entry):
                return _Link(entry.client._aclient, entry.client)
            if entry is not None:
                await self._release(slot)
            backend = self.backend
            try:
                entry = await _aconnect(
                    backend.host,
                    backend.port,
                    backend.user,
                    backend.password,
                    connect_opts=backend.connect_opts,
                    sftp_version=backend.sftp_version,
                    timeout=backend.timeout,
                )
            except _asyncssh.Error as error:
                raise _transport_error(error, backend.endpoint) from None
            slot.entry = entry
            holder = slot.holder = loop.create_task(_hold(entry))
            _HOLDERS.add(holder)
            holder.add_done_callback(_HOLDERS.discard)
            # Let the holder start: a task cancelled before its first step
            # never runs its cleanup.
            await _asyncio.sleep(0)
        return _Link(entry.client._aclient, entry.client)

    @staticmethod
    async def _release(slot: _Slot) -> None:
        holder = slot.holder
        slot.entry = slot.holder = None
        if holder is not None:
            holder.cancel()
            await _asyncio.wait([holder])

    async def aclose(self) -> None:
        """Close the connection on the running loop; harmless when none is
        open, and the next request opens another."""
        slot = self._slots.get(_asyncio.get_running_loop())
        if slot is not None:
            await self._release(slot)


_SESSIONS: "_weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()


def _session_for(backend) -> _Session:
    session = _SESSIONS.get(backend)
    if session is None:
        session = _SESSIONS[backend] = _Session(backend)
    return session
