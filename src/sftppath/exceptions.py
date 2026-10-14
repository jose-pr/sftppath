"""The exceptions `sftppath` defines.

A failure of the server or of the connection is the `OSError` type that
`pathlib` raises for the same condition (`FileNotFoundError`,
`ConnectionResetError`, `TimeoutError`, ...); this module holds the two that
name a cause no builtin does. An exception of the SSH library is never raised
across the API. A bad argument is a plain `ValueError` or `TypeError`.
"""

from __future__ import annotations

from pathlib_next import PathError

__all__ = ["SFTPAuthenticationError", "SFTPHostKeyError", "SFTPPathError"]


class SFTPPathError(PathError):
    """Base class of the exceptions `sftppath` defines for a failure at run
    time that no builtin describes."""


class SFTPAuthenticationError(SFTPPathError, PermissionError):
    """The server refused the credentials offered (`errno.EACCES`)."""


class SFTPHostKeyError(SFTPPathError, ConnectionError):
    """The server's host key was unknown, changed or not accepted, so no
    credential was sent (`errno.ECONNABORTED`)."""
