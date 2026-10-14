"""A sync client with the method names of paramiko's `SFTPClient`."""

from __future__ import annotations

import io as _io

import asyncssh as _asyncssh
import asyncssh.packet as _packet

from .. import _checkfile
from . import _loop as _bridge
from ._attrs import _StatAdapter
from ._file import _BUFFER_SIZE, _SyncSFTPFile
from ._translate import _reraise_sftp_errors


async def _aopen(aclient: "_asyncssh.SFTPClient", path: str, mode: str):
    return await aclient.open(path, mode, encoding=None)


#: `check-file-handle` as asyncssh keys an extended request.
_CHECK_FILE_REQUEST = _checkfile.EXTENSION.encode("ascii")


async def _acheck_file(
    aclient: "_asyncssh.SFTPClient", handle: bytes, algorithm: str
) -> bytes:
    # No public asyncssh API sends an arbitrary extended request; this is the
    # `_make_request()` of its own statvfs@openssh.com. Its reply type is added to
    # this connection's `_return_types` only: the class table is the server's too.
    handler = aclient._handler
    if _CHECK_FILE_REQUEST not in handler._return_types:
        handler._return_types = {
            **handler._return_types,
            _CHECK_FILE_REQUEST: _asyncssh.FXP_EXTENDED_REPLY,
        }
    packet = await handler._make_request(
        _CHECK_FILE_REQUEST,
        _packet.String(handle),
        _packet.String(algorithm),  # hash-algorithm-list: just the one wanted
        _packet.UInt64(_checkfile.START_OFFSET),
        _packet.UInt64(_checkfile.LENGTH),
        _packet.UInt32(_checkfile.BLOCK_SIZE),
    )
    return packet.get_remaining_payload()


