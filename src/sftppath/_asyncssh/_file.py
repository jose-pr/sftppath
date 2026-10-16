"""A file of the server as a sync binary file object."""

from __future__ import annotations

import io as _io
import stat as _stat
import sys as _sys

import asyncssh as _asyncssh

from .. import _errors
from . import _loop as _bridge
from ._attrs import _StatAdapter
from ._translate import _library_error, _reraise_sftp_errors


class _SyncSFTPFile(_io.RawIOBase):
    """Wraps an asyncssh `SFTPClientFile` (every method a coroutine,
    including read/write/seek/close) as a sync binary file-like object --
    the `BinaryOpen` protocol contract (`protocols/io.py`) expects `_open()`
    to hand back a genuine `io.IOBase`: `open()` in text mode wraps it in
    `io.TextIOWrapper`, which needs `flush()`/`readable()`/etc., not just
    `read()`/`write()` -- a bare duck-typed object without a real `io.*`
    base class raises `AttributeError: ... no attribute 'flush'` the first
    time a caller does `read_text()`/`write_text()`. Subclassing
    `io.RawIOBase` gets `flush()`, `fileno()`, `isatty()`, the context
    manager protocol, and `closed`-state bookkeeping for free -- only the
    genuinely backend-specific methods below need overriding. Construction
    always passes `encoding=None` (see `_SyncSFTPClient.open`): asyncssh
    defaults to TEXT mode (`encoding='utf-8'`), unlike paramiko whose SFTP
    files are always binary -- a text-mode stream here would return `str`
    from every read and blow up every downstream `read_bytes()` caller."""

    def __init__(
        self,
        afile: "_asyncssh.SFTPClientFile",
        timeout: "float | None" = _bridge._UNSET_TIMEOUT,
        mode: str = "r+",
        filename: "str | None" = None,
    ):
        super().__init__()
        self._afile = afile
        self._timeout = timeout
        self._filename = filename
        self._readable = "r" in mode or "+" in mode
        self._writable = any(kind in mode for kind in "wxa+")

    def _read(self, size: int) -> bytes:
        # read() carries whole payloads (read_bytes() is one read(-1)), so its
        # duration grows with the data: no wall-clock bound.
        try:
            data = _bridge._run(self._afile.read(size), None)
        except _asyncssh.Error as error:
            translated = _library_error(error)
            # OpenSSH opens a directory for reading and fails the read with a
            # bare failure (SFTPv3 has no EISDIR status).
            if (
                isinstance(translated, OSError)
                and translated.errno is None
                and self._is_directory()
            ):
                raise _errors.is_a_directory(self._filename) from None
            raise translated from None
        # An unsized read reads the handle's fstat size, which some servers
        # (Windows OpenSSH) report as 0 for a directory they opened.
        if size < 0 and not data and self._is_directory():
            raise _errors.is_a_directory(self._filename)
        return data

    def _is_directory(self) -> bool:
        """Whether the server says this handle is a directory: one fstat,
        asked only after a read came back empty or failed."""
        try:
            attrs = _bridge._run(self._afile.stat(), self._timeout)
        except (_asyncssh.Error, OSError):
            return False
        return _stat.S_ISDIR(_StatAdapter(attrs).st_mode)

    def read(self, size: int = -1) -> bytes:
        return self._read(-1 if size is None else size)

    def readall(self) -> bytes:
        # One request stream to EOF (asyncssh parallelizes it), instead of
        # RawIOBase's loop of DEFAULT_BUFFER_SIZE reads.
        return self._read(-1)

    def readinto(self, buffer) -> int:
        view = memoryview(buffer).cast("B")
        data = self._read(len(view))
        view[: len(data)] = data
        return len(data)

    @_reraise_sftp_errors
    def write(self, data: bytes) -> int:
        return _bridge._run(self._afile.write(bytes(data)), None)

    @_reraise_sftp_errors
    def truncate(self, size: "int | None" = None) -> int:
        if size is None:
            size = self.tell()
        _bridge._run(self._afile.truncate(size), self._timeout)
        return size

    @_reraise_sftp_errors
    def seek(self, offset: int, whence: int = 0) -> int:
        return _bridge._run(self._afile.seek(offset, whence), self._timeout)

    @_reraise_sftp_errors
    def tell(self) -> int:
        return _bridge._run(self._afile.tell(), self._timeout)

    def close(self) -> None:
        if self.closed:
            return
        try:
            if _sys.is_finalizing():
                # The loop thread cannot run any more; the server releases
                # the handle when the connection drops.
                pass
            elif _bridge._on_loop_thread():
                # A finalizer can run here; closing must not block the loop.
                _bridge._loop.create_task(self._afile.close())
            else:
                try:
                    _bridge._run(self._afile.close(), self._timeout)
                except _asyncssh.Error as error:
                    raise _library_error(error) from None
        finally:
            super().close()

    def readable(self) -> bool:
        return self._readable

    def writable(self) -> bool:
        return self._writable

    def seekable(self) -> bool:
        return True


#: Buffer size for the `io.Buffered*` wrapper `_SyncSFTPClient.open()`
#: returns: each refill is one SFTP read request.
_BUFFER_SIZE = 64 * 1024
