"""`AsyncSFTPFile`: a file of the server read and written on the caller's loop."""

from __future__ import annotations

import asyncio as _asyncio
import io as _io
import stat as _stat

import asyncssh as _asyncssh
from pathapi import AsyncFile

from .. import _errors
from ._attrs import _StatAdapter
from ._translate import _library_error

#: Bytes one line search reads ahead, and the text a write buffers before it
#: is sent.
_CHUNK = 64 * 1024


async def _complete(work) -> None:
    """Await `work` to completion even when the caller is cancelled, and then
    re-raise the cancellation."""
    task = _asyncio.ensure_future(work)
    try:
        await _asyncio.shield(task)
    except _asyncio.CancelledError:
        if not task.done():
            await _asyncio.wait([task])
        raise


def _decode_arguments(encoding):
    # io.text_encoding is 3.10+.
    return getattr(_io, "text_encoding", lambda value, stacklevel=1: value)(encoding)


class AsyncSFTPFile(AsyncFile):
    """An open file whose requests are made on the running loop.

    The coroutines are those of `AsyncFile`. A binary file reads and writes
    with one request per call and keeps what a line search read ahead; a text
    file opened for reading loads the file at its first read, and one opened
    for writing sends what it holds when 64 KiB are buffered, on `flush()` and
    on `aclose()`. `file` is the asyncssh file under it."""

    __slots__ = (
        "_afile",
        "_filename",
        "_mode",
        "_kind",
        "_closed",
        "_rbuf",
        "_options",
        "_text",
        "_bytes",
    )

    def __init__(self, afile, filename, mode, kind, encoding, errors, newline):
        self._afile = afile
        self._filename = filename
        self._mode = mode
        self._kind = kind  # "b" binary, "r" text for reading, "w" text for writing
        self._closed = False
        self._rbuf = bytearray()
        self._options = (encoding, errors, newline)
        self._bytes = None if kind == "b" else _io.BytesIO()
        self._text = (
            _io.TextIOWrapper(self._bytes, _decode_arguments(encoding), errors, newline)
            if kind == "w"
            else None
        )

    # --- state ---------------------------------------------------------------

    @property
    def file(self):
        return self._afile

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def name(self):
        return self._filename

    @property
    def mode(self):
        return self._mode

    def readable(self) -> bool:
        return "r" in self._mode or "+" in self._mode

    def writable(self) -> bool:
        return any(kind in self._mode for kind in "wxa+")

    def seekable(self) -> bool:
        return True

    def _check(self, needs: str = "") -> None:
        if self._closed:
            raise ValueError("I/O operation on closed file.")
        if needs == "read" and not self.readable():
            raise _io.UnsupportedOperation("not readable")
        if needs == "write" and not self.writable():
            raise _io.UnsupportedOperation("not writable")

    # --- requests ------------------------------------------------------------

    async def _fetch(self, size: int) -> bytes:
        try:
            data = await self._afile.read(size)
        except _asyncssh.Error as error:
            translated = _library_error(error)
            # OpenSSH opens a directory for reading and fails the read with a
            # bare failure (SFTPv3 has no EISDIR status).
            if (
                isinstance(translated, OSError)
                and translated.errno is None
                and await self._is_directory()
            ):
                raise _errors.is_a_directory(self._filename) from None
            raise translated from None
        # An unsized read reads the handle's fstat size, which some servers
        # report as 0 for a directory they opened.
        if size < 0 and not data and await self._is_directory():
            raise _errors.is_a_directory(self._filename)
        return data

    async def _is_directory(self) -> bool:
        try:
            attrs = await self._afile.stat()
        except (_asyncssh.Error, OSError):
            return False
        return _stat.S_ISDIR(_StatAdapter(attrs).st_mode)

    async def _send(self, data: bytes) -> int:
        if self._rbuf:
            position = await self._afile.tell() - len(self._rbuf)
            self._rbuf.clear()
            await self._afile.seek(position)
        try:
            return await self._afile.write(data)
        except _asyncssh.Error as error:
            raise _library_error(error) from None

    async def _load(self) -> None:
        """The text a file opened for reading holds, behind a text stream."""
        if self._text is None:
            data = await self._bread(-1)
            encoding, errors, newline = self._options
            self._text = _io.TextIOWrapper(
                _io.BytesIO(data), _decode_arguments(encoding), errors, newline
            )

    async def _drain(self) -> None:
        text = self._text
        text.flush()
        data = self._bytes.getvalue()
        if data:
            self._bytes.seek(0)
            self._bytes.truncate()
            await self._send(data)

    async def _bread(self, size: int) -> bytes:
        buffered = self._rbuf
        if size is None or size < 0:
            head = bytes(buffered)
            buffered.clear()
            return head + await self._fetch(-1)
        if len(buffered) >= size:
            head = bytes(buffered[:size])
            del buffered[:size]
            return head
        head = bytes(buffered)
        buffered.clear()
        return head + await self._fetch(size - len(head))

    async def _bline(self, limit: int) -> bytes:
        buffered = self._rbuf
        while True:
            newline = buffered.find(b"\n")
            if newline >= 0:
                end = newline + 1
                break
            if 0 <= limit <= len(buffered):
                end = limit
                break
            chunk = await self._fetch(_CHUNK)
            if not chunk:
                end = len(buffered)
                break
            buffered += chunk
        if limit >= 0:
            end = min(end, limit)
        line = bytes(buffered[:end])
        del buffered[:end]
        return line

    # --- the file ------------------------------------------------------------

    async def read(self, size=-1):
        self._check("read")
        if self._kind == "b":
            return await self._bread(-1 if size is None else size)
        await self._load()
        return self._text.read(size)

    async def readline(self, limit=-1):
        self._check("read")
        limit = -1 if limit is None else limit
        if self._kind == "b":
            return await self._bline(limit)
        await self._load()
        return self._text.readline(limit)

    async def readlines(self, hint=-1):
        lines, total = [], 0
        while hint is None or hint <= 0 or total < hint:
            line = await self.readline()
            if not line:
                break
            lines.append(line)
            total += len(line)
        return lines

    async def write(self, data):
        self._check("write")
        if self._kind == "b":
            view = memoryview(data)
            await self._send(bytes(view))
            return view.nbytes
        if self._kind == "r":
            raise _io.UnsupportedOperation("not writable")
        if not isinstance(data, str):
            raise TypeError(
                "write() argument must be str, not %s" % type(data).__name__
            )
        written = self._text.write(data)
        if self._bytes.tell() >= _CHUNK:
            await self._drain()
        return written

    async def writelines(self, lines):
        for line in lines:
            await self.write(line)

    async def flush(self):
        self._check()
        if self._kind == "w":
            await self._drain()

    async def tell(self):
        self._check()
        if self._kind == "b":
            return await self._afile.tell() - len(self._rbuf)
        if self._kind == "r":
            await self._load()
            return self._text.tell()
        raise _io.UnsupportedOperation("tell")

    async def seek(self, offset, whence=0):
        self._check()
        if self._kind == "b":
            if whence == 1:
                offset, whence = offset + await self.tell(), 0
            self._rbuf.clear()
            try:
                return await self._afile.seek(offset, whence)
            except _asyncssh.Error as error:
                raise _library_error(error) from None
        if self._kind == "r":
            await self._load()
            return self._text.seek(offset, whence)
        raise _io.UnsupportedOperation("seek")

    async def truncate(self, size=None):
        self._check("write")
        if self._kind != "b":
            raise _io.UnsupportedOperation("truncate")
        if size is None:
            size = await self.tell()
        try:
            await self._afile.truncate(size)
        except _asyncssh.Error as error:
            raise _library_error(error) from None
        return size

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await _complete(self._finish())

    async def _finish(self) -> None:
        try:
            if self._kind == "w":
                await self._drain()
        finally:
            try:
                await self._afile.close()
            except _asyncssh.Error as error:
                raise _library_error(error) from None
