"""The status and connection errors of asyncssh as the `OSError` types of
pathlib (no asyncssh exception is an `OSError`)."""

from __future__ import annotations

import errno as _errno
import functools as _functools
import os as _os

import asyncssh as _asyncssh

from .. import _errors

# A server below SFTP v5 has no EEXIST or ENOTEMPTY: `SFTPPath._open()`/`_mkdir()`
# tell its generic failure apart by consulting the entry, so every failure is an OSError.


def _translate(
    error: "_asyncssh.SFTPError",
    filename: "str | None" = None,
    filename2: "str | None" = None,
) -> Exception:
    """The pathlib exception for an asyncssh `SFTPError`, built as pathlib
    builds it -- `(errno, strerror, filename[, filename2])` -- so `errno`
    and `filename` are set, as they are on the paramiko backend."""
    if isinstance(error, (_asyncssh.SFTPConnectionLost, _asyncssh.SFTPNoConnection)):
        return _errors.lost()
    if isinstance(error, (_asyncssh.SFTPNoSuchFile, _asyncssh.SFTPNoSuchPath)):
        cls, code = FileNotFoundError, _errno.ENOENT
    elif isinstance(error, _asyncssh.SFTPFileAlreadyExists):
        cls, code = FileExistsError, _errno.EEXIST
    elif isinstance(error, _asyncssh.SFTPDirNotEmpty):
        cls, code = OSError, _errno.ENOTEMPTY
    elif isinstance(error, _asyncssh.SFTPPermissionDenied):
        cls, code = PermissionError, _errno.EACCES
    elif isinstance(error, _asyncssh.SFTPOpUnsupported):
        return NotImplementedError(str(error))
    else:
        return _errors.bare_failure(str(error), filename, filename2)
    return _errors.os_error(
        cls, code, str(error) or _os.strerror(code), filename, filename2
    )


def _transport_error(
    error: "_asyncssh.Error", endpoint: "object | None" = None
) -> Exception:
    """The `OSError` for an asyncssh failure of the connection, the login or
    the host key; the same classes the paramiko backend raises."""
    if isinstance(error, _asyncssh.HostKeyNotVerifiable):
        return _errors.host_key_refused(endpoint)
    if isinstance(error, _asyncssh.PermissionDenied):
        return _errors.login_refused(endpoint)
    if isinstance(error, _asyncssh.ConnectionLost):
        return _errors.lost()
    if isinstance(error, (_asyncssh.DisconnectError, _asyncssh.ChannelOpenError)):
        return _errors.aborted(error)
    return _errors.request_failed(error)


def _library_error(
    error: "_asyncssh.Error",
    filename: "str | None" = None,
    filename2: "str | None" = None,
    endpoint: "object | None" = None,
) -> Exception:
    if isinstance(error, _asyncssh.SFTPError):
        return _translate(error, filename, filename2)
    return _transport_error(error, endpoint)


def _path_error(cls, code: int, path) -> OSError:
    return cls(code, _os.strerror(code), str(path))


#: `_SyncSFTPClient` methods whose second path argument is `filename2`.
_TWO_PATH_METHODS = frozenset({"rename", "posix_rename", "symlink", "link"})


def _reraise_sftp_errors(fn):
    two_paths = fn.__name__ in _TWO_PATH_METHODS

    @_functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _asyncssh.Error as error:
            # `(self, path, ...)` for a client method; a file method's
            # arguments (a size, a buffer) name no path.
            filename = args[1] if len(args) > 1 and isinstance(args[1], str) else None
            filename2 = (
                args[2]
                if two_paths and len(args) > 2 and isinstance(args[2], str)
                else None
            )
            raise _library_error(error, filename, filename2) from None

    return wrapper
