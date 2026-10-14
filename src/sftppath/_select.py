"""Which backend class a path builds when it is given none.

Highest first: `backend=` > the `_default_backend_cls` of the path class >
`SFTPPATH_BACKEND` > auto-detect (asyncssh, else paramiko). Both libraries are
probed lazily: importing the package needs neither SSH library, and a path named
without one fails when it makes its first request (`_MissingBackend`).
"""

from __future__ import annotations

import os as _os

from ._backend import BaseSFTPBackend, SFTPEndpoint

_ENV_VAR = "SFTPPATH_BACKEND"
_BACKEND_REGISTRY: "dict[str, type[BaseSFTPBackend]]" = {}
_asyncssh_probed = False
_paramiko_probed = False
_resolved_backend_cls: "type[BaseSFTPBackend] | None" = None


def _probe_asyncssh() -> None:
    # Lazy and only once: a caller that forces SFTPPATH_BACKEND=paramiko, or
    # never resolves a backend, never imports asyncssh.
    global _asyncssh_probed
    if _asyncssh_probed:
        return
    _asyncssh_probed = True
    try:
        from ._asyncssh import AsyncSSHBackend
    except ImportError:
        return
    _BACKEND_REGISTRY["asyncssh"] = AsyncSSHBackend


def _probe_paramiko() -> None:
    # Symmetric with `_probe_asyncssh`: only import paramiko when it is actually
    # needed (paramiko selected, or auto-detect with asyncssh unavailable), so an
    # asyncssh-only install never imports paramiko.
    global _paramiko_probed
    if _paramiko_probed:
        return
    _paramiko_probed = True
    try:
        from ._paramiko import SFTPBackend
    except ImportError:
        return
    _BACKEND_REGISTRY["paramiko"] = SFTPBackend


def _resolve_default_backend_cls(reload: bool = False) -> "type[BaseSFTPBackend]":
    global _resolved_backend_cls
    if not reload and _resolved_backend_cls is not None:
        return _resolved_backend_cls
    value = _os.environ.get(_ENV_VAR, "auto")
    if value == "paramiko":
        _probe_paramiko()
        if "paramiko" not in _BACKEND_REGISTRY:
            raise ImportError(
                f"{_ENV_VAR}=paramiko but the paramiko package is not "
                'installed -- pip install "sftppath[paramiko]", or unset '
                f"{_ENV_VAR} to auto-detect (uses asyncssh if available)."
            )
        cls = _BACKEND_REGISTRY["paramiko"]
    elif value == "asyncssh":
        _probe_asyncssh()
        if "asyncssh" not in _BACKEND_REGISTRY:
            # Fail loud -- a silent fallback to paramiko would hide a
            # deployment misconfiguration (asyncssh extra not installed
            # where the operator explicitly asked for it).
            raise ImportError(
                f"{_ENV_VAR}=asyncssh but the asyncssh package is not "
                'installed -- pip install "sftppath[asyncssh]", or unset '
                f"{_ENV_VAR} to auto-detect (falls back to paramiko)."
            )
        cls = _BACKEND_REGISTRY["asyncssh"]
    elif value == "auto":
        _probe_asyncssh()
        if "asyncssh" not in _BACKEND_REGISTRY:
            _probe_paramiko()
        cls = _BACKEND_REGISTRY.get("asyncssh") or _BACKEND_REGISTRY.get("paramiko")
        if cls is None:
            raise ImportError(
                "no SFTP backend available -- install the 'asyncssh' "
                "(pip install \"sftppath[asyncssh]\") or the 'paramiko' "
                '(pip install "sftppath[paramiko]") extra.'
            )
    else:
        raise ValueError(
            f"{_ENV_VAR}={value!r} is not a recognized SFTP backend "
            "(expected one of 'auto', 'asyncssh', 'paramiko')"
        )
    _resolved_backend_cls = cls
    return cls


class _MissingBackend(BaseSFTPBackend):
    """The backend of a path named while no SSH library it can use is
    installed. It keeps the endpoint, so the path can still be named, joined and
    compared; `client()` raises the `ImportError` that names the extras."""

    __slots__ = ("host", "port", "user", "_message")

    def __init__(self, host, port, user, message: str) -> None:
        if not host:
            raise ValueError("an SFTP backend needs a host")
        self.host = str(host)
        self.port = None if port is None else int(port)
        self.user = user
        self._message = message

    @property
    def endpoint(self) -> SFTPEndpoint:
        return SFTPEndpoint(self.host.lower(), self.port, self.user)

    def client(self):
        raise ImportError(self._message)

    def __repr__(self) -> str:
        shown = [repr(self.host)]
        if self.port is not None:
            shown.append(str(self.port))
        if self.user is not None:
            shown.append(f"user={self.user!r}")
        return f"{type(self).__name__}({', '.join(shown)})"

    def __reduce__(self):
        return type(self), (self.host, self.port, self.user, self._message)