class _SyncSFTPClient:
    """Sync wrapper around an asyncssh `SFTPClient`, exposing the same
    method names paramiko's `SFTPClient` uses (`stat`/`lstat`/
    `listdir_attr`/`open`/`mkdir`/`chmod`/`chown`/`remove`/`rmdir`/
    `rename`/`symlink`/`readlink`/`link`) -- `SFTPPath` calls
    `self._sftpclient.X()`
    directly with no per-backend branching, so matching that shape here is
    what makes everything above "just add one more mirrored method" rather
    than new plumbing in `SFTPPath` itself.

    Weakly referenceable: per-connection records (a refused
    `check-file-handle`, a missing `posix-rename@openssh.com`) are keyed on
    this object, and without `__weakref__` they are silently never kept."""

    __slots__ = ("_aclient", "_timeout", "__weakref__")

    def __init__(
        self,
        aclient: "_asyncssh.SFTPClient",
        timeout: "float | None" = _bridge._UNSET_TIMEOUT,
    ):
        self._aclient = aclient
        self._timeout = timeout

    def _run(self, coro):
        return _bridge._run(coro, self._timeout)

    @_reraise_sftp_errors
    def stat(self, path: str) -> _StatAdapter:
        return _StatAdapter(self._run(self._aclient.stat(path)))

    @_reraise_sftp_errors
    def lstat(self, path: str) -> _StatAdapter:
        return _StatAdapter(self._run(self._aclient.lstat(path)))

    @_reraise_sftp_errors
    def listdir_attr(self, path: str) -> "list[_StatAdapter]":
        # asyncssh has no listdir_attr() of its own -- readdir() returns
        # SFTPName(filename, longname, attrs), the same conceptual shape.
        names = self._run(self._aclient.readdir(path))
        return [
            _StatAdapter(name.attrs, filename=name.filename)
            for name in names
            if name.filename not in (".", "..")
        ]

    @_reraise_sftp_errors
    def open(self, path: str, mode: str = "r", buffering: int = -1) -> _io.IOBase:
        # `aclient.open()` returns a custom awaitable, which
        # asyncio.run_coroutine_threadsafe() rejects; `_aopen` wraps the `await` in a
        # real coroutine.
        afile = self._run(_aopen(self._aclient, path, mode))
        raw = _SyncSFTPFile(afile, self._timeout, mode)
        if buffering == 0:
            return raw
        # Buffered like paramiko's files and a local open(): a raw handle
        # made readline() one round trip per byte.
        size = buffering if buffering > 1 else _BUFFER_SIZE
        if "+" in mode:
            return _io.BufferedRandom(raw, size)
        if raw.readable():
            return _io.BufferedReader(raw, size)
        return _io.BufferedWriter(raw, size)

    @_reraise_sftp_errors
    def mkdir(self, path: str, mode: "int | None" = None) -> None:
        attrs = (
            _asyncssh.SFTPAttrs(permissions=mode)
            if mode is not None
            else _asyncssh.SFTPAttrs()
        )
        self._run(self._aclient.mkdir(path, attrs))

    @_reraise_sftp_errors
    def chmod(self, path: str, mode: int, *, follow_symlinks: bool = True) -> None:
        self._run(self._aclient.chmod(path, mode, follow_symlinks=follow_symlinks))

    @_reraise_sftp_errors
    def chown(self, path: str, uid: int, gid: int) -> None:
        # asyncssh sends uid and gid as SFTPv3's paired UIDGID attribute, always both,
        # which is why `SFTPPath._chown()` reads the current owner for a field left "unchanged".
        self._run(self._aclient.chown(path, uid, gid))

    @_reraise_sftp_errors
    def utime(self, path: str, times: "tuple[float, float]") -> None:
        # (atime, mtime) in seconds: SFTPv3 carries both, whole seconds only.
        self._run(self._aclient.utime(path, times))

    @_reraise_sftp_errors
    def remove(self, path: str) -> None:
        self._run(self._aclient.remove(path))

    @_reraise_sftp_errors
    def rmdir(self, path: str) -> None:
        self._run(self._aclient.rmdir(path))

    @_reraise_sftp_errors
    def rename(self, oldpath: str, newpath: str) -> None:
        self._run(self._aclient.rename(oldpath, newpath))

    @_reraise_sftp_errors
    def posix_rename(self, oldpath: str, newpath: str) -> None:
        # Replaces newpath. Raises NotImplementedError (SFTPOpUnsupported,
        # decided locally) when the server did not advertise
        # posix-rename@openssh.com and the protocol is below v5.
        self._run(self._aclient.posix_rename(oldpath, newpath))

    @_reraise_sftp_errors
    def symlink(self, source: str, dest: str) -> None:
        # asyncssh's docstring confirms it auto-corrects for OpenSSH's
        # well-known swapped wire argument order internally -- the natural
        # "create dest pointing at source" call is already correct as-is.
        self._run(self._aclient.symlink(source, dest))

    @_reraise_sftp_errors
    def readlink(self, path: str) -> str:
        return self._run(self._aclient.readlink(path))

    @_reraise_sftp_errors
    def link(self, source: str, dest: str) -> None:
        # No typed exception is guaranteed for "the server lacks this extension";
        # `_reraise_sftp_errors` maps whatever comes back to some OSError subclass, and
        # `SFTPPath.hardlink_to()` decides the NotImplementedError fallback.
        self._run(self._aclient.link(source, dest))

    @_reraise_sftp_errors
    def check_file(self, file: "_SyncSFTPFile", algorithm: str) -> bytes:
        """The `check-file-handle` reply payload for `file`, an unbuffered
        handle from `open()`. The server hashes the whole file before it
        answers, so the duration grows with the data: no wall-clock bound.
        A reply of a type the request does not have is
        `NotImplementedError`, as an unsupported operation is."""
        try:
            return _bridge._run(
                _acheck_file(self._aclient, file._afile.handle, algorithm), None
            )
        except _asyncssh.SFTPBadMessage:
            raise NotImplementedError(
                f"{_checkfile.EXTENSION}: unexpected reply type"
            ) from None
