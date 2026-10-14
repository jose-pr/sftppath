"""pathlib-style paths for SFTP servers, on paramiko or asyncssh.

`SFTPPath` needs no URI and imports neither SSH library until a backend is
built: `SFTPBackend` (paramiko) and `AsyncSSHBackend` (asyncssh) are resolved
on first use (PEP 562), so `import sftppath` needs neither extra and never
imports `uripath`. The `sftp:` scheme of `uripath` is `sftppath.uri`.
"""

from __future__ import annotations

import importlib as _importlib
import typing as _ty

from ._backend import DEFAULT_PORT, BaseSFTPBackend, SFTPEndpoint
from ._checkfile import CheckFileSFTPBackend
from ._path import SFTPPath
from ._sshconfig import DEFAULT_SSH_CONFIG
from ._version import __version__
from .exceptions import SFTPAuthenticationError, SFTPHostKeyError, SFTPPathError

if _ty.TYPE_CHECKING:
    from ._asyncssh import AsyncSSHBackend as AsyncSSHBackend
    from ._paramiko import SFTPBackend as SFTPBackend

__all__ = [
    "AsyncSSHBackend",
    "BaseSFTPBackend",
    "CheckFileSFTPBackend",
    "DEFAULT_PORT",
    "DEFAULT_SSH_CONFIG",
    "SFTPAuthenticationError",
    "SFTPBackend",
    "SFTPEndpoint",
    "SFTPHostKeyError",
    "SFTPPath",
    "SFTPPathError",
    "__version__",
]

#: Public name -> the private module that defines it, imported on first use.
_LAZY = {
    "AsyncSSHBackend": "._asyncssh",
    "SFTPBackend": "._paramiko",
}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(_importlib.import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
