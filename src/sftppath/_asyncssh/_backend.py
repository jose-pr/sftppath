"""`AsyncSSHBackend`: the connections to one SFTP server on asyncssh."""

from __future__ import annotations

import asyncio as _asyncio
import typing as _ty

import asyncssh as _asyncssh

from .. import _checkfile, _errors
from .._backend import SFTPEndpoint
from .._sshconfig import DEFAULT_SSH_CONFIG
from . import _loop as _bridge
from ._cache import _CACHE
from ._client import _SyncSFTPClient
from ._connect import _aconnect, _aopen_on
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
    too.

    `over_connection(connection, loop)` builds one that runs SFTP over an
    asyncssh connection the caller has already opened, on the loop that owns
    it, instead of connecting itself."""

    __slots__ = (
        "host",
        "port",
        "user",
        "password",
        "connect_opts",
        "max_concurrency",
        "sftp_version",
        "timeout",
        "_connection",
        "_caller",
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
        self._connection: "_asyncssh.SSHClientConnection | None" = None
        self._caller: "_bridge._CallerLoop | None" = None

    @classmethod
    def over_connection(
        cls,
        connection: "_asyncssh.SSHClientConnection",
        loop: "_asyncio.AbstractEventLoop",
        *,
        host: "str | None" = None,
        max_concurrency: "int | None" = None,
        sftp_version: int = 4,
        timeout: "float | None" = _bridge._DEFAULT_TIMEOUT,
    ) -> "AsyncSSHBackend":
        """A backend that opens its SFTP channel on `connection`, an
        authenticated asyncssh client connection the caller opened, and runs
        every request on `loop`, the running event loop `connection` is bound
        to, instead of on this package's own loop thread. Nothing is sent
        until the first request; no second login is made.

        The connection stays the caller's: `close()` closes only the SFTP
        channel the backend opened (the next request opens another on the
        same connection), and the backend never closes the connection. Once
        the connection is closed, every request raises
        `ConnectionResetError`. `loop` must keep running while the backend
        is used; a request made while it is closed or stopped, or that is
        waiting when it stops, raises `RuntimeError`, as does a request made
        on the thread running `loop` (it would deadlock it) and any use in a
        `fork()`ed child. Such a backend cannot be pickled (`TypeError`).

        `host` names the server in `endpoint`, `repr()` and `as_uri()`
        (default: the peer address of `connection`; `ValueError` when it has
        none, as through a tunnel); `port` is the peer port and `user` the
        connection's login name. `max_concurrency`, `sftp_version` and
        `timeout` are those of the constructor. `TypeError` when
        `connection` is not an `asyncssh.SSHClientConnection` or `loop` not
        an event loop, `ValueError` when `connection` is bound to another
        loop."""
        if not isinstance(connection, _asyncssh.SSHClientConnection):
            raise TypeError(
                "connection must be an asyncssh.SSHClientConnection, not "
                f"{type(connection).__name__}"
            )
        if not isinstance(loop, _asyncio.AbstractEventLoop):
            raise TypeError(
                f"loop must be an asyncio event loop, not {type(loop).__name__}"
            )
        # asyncssh keeps the loop a connection was made on as `_loop`; a
        # request scheduled on any other loop would touch it from two threads.
        if getattr(connection, "_loop", loop) is not loop:
            raise ValueError("connection is bound to another event loop than loop")
        peer = connection.get_extra_info("peername")
        port = None
        if isinstance(peer, tuple) and len(peer) >= 2:
            port = peer[1] or None
            if host is None:
                host = peer[0] or None
        if not host:
            raise ValueError(
                "the connection has no peer address to name the server: pass host="
            )
        backend = cls.__new__(cls)
        AsyncSSHBackend.__init__(
            backend,
            host,
            port,
            user=connection.get_extra_info("username") or None,
            max_concurrency=max_concurrency,
            sftp_version=sftp_version,
            timeout=timeout,
        )
        backend._connection = connection
        backend._caller = _bridge._CallerLoop(loop)
        return backend

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
        if self._connection is not None:
            raise TypeError(
                f"an {type(self).__name__} over a caller's connection cannot be "
                "pickled: the connection belongs to this process"
            )
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
            entry = _CACHE.get_or_create((self,), self._open_entry)
        except _asyncssh.Error as error:
            connection = self._connection
            if connection is not None and connection.is_closed():
                raise _errors.connection_closed() from None
            raise _transport_error(error, self.endpoint) from None
        return entry.client

    def _open_entry(self):
        if self._connection is not None:
            return _bridge._run(
                _aopen_on(
                    self._connection, self.sftp_version, self.timeout, self._caller
                ),
                self.timeout,
                self._caller,
            )
        return _bridge._run(
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
        )

    def close(self) -> None:
        """Close every cached connection this backend opened; over a caller's
        connection, the SFTP channel opened on it."""
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
            self._caller,
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
            self._caller,
        )
