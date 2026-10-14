"""`AsyncSSHBackend`: the connections to one SFTP server on asyncssh."""

from __future__ import annotations

import typing as _ty

import asyncssh as _asyncssh

from .. import _checkfile
from .._backend import SFTPEndpoint
from .._sshconfig import DEFAULT_SSH_CONFIG
from . import _loop as _bridge
from ._cache import _CACHE
from ._client import _SyncSFTPClient
from ._connect import _aconnect
from ._translate import _transport_error
from ._walk import _concurrent_copy, _concurrent_rm


def _rebuild_backend(cls, state):
    return cls(**state)


class AsyncSSHBackend(_checkfile.CheckFileSFTPBackend):
    """The connections to one SFTP server and account, on `asyncssh` instead
    of paramiko. Selected automatically when `asyncssh` is importable (see
    backend selection in `SFTPPath`), or explicitly via
    `backend=AsyncSSHBackend(host)` / `SFTPPATH_BACKEND=asyncssh`. Nothing
    connects when the backend is built. The connection is cached per backend
    (see `_ConnectionCache`) and served through a single shared background
    asyncio loop (see `_run`) -- not fork-safe (a `fork()`ed child inherits a
    dead loop thread; detected via stored PID, loop+cache lazily recreated
    when `os.getpid()` changes). `close()` closes every connection the backend
    opened, returns when they are closed, and is harmless when called again.

    `host` may be a name from the user's ssh_config: asyncssh applies the
    matching block where the caller named no `port`, `user` or key. Host keys
    are verified by default: asyncssh checks the server key against
    `~/.ssh/known_hosts` and the ssh_config's `UserKnownHostsFile`, and an
    unknown or changed key raises `SFTPHostKeyError`. **Opt-out**, in code
    only: `AsyncSSHBackend(host, connect_opts={"known_hosts": None})` accepts
    any server key -- a network man-in-the-middle then receives the password.
    Any other asyncssh `known_hosts` value (a file, a list of keys) is passed
    through as given.

    `timeout` (default 60 s) bounds a single request -- connect, stat, open,
    mkdir, rename, ... -- and a timed-out request is cancelled and raises
    `TimeoutError`. Recursive `copy()`/`rm()`, streaming file reads/writes
    (`read_bytes()`, `write_bytes()`, chunked copies) and a native `checksum()`
    (the server hashes the whole file) have no wall-clock bound; asyncssh's own
    `connect_timeout`, `login_timeout` and `keepalive_interval` connect options
    detect a dead peer there. `timeout=None` disables the per-request bound
    too."""

    __slots__ = (
        "host",
        "port",
        "user",
        "password",
        "connect_opts",
        "max_concurrency",
        "sftp_version",
        "timeout",
    )

    #: asyncssh's chmod() takes follow_symlinks natively.
    supports_lchmod = True
    #: Recursive `copy()` and `rm()` have a concurrent implementation here.
    supports_tree = True
    #: asyncssh's SFTPClient.link() exists (SFTPv3 has no core hard-link
    #: op, but this works via the hardlink@openssh.com extension against
    #: real-world OpenSSH v3 servers, or the standard opcode against v5/v6).
    supports_hardlink = True

    #: Default bound on concurrent SFTP requests in a recursive copy or rm. On a
    #: loopback sweep of 1 to 16 copy improved up to 16 (~1.13x from 1 to 8, ~3% more
    #: to 16) and rm was flat; no network latency, so a remote link may favour more.
    DEFAULT_MAX_CONCURRENCY = 16

    def __init__(
        self,
        host: str,
        port: "int | None" = None,
        *,
        user: "str | None" = None,
        password: "str | None" = None,
        connect_opts: "dict[str, _ty.Any] | None" = None,
        max_concurrency: "int | None" = None,
        sftp_version: int = 4,
        ssh_config=DEFAULT_SSH_CONFIG,
        timeout: "float | None" = _bridge._DEFAULT_TIMEOUT,
    ):
        if not host:
            raise ValueError("an SFTP backend needs a host")
        if max_concurrency is None:
            max_concurrency = self.DEFAULT_MAX_CONCURRENCY
        self.host = str(host)
        self.port = None if port is None else int(port)
        self.user = user
        self.password = password
        self.connect_opts = {} if connect_opts is None else dict(connect_opts)
        if "config" not in self.connect_opts:
            if ssh_config is None:
                self.connect_opts["config"] = None
            elif ssh_config is not DEFAULT_SSH_CONFIG:
                self.connect_opts["config"] = ssh_config
        self.max_concurrency = max_concurrency
        self.sftp_version = sftp_version
        self.timeout = timeout

    @property
    def endpoint(self) -> SFTPEndpoint:
        return SFTPEndpoint(self.host.lower(), self.port, self.user)

    def __repr__(self) -> str:
        shown = [repr(self.host)]
        if self.port is not None:
            shown.append(str(self.port))
        if self.user is not None:
            shown.append(f"user={self.user!r}")
        return f"{type(self).__name__}({', '.join(shown)})"

    def __reduce__(self):
        # `connect_opts` already holds the ssh_config as `config`.
        return (
            _rebuild_backend,
            (
                type(self),
                {
                    "host": self.host,
                    "port": self.port,
                    "user": self.user,
                    "password": self.password,
                    "connect_opts": self.connect_opts,
                    "max_concurrency": self.max_concurrency,
                    "sftp_version": self.sftp_version,
                    "timeout": self.timeout,
                },
            ),
        )

    def client(self) -> _SyncSFTPClient:
        try:
            entry = _CACHE.get_or_create(
                (self,),
                lambda: _bridge._run(
                    _aconnect(
                        self.host,
                        self.port,
                        self.user,
                        self.password,
                        connect_opts=self.connect_opts,
                        sftp_version=self.sftp_version,
                        timeout=self.timeout,
                    ),
                    self.timeout,
                ),
            )
        except _asyncssh.Error as error:
            raise _transport_error(error, self.endpoint) from None
        return entry.client

    def close(self) -> None:
        """Close every cached connection this backend opened."""
        _CACHE.close_backend(self)

    def _check_file_request(
        self, client: _SyncSFTPClient, file, algorithm: str
    ) -> bytes:
        # SSH_FX_OP_UNSUPPORTED and an unexpected reply type both arrive as
        # NotImplementedError, which `_checkfile.refused()` reads as a refusal.
        return client.check_file(file, algorithm)

    # The connection is resolved on the calling thread: looking it up on the
    # bridge loop would open it through a blocking `_run()` on that loop.
    def tree_rm(self, path, *, missing_ok, on_error):
        return _bridge._run(
            _concurrent_rm(
                path,
                max_concurrency=self.max_concurrency,
                missing_ok=missing_ok,
                on_error=on_error,
                aclient=path._sftpclient._aclient,
            ),
            None,
        )

    def tree_copy(
        self,
        path,
        target,
        *,
        overwrite,
        follow_symlinks,
        preserve_metadata,
        ignore_error,
        on_error=None,
    ):
        return _bridge._run(
            _concurrent_copy(
                path,
                target,
                overwrite=overwrite,
                follow_symlinks=follow_symlinks,
                preserve_metadata=preserve_metadata,
                max_concurrency=self.max_concurrency,
                ignore_error=ignore_error,
                on_error=on_error,
                aclient=path._sftpclient._aclient,
            ),
            None,
        )
