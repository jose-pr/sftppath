"""`AsyncSFTPPath`: `pathapi.AsyncPath` over an SFTP server, on asyncssh."""

from __future__ import annotations

import errno as _errno
import functools as _functools
import importlib.metadata as _metadata
import io as _io
import os as _os
import stat as _stat

import asyncssh as _asyncssh

try:
    from pathapi import AsyncPath as _AsyncPath

    if not hasattr(_AsyncPath, "_adopt"):
        raise ImportError("this AsyncPath has no _adopt hook")
except ImportError as _missing:
    try:
        _installed = _metadata.version("pathapi")
    except _metadata.PackageNotFoundError:
        _installed = "not installed"
    raise ImportError(
        "AsyncSFTPPath needs pathapi>=0.1.2, which provides AsyncPath._adopt "
        f"(found {_installed})",
        name="pathapi",
    ) from _missing

from pathapi import FileStat
from pathapi.providers import (
    check_follow,
    contains,
    copy_error_predicate,
    is_safe_child_name,
)

from ._asyncssh import AsyncSSHBackend
from ._asyncssh._aiofile import AsyncSFTPFile, _decode_arguments
from ._asyncssh._attrs import _StatAdapter
from ._asyncssh._session import _session_for
from ._asyncssh._translate import _library_error
from ._asyncssh._walk import _concurrent_copy, _concurrent_rm
from ._path import SFTPPath
from ._rename import _mark_posix_rename_unsupported, _posix_rename_known_unsupported
from ._sshconfig import DEFAULT_SSH_CONFIG, UNSET

__all__ = ["AsyncSFTPPath"]


def _sync_of(value):
    return value.path if isinstance(value, _AsyncPath) else value


async def _call(awaitable, filename=None, filename2=None):
    """Await one asyncssh request, raising what the sync client raises."""
    try:
        return await awaitable
    except _asyncssh.Error as error:
        raise _library_error(error, filename, filename2) from None


def _parse_mode(mode, encoding, errors, newline):
    """`(kind, plus, binary)` of an `open()` mode, validated as `open()` does."""
    if not isinstance(mode, str):
        raise TypeError(
            f"open() argument 'mode' must be str, not {type(mode).__name__}"
        )
    letters = set(mode)
    if len(letters) != len(mode) or letters - set("rwxabt+"):
        raise ValueError(f"invalid mode: {mode!r}")
    kinds = letters & set("rwxa")
    if len(kinds) != 1:
        raise ValueError("must have exactly one of create/read/write/append mode")
    if "t" in letters and "b" in letters:
        raise ValueError("can't have text and binary mode at once")
    binary = "b" in letters
    if binary:
        for name, value in (
            ("encoding", encoding),
            ("errors", errors),
            ("newline", newline),
        ):
            if value is not None:
                raise ValueError(f"binary mode doesn't take an {name} argument")
    return kinds.pop(), "+" in letters, binary


class _Opening:
    """What `AsyncSFTPPath.open()` returns: awaited, it opens the file; as an
    async context manager it opens and closes it."""

    __slots__ = ("_make", "_file")

    def __init__(self, make) -> None:
        self._make = make
        self._file = None

    def __await__(self):
        return self._make().__await__()

    async def __aenter__(self) -> AsyncSFTPFile:
        self._file = await self._make()
        return self._file

    async def __aexit__(self, *exc) -> None:
        await self._file.aclose()


