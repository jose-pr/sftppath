"""pathlib-style paths for SFTP servers, on paramiko or asyncssh.

`SFTPPath` needs no URI and imports neither SSH library until a backend is
built: `SFTPBackend` (paramiko) and `AsyncSSHBackend` (asyncssh) are resolved
on first use (PEP 562), so `import sftppath` needs neither extra and never
imports `uripath`; naming one whose library is missing raises an `ImportError`
that names the extra. The `sftp:` scheme of `uripath` is `sftppath.uri`.
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
    from ._async import AsyncSFTPPath as AsyncSFTPPath
    from ._asyncssh import AsyncSSHBackend as AsyncSSHBackend
    from ._paramiko import SFTPBackend as SFTPBackend

__all__ = [
    "AsyncSFTPPath",
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

#: Public name -> (the private module that defines it, imported on first use;
#: the library and extra it needs).
_LAZY = {
    "AsyncSFTPPath": ("._async", "asyncssh"),
    "AsyncSSHBackend": ("._asyncssh", "asyncssh"),
    "SFTPBackend": ("._paramiko", "paramiko"),
}


def __getattr__(name: str):
    entry = _LAZY.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, library = entry
    try:
        loaded = _importlib.import_module(module, __name__)
    except ModuleNotFoundError as error:
        if (error.name or "").partition(".")[0] != library:
            raise
        raise ImportError(
            f"{name} needs the {library!r} extra: "
            f'pip install "sftppath[{library}]"',
            name=library,
        ) from error
    value = getattr(loaded, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
