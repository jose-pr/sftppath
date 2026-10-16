"""The exceptions both SSH libraries raise for a connection, a login or a host
key, as the `OSError` subclasses `SFTPPath` raises instead.

Neither SSH library is imported here: each backend decides which of its own
exceptions is which and builds the result with these functions, so a failure
reads the same on both. The library exception is never chained: its text can
carry credentials. The text of the library exceptions that `aborted()` and
`request_failed()` reduce to a class name is logged at DEBUG on the logger
`sftppath`.
"""

from __future__ import annotations

import errno as _errno
import logging as _logging
import os as _os

from .exceptions import SFTPAuthenticationError, SFTPHostKeyError

_logger = _logging.getLogger("sftppath")


def _log_library_error(what: str, library_error: BaseException) -> None:
    # A DEBUG record, not part of the exception: the library's text can carry
    # credentials, and only a handler the program installed ever sees it.
    _logger.debug("%s: %s: %s", what, type(library_error).__name__, library_error)


def _where(endpoint: "object | None") -> str:
    """The server an error is about, from anything with `host`, `port` and
    `user` attributes (an `SFTPEndpoint`): never a password."""
    host = getattr(endpoint, "host", None)
    if not host:
        return "the SFTP server"
    user = getattr(endpoint, "user", None)
    port = getattr(endpoint, "port", None)
    return f"{user + '@' if user else ''}{host}{f':{port}' if port else ''}"


def os_error(
    cls: "type[OSError]",
    code: int,
    strerror: str,
    filename: "str | None" = None,
    filename2: "str | None" = None,
) -> OSError:
    """`cls(code, strerror, filename)`, pathlib's shape, with `filename2` set
    afterwards: the five-argument form prints a winerror of None as
    "[WinError None]" on Windows."""
    if filename is None:
        return cls(code, strerror)
    result = cls(code, strerror, filename)
    if filename2 is not None:
        result.filename2 = filename2
    return result


def bare_failure(
    message: str, filename: "str | None" = None, filename2: "str | None" = None
) -> OSError:
    """An `OSError` without an errno, for a status that has no POSIX
    equivalent: `SFTPPath` reads `errno is None` as "consult the entry
    itself"."""
    if filename is None:
        return OSError(message)
    result = OSError(None, message, filename)
    if filename2 is not None:
        result.filename2 = filename2
    return result


def is_a_directory(filename: "str | None") -> IsADirectoryError:
    """What reading a directory as a file raises, as pathlib raises it."""
    return os_error(
        IsADirectoryError, _errno.EISDIR, _os.strerror(_errno.EISDIR), filename
    )


def lost() -> ConnectionResetError:
    """The connection went away while a request was in flight."""
    return ConnectionResetError(_errno.ECONNRESET, "SFTP connection lost")


def aborted(library_error: BaseException) -> ConnectionAbortedError:
    """The SSH handshake or session failed for a reason other than a login or
    a host key; only the name of the library's exception is carried over, and
    its text is logged at DEBUG."""
    _log_library_error("SFTP connection failed", library_error)
    return ConnectionAbortedError(
        _errno.ECONNABORTED,
        f"SFTP connection failed ({type(library_error).__name__})",
    )


def request_failed(library_error: BaseException) -> OSError:
    """A failure of the SSH library that is neither the connection, the login
    nor the host key; like `aborted()`, only the exception's name is carried."""
    _log_library_error("SFTP request failed", library_error)
    return OSError(_errno.EIO, f"SFTP request failed ({type(library_error).__name__})")


def login_refused(endpoint: "object | None" = None) -> SFTPAuthenticationError:
    return SFTPAuthenticationError(
        _errno.EACCES, f"SFTP authentication to {_where(endpoint)} was refused"
    )


def host_key_refused(endpoint: "object | None" = None) -> SFTPHostKeyError:
    return SFTPHostKeyError(
        _errno.ECONNABORTED,
        f"the host key of {_where(endpoint)} is unknown, changed or not accepted",
    )