class AsyncSFTPPath(_AsyncPath):
    """An `AsyncPath` over an SFTP server whose requests are made on the
    running event loop with asyncssh, not on a worker thread.

    Built like `SFTPPath` (`AsyncSFTPPath("/srv/x", host="h", user="u")`) or
    from one (`AsyncSFTPPath(sftp_path)`, whose backend must be an
    `AsyncSSHBackend`); `path` is the `SFTPPath` it keeps for the pure members
    and for the methods it does not implement natively, which run as
    `AsyncPath` runs them. Nothing connects when a path is built.

    `stat()`, `lstat()`, `exists()`, `is_dir()`, `is_file()`, `iterdir()`,
    `walk()`, `open()`, `read_bytes()`, `read_text()`, `write_bytes()`,
    `write_text()`, `mkdir()`, `unlink()`, `rmdir()`, `rename()` and, for a
    directory tree on one server, `rm(recursive=True)` and
    `copy(recursive=True)` are native. The other I/O methods, `glob()`,
    `rglob()` and a text file opened for reading and writing run on worker
    threads.

    The connection is opened by the first request, belongs to the backend and
    the running loop, and is shared by every path with that backend. `aclose()`
    closes it (the next request opens another), as does `async with`; one
    never closed is closed when its loop cancels its tasks on the way out. A
    request has no time bound of its own: use `asyncio.timeout()` or
    `asyncio.wait_for()`. Callbacks (`ignore_error`, `on_error`) given to
    `rm()` and `copy()` run on worker threads; those of `walk()` run on the
    loop."""

    __slots__ = ()

    def __init__(
        self,
        *segments,
        backend=None,
        host=None,
        port=None,
        user=None,
        password=None,
        ssh_config=DEFAULT_SSH_CONFIG,
        timeout=UNSET,
        connect_opts=None,
        **options,
    ) -> None:
        segments = tuple(_sync_of(segment) for segment in segments)
        endpoint = (
            host is not None
            or port is not None
            or user is not None
            or password is not None
            or ssh_config is not DEFAULT_SSH_CONFIG
            or timeout is not UNSET
            or connect_opts is not None
            or bool(options)
        )
        if backend is not None and endpoint:
            raise TypeError(
                "pass either backend= or the endpoint keywords "
                "(host=, port=, user=, ...), not both"
            )
        if backend is None and endpoint:
            if host is None:
                raise TypeError("host= is required with the other endpoint keywords")
            extra = dict(options)
            if timeout is not UNSET:
                extra["timeout"] = timeout
            if connect_opts is not None:
                extra["connect_opts"] = connect_opts
            backend = AsyncSSHBackend(
                host, port, user=user, password=password, ssh_config=ssh_config, **extra
            )
        path = (
            SFTPPath(*segments)
            if backend is None
            else SFTPPath(*segments, backend=backend)
        )
        if not isinstance(path.backend, AsyncSSHBackend):
            raise TypeError(
                "AsyncSFTPPath runs on an AsyncSSHBackend, not "
                f"{type(path.backend).__name__}; wrap the path in pathapi.AsyncPath "
                "to use another backend from an event loop"
            )
        super().__init__(path)

    @classmethod
    def _adopt(cls, path):
        # A derived path on another kind of backend cannot be an
        # `AsyncSFTPPath`, which needs an `AsyncSSHBackend`.
        if isinstance(path, SFTPPath) and isinstance(path.backend, AsyncSSHBackend):
            return cls(path)
        return _AsyncPath(path)

    def _child(self, name: str):
        sync = self._path
        return type(self)(sync.with_segments(*sync.segments, name))

    async def _link(self):
        return await _session_for(self._path._backend).link()

    async def aclose(self) -> None:
        """Close the connection on the running loop that this path's backend
        has opened for `AsyncSFTPPath`. Harmless when none is open."""
        await _session_for(self._path._backend).aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # --- status --------------------------------------------------------------

    async def stat(self, *, follow_symlinks=True):
        link = await self._link()
        wire = self._path._wirepath
        request = link.aclient.stat if follow_symlinks else link.aclient.lstat
        return _StatAdapter(await _call(request(wire), wire))

    async def lstat(self):
        return await self.stat(follow_symlinks=False)

    async def _mode(self, follow_symlinks=True):
        try:
            return (await self.stat(follow_symlinks=follow_symlinks)).st_mode
        except (OSError, ValueError):
            return None

    async def exists(self, *, follow_symlinks=True):
        return await self._mode(follow_symlinks) is not None

    async def is_dir(self, *, follow_symlinks=True):
        return _stat.S_ISDIR(await self._mode(follow_symlinks) or 0)

    async def is_file(self, *, follow_symlinks=True):
        return _stat.S_ISREG(await self._mode(follow_symlinks) or 0)

    async def _entry_stat(self):
        try:
            return FileStat.from_stat(await self.stat())
        except OSError:
            return None

    # --- listing -------------------------------------------------------------

    async def _directory_error(self, error: OSError, check_empty=False):
        """`SFTPPath._directory_error()`, asking the server on the loop."""
        if self._path._unreachable(error):
            return None
        found = await self._entry_stat()
        if found is None:
            return None
        if not found.is_dir():
            return NotADirectoryError(
                _errno.ENOTDIR, _os.strerror(_errno.ENOTDIR), str(self)
            )
        if check_empty and error.errno is None:
            try:
                link = await self._link()
                wire = self._path._wirepath
                has_children = any(
                    entry.filename not in (".", "..")
                    for entry in await _call(link.aclient.readdir(wire), wire)
                )
            except OSError:
                return None
            if has_children:
                return OSError(
                    _errno.ENOTEMPTY, _os.strerror(_errno.ENOTEMPTY), str(self)
                )
        return None

    async def _file_error(self, error: OSError):
        """`SFTPPath._file_error()`, asking the server on the loop."""
        if error.errno is not None or self._path._unreachable(error):
            return None
        found = await self._entry_stat()
        if found is None or not found.is_dir():
            return None
        return IsADirectoryError(_errno.EISDIR, _os.strerror(_errno.EISDIR), str(self))

    async def _entries(self):
        """`[(name, listed FileStat or None)]` of this directory, unfiltered."""
        link = await self._link()
        wire = self._path._wirepath
        try:
            names = await _call(link.aclient.readdir(wire), wire)
        except OSError as error:
            translated = await self._directory_error(error)
            if translated is None:
                raise
            raise translated from error
        entries = []
        for entry in names:
            if entry.filename in (".", ".."):
                continue
            listed = FileStat.from_stat(_StatAdapter(entry.attrs))
            entries.append(
                (entry.filename, listed if _stat.S_IFMT(listed.st_mode) else None)
            )
        return entries

    async def iterdir(self):
        """Async iterator over the entries of this directory, as
        `AsyncSFTPPath`; the listing is one request, made at the first step."""
        for name, _listed in await self._entries():
            if is_safe_child_name(name):
                yield self._child(name)

    async def walk(self, top_down=True, on_error=None, follow_symlinks=False):
        """Async iterator over `Path.walk()`: `(AsyncSFTPPath, dirnames,
        filenames)`, one request per directory, with `dirnames` open to
        pruning when `top_down`. `on_error` is called on the loop."""
        pending = [self]
        while pending:
            item = pending.pop()
            if isinstance(item, tuple):
                yield item
                continue
            try:
                entries = await item._entries()
            except OSError as error:
                if on_error is not None:
                    on_error(error)
                continue
            dirnames, filenames = [], []
            for name, listed in entries:
                if not is_safe_child_name(name):
                    if on_error is not None:
                        error = ValueError(
                            f"refusing unsafe child name {name!r} under {item}"
                        )
                        error.filename = str(item)
                        on_error(error)
                    continue
                try:
                    if listed is None or follow_symlinks:
                        listed = FileStat.from_stat(
                            await item._child(name).stat(
                                follow_symlinks=follow_symlinks
                            )
                        )
                    is_dir = listed.is_dir()
                except OSError:
                    is_dir = False
                (dirnames if is_dir else filenames).append(name)
            if top_down:
                yield item, dirnames, filenames
            else:
                pending.append((item, dirnames, filenames))
            pending.extend(item._child(name) for name in reversed(dirnames))

    # --- files ---------------------------------------------------------------

    async def _open_wire(self, mode: str):
        """The asyncssh file for `mode` (`r`, `w`, `a`, `x`, with `+`), with
        the errors of `SFTPPath._open()`."""
        link = await self._link()
        wire = self._path._wirepath
        wire_mode = self._path._backend._wire_open_mode(mode)
        try:
            return await _call(link.aclient.open(wire, wire_mode, encoding=None), wire)
        except OSError as error:
            # SFTPv3 has no "already exists" status.
            if (
                "x" in mode
                and not self._path._unreachable(error)
                and await self.exists()
            ):
                raise FileExistsError(
                    _errno.EEXIST, _os.strerror(_errno.EEXIST), str(self)
                ) from error
            translated = await self._file_error(error)
            if translated is None:
                raise
            raise translated from error

    async def _open_file(
        self, mode="r", buffering=-1, encoding=None, errors=None, newline=None
    ):
        kind, plus, binary = _parse_mode(mode, encoding, errors, newline)
        if not binary and plus:
            # A text stream that reads and writes has no native form here.
            return await _AsyncPath.open(
                self, mode, buffering, encoding, errors, newline
            )
        if not binary:
            # An unknown encoding or newline fails before the file is touched.
            _io.TextIOWrapper(
                _io.BytesIO(), _decode_arguments(encoding), errors, newline
            ).close()
        wire_mode = kind + ("+" if plus else "")
        afile = await self._open_wire(wire_mode)
        file_kind = "b" if binary else ("r" if kind == "r" else "w")
        return AsyncSFTPFile(
            afile,
            str(self),
            mode,
            file_kind,
            encoding,
            errors,
            newline,
        )

    def open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        """Open the file. `await path.open(...)` gives an `AsyncSFTPFile` to
        close with `aclose()`; `async with path.open(...) as f` closes it. A
        text stream for reading and writing (`"r+"`, `"w+"`) is the
        worker-thread file of `AsyncPath`."""
        return _Opening(
            _functools.partial(
                self._open_file, mode, buffering, encoding, errors, newline
            )
        )

    async def read_bytes(self):
        file = await self._open_file("rb")
        try:
            return await file.read()
        finally:
            await file.aclose()

    async def read_text(self, encoding=None, errors=None, newline=None):
        data = await self.read_bytes()
        with _io.TextIOWrapper(
            _io.BytesIO(data), _decode_arguments(encoding), errors, newline
        ) as text:
            return text.read()

    async def write_bytes(self, data):
        view = memoryview(data)
        file = await self._open_file("wb")
        try:
            return await file.write(view)
        finally:
            await file.aclose()

    async def write_text(self, data, encoding=None, errors=None, newline=None):
        if not isinstance(data, str):
            raise TypeError("data must be str, not %s" % data.__class__.__name__)
        file = await self._open_file("w", -1, encoding, errors, newline)
        try:
            return await file.write(data)
        finally:
            await file.aclose()

    # --- changing the tree ---------------------------------------------------

    async def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        try:
            await self._mkdir(mode)
        except FileNotFoundError:
            if not parents or self.parent == self:
                raise
            await self.parent.mkdir(parents=True, exist_ok=True)
            await self.mkdir(mode, parents=False, exist_ok=exist_ok)
        except FileExistsError:
            if not exist_ok or not await self.is_dir():
                raise

    async def _mkdir(self, mode):
        link = await self._link()
        wire = self._path._wirepath
        attrs = (
            _asyncssh.SFTPAttrs(permissions=mode)
            if mode is not None
            else _asyncssh.SFTPAttrs()
        )
        try:
            await _call(link.aclient.mkdir(wire, attrs), wire)
        except OSError as error:
            # SFTPv3 has no "already exists" status either.
            if not self._path._unreachable(error) and await self.exists():
                raise FileExistsError(
                    _errno.EEXIST, _os.strerror(_errno.EEXIST), str(self)
                ) from error
            raise

    async def unlink(self, missing_ok=False):
        link = await self._link()
        wire = self._path._wirepath
        try:
            await _call(link.aclient.remove(wire), wire)
        except FileNotFoundError:
            if not missing_ok:
                raise
        except OSError as error:
            translated = await self._file_error(error)
            if translated is None:
                raise
            raise translated from error

    async def rmdir(self):
        link = await self._link()
        wire = self._path._wirepath
        try:
            await _call(link.aclient.rmdir(wire), wire)
        except OSError as error:
            translated = await self._directory_error(error, check_empty=True)
            if translated is None:
                raise
            raise translated from error

    async def rename(self, target):
        sync = self._path
        if isinstance(target, str):
            target = sync.parent / target
        else:
            target = _sync_of(target)
            if not isinstance(target, SFTPPath) or not sync._same_filesystem(target):
                raise NotImplementedError("rename() onto another server")
        destination = sync._rebuild(target.as_posix())
        await self._rename_on_wire(destination)
        return type(self)(destination)

    async def _rename_on_wire(self, target: SFTPPath) -> None:
        link = await self._link()
        client, token = link.aclient, link.token
        source, destination = self._path._wirepath, target._wirepath
        if not _posix_rename_known_unsupported(token):
            try:
                return await _call(
                    client.posix_rename(source, destination), source, destination
                )
            except NotImplementedError:
                _mark_posix_rename_unsupported(token)
            except OSError as error:
                if error.errno is not None:
                    raise
                try:
                    result = await _call(
                        client.rename(source, destination), source, destination
                    )
                except OSError as rename_error:
                    await self._raise_if_exists(target, rename_error)
                    raise
                _mark_posix_rename_unsupported(token)
                return result
        try:
            return await _call(client.rename(source, destination), source, destination)
        except OSError as error:
            await self._raise_if_exists(target, error)
            raise

    async def _raise_if_exists(self, target: SFTPPath, error: OSError) -> None:
        if isinstance(error, (FileNotFoundError, PermissionError)) or (
            self._path._unreachable(error)
        ):
            return
        try:
            await type(self)(target).lstat()
        except Exception:
            return
        raise FileExistsError(_errno.EEXIST, "File exists", target._wirepath) from error

    async def rm(
        self,
        recursive=False,
        missing_ok=False,
        ignore_error=False,
        *,
        follow_symlinks=False,
        follow_binds=False,
        max_workers=None,
    ):
        """Remove as `Path.rm()` does. A recursive removal that does not
        follow links is concurrent requests on the loop, bounded by
        `backend.max_concurrency` (`max_workers` is not used); anything else
        is `AsyncPath.rm()`."""
        check_follow("follow_symlinks", follow_symlinks)
        check_follow("follow_binds", follow_binds)
        if not recursive or follow_symlinks is not False:
            return await _AsyncPath.rm(
                self,
                recursive,
                missing_ok,
                ignore_error,
                follow_symlinks=follow_symlinks,
                follow_binds=follow_binds,
                max_workers=max_workers,
            )
        on_error = None
        if ignore_error:
            on_error = (
                ignore_error
                if callable(ignore_error)
                else lambda _error, _path: bool(ignore_error)
            )
        link = await self._link()
        await _concurrent_rm(
            self._path,
            max_concurrency=self._path._backend.max_concurrency,
            missing_ok=missing_ok,
            on_error=on_error,
            aclient=link.aclient,
        )

    async def copy(
        self,
        target,
        *,
        overwrite=False,
        follow_symlinks=True,
        preserve_metadata=True,
        recursive=False,
        ignore_error=None,
        progress=None,
        on_error=None,
        max_workers=None,
    ):
        """Copy as `Path.copy()` does. A directory copied to this server with
        `recursive=True` is concurrent requests on the loop, bounded by
        `backend.max_concurrency` (`progress` and `max_workers` are not used);
        anything else is `AsyncPath.copy()`."""
        sync, other = self._path, _sync_of(target)
        native = (
            recursive
            and isinstance(other, SFTPPath)
            and sync._same_filesystem(other)
            and (follow_symlinks or not await self.is_symlink())
            and await self.is_dir()
        )
        if not native:
            return await _AsyncPath.copy(
                self,
                target,
                overwrite=overwrite,
                follow_symlinks=follow_symlinks,
                preserve_metadata=preserve_metadata,
                recursive=recursive,
                ignore_error=ignore_error,
                progress=progress,
                on_error=on_error,
                max_workers=max_workers,
            )
        if contains(sync, other):
            raise OSError(
                _errno.EINVAL, "Cannot copy a directory into itself", str(other)
            )
        destination = type(self)(other)
        if await destination.exists():
            if not await destination.is_dir() or not overwrite:
                raise FileExistsError(
                    _errno.EEXIST, _os.strerror(_errno.EEXIST), str(other)
                )
        else:
            await destination.mkdir()
        offering = copy_error_predicate(ignore_error, on_error)
        link = await self._link()
        await _concurrent_copy(
            sync,
            other,
            overwrite=overwrite,
            follow_symlinks=follow_symlinks,
            preserve_metadata=preserve_metadata,
            max_concurrency=sync._backend.max_concurrency,
            ignore_error=ignore_error,
            aclient=link.aclient,
            **({} if offering is None else {"on_error": offering}),
        )
