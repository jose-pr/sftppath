from __future__ import annotations

import errno as _errno
import functools as _functools
import io as _io
import pathlib as _pathlib
import socket as _socket
import threading as _thread

import paramiko as _paramiko
import paramiko.sftp as _paramiko_sftp
from paramiko.sftp_attr import SFTPAttributes as _SFTPAttributes
from pathlib_next import providers as _providers

from . import _checkfile, _errors
from ._backend import DEFAULT_PORT, SFTPEndpoint
from ._paramiko_config import _SSHConfig, _lookup_ssh_config

# The sentinel and path normalization are paramiko-free and live in `_sshconfig`.
from ._sshconfig import (
    DEFAULT_SSH_CONFIG,
    _Sentinel,
    _check_host,
    _expand_proxy_command,
)

#: Sentinel for `SFTPBackend(known_hosts=...)`: the user's `~/.ssh/known_hosts`
#: plus every `UserKnownHostsFile` the ssh_config names for the host.
_DEFAULT_KNOWN_HOSTS = _Sentinel(__name__, "_DEFAULT_KNOWN_HOSTS")


# --- the SFTP client ---------------------------------------------------------


def _wire_path(path):
    """A `str` path as the bytes it stands for on the wire: a lone surrogate is
    a byte of a name that was not UTF-8 when it was listed."""
    return path.encode("utf-8", "surrogateescape") if isinstance(path, str) else path


def _text(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


class _SFTPFile(_paramiko.SFTPFile):
    """paramiko's file with the `io` behaviours it lacks: `write()` returns
    the number of bytes accepted, `truncate()` takes no size and flushes, and
    `fileno()` says it has none."""

    def write(self, data):
        count = (
            len(data.encode("utf-8"))
            if isinstance(data, str)
            else memoryview(data).nbytes
        )
        super().write(data)
        return count

    def truncate(self, size=None):
        # `io` semantics: no size means the current position, and what was
        # written is on the server before it is cut.
        self.flush()
        if size is None:
            size = self.tell()
        super().truncate(size)
        return size

    def fileno(self):
        raise _io.UnsupportedOperation("fileno")


def _named_errors(method, two_paths: bool = False):
    """`method(self, path, ...)` with the path(s) in the `filename`
    (`filename2`) of the `OSError` a server status raised, as pathlib's own
    errors carry them."""

    @_functools.wraps(method)
    def wrapper(self, path, *args, **kwargs):
        try:
            return method(self, path, *args, **kwargs)
        except OSError as error:
            if error.filename is None and not isinstance(
                error, (ConnectionError, TimeoutError)
            ):
                error.filename = path if isinstance(path, str) else _text(path)
                if two_paths and args:
                    error.filename2 = args[0]
            raise

    return wrapper


class _SFTPClient(_paramiko.SFTPClient):
    """paramiko's client with the status mapping and the names the asyncssh
    backend has: an "operation unsupported" status is `NotImplementedError`,
    a lost connection `ConnectionResetError`, an error carries the path it
    was about, and a name that is not UTF-8 lists, opens and removes as the
    lone-surrogate string of its bytes."""

    def _convert_status(self, msg):
        code = msg.get_int()
        text = msg.get_text()
        if code == _paramiko_sftp.SFTP_OK:
            return
        if code == _paramiko_sftp.SFTP_EOF:
            raise EOFError(text)
        if code == _paramiko_sftp.SFTP_NO_SUCH_FILE:
            raise IOError(_errno.ENOENT, text)
        if code == _paramiko_sftp.SFTP_PERMISSION_DENIED:
            raise IOError(_errno.EACCES, text)
        if code == _paramiko_sftp.SFTP_OP_UNSUPPORTED:
            raise NotImplementedError(text)
        if code in (
            _paramiko_sftp.SFTP_NO_CONNECTION,
            _paramiko_sftp.SFTP_CONNECTION_LOST,
        ):
            raise _errors.lost()
        raise IOError(text)

    def _read_response(self, waitfor=None):
        try:
            return super()._read_response(waitfor)
        except _paramiko.SSHException:
            # "Server connection dropped": the only SSHException it raises.
            raise _errors.lost() from None

    def _send_packet(self, t, packet):
        try:
            super()._send_packet(t, packet)
        except (EOFError, _paramiko.SSHException):
            raise _errors.lost() from None
        except OSError as error:
            if error.errno is not None:
                raise
            # A closed channel: "Socket is closed".
            raise _errors.lost() from None

    def _adjust_cwd(self, path):
        return super()._adjust_cwd(_wire_path(path))

    def symlink(self, source, dest):
        return super().symlink(_wire_path(source), dest)

    def listdir_attr(self, path="."):
        # paramiko decodes every listed name as strict UTF-8, so one other
        # name fails the whole listing.
        path = self._adjust_cwd(path)
        kind, msg = self._request(_paramiko_sftp.CMD_OPENDIR, path)
        if kind != _paramiko_sftp.CMD_HANDLE:
            raise _paramiko.SFTPError("Expected handle")
        handle = msg.get_binary()
        entries = []
        while True:
            try:
                kind, msg = self._request(_paramiko_sftp.CMD_READDIR, handle)
            except EOFError:
                break
            if kind != _paramiko_sftp.CMD_NAME:
                raise _paramiko.SFTPError("Expected name response")
            for _ in range(msg.get_int()):
                filename = _text(msg.get_string())
                longname = _text(msg.get_string())
                attrs = _SFTPAttributes._from_msg(msg, filename, longname)
                if filename not in (".", ".."):
                    entries.append(attrs)
        self._request(_paramiko_sftp.CMD_CLOSE, handle)
        return entries

    def readlink(self, path):
        path = self._adjust_cwd(path)
        kind, msg = self._request(_paramiko_sftp.CMD_READLINK, path)
        if kind != _paramiko_sftp.CMD_NAME:
            raise _paramiko.SFTPError("Expected name response")
        count = msg.get_int()
        if count == 0:
            return None
        if count != 1:
            raise _paramiko.SFTPError(f"Readlink returned {count} results")
        return _text(msg.get_string())

    def open(self, filename, mode="r", bufsize=-1):
        file = super().open(filename, mode, bufsize)
        # The same attributes and slots: only `write()` and `fileno()` differ.
        file.__class__ = _SFTPFile
        return file


for (
    _name
) in (
    "stat lstat listdir_attr open mkdir chmod chown utime remove rmdir readlink".split()
):
    setattr(_SFTPClient, _name, _named_errors(getattr(_SFTPClient, _name)))
for _name in "rename posix_rename symlink".split():
    setattr(_SFTPClient, _name, _named_errors(getattr(_SFTPClient, _name), True))
del _name


class _NoTransport(_paramiko.SSHException):
    """`connect()` returned without a transport: a broken invariant, not a
    failure of the connection, so it is raised as it is."""


def _timed_out(error: BaseException) -> bool:
    """Whether `error`, or what it was raised while handling, is a socket
    timeout: paramiko re-raises one as an `SSHException` ("Error reading SSH
    protocol banner") or words its own ("Authentication timeout")."""
    seen = 0
    while error is not None and seen < 8:
        if isinstance(error, _socket.timeout):
            return True
        error = error.__cause__ or error.__context__
        seen += 1
    return False


def _connect_error(error: BaseException, endpoint: SFTPEndpoint) -> "Exception | None":
    """The `OSError` for a paramiko failure to connect, log in or open the
    SFTP session, or None when `error` is not one of those."""
    if isinstance(error, _NoTransport):
        return None
    if isinstance(error, _paramiko.ssh_exception.NoValidConnectionsError):
        # Every address refused: say what the first one said, as asyncssh does.
        for cause in error.errors.values():
            if isinstance(cause, OSError) and cause.errno is not None:
                return OSError(cause.errno, cause.strerror)
        return None
    if isinstance(error, _paramiko.BadHostKeyException):
        return _errors.host_key_refused(endpoint)
    if isinstance(error, _paramiko.SSHException):
        text = str(error)
        if _timed_out(error) or "timeout" in text.lower():
            return TimeoutError(_errno.ETIMEDOUT, "SFTP connection timed out")
        # `RejectPolicy` (the default) refuses a key no known_hosts file has.
        if "not found in known_hosts" in text:
            return _errors.host_key_refused(endpoint)
        # "No authentication methods available": nothing was left to try.
        if (
            isinstance(error, _paramiko.AuthenticationException)
            or "No authentication methods" in text
        ):
            return _errors.login_refused(endpoint)
        return _errors.aborted(error)
    if isinstance(error, EOFError):
        return _errors.lost()
    if isinstance(error, _socket.timeout) and not isinstance(error, TimeoutError):
        # Python 3.9: socket.timeout is not yet TimeoutError.
        return TimeoutError(_errno.ETIMEDOUT, "SFTP connection timed out")
    return None


def _create_sftpclient(backend: "SFTPBackend", thread_id: int):
    transport = backend.transport()
    try:
        client = transport.open_sftp_client()
        if client is None:
            raise _paramiko.SSHException("the server refused the sftp subsystem")
        if type(client) is _paramiko.SFTPClient:
            # `_SFTPClient` adds behaviour and no state.
            client.__class__ = _SFTPClient
        return client
    except BaseException as error:
        transport.close()
        translated = _connect_error(error, backend.endpoint)
        if translated is None:
            raise
        raise translated from None


def _close_sftpclient(key: tuple, client) -> None:
    # An SFTPClient going out of scope closes nothing: its Transport is a
    # running thread that the threading module keeps alive, so an evicted or
    # dead client must have its transport closed explicitly.
    try:
        client.close()
    finally:
        transport = client.sock.get_transport()
        if transport is not None:
            transport.close()


def _client_is_alive(client) -> bool:
    # `Channel.active` is set once on open and never cleared on close, so it
    # cannot detect a dropped connection on its own; `closed` and the
    # transport's own liveness can.
    if client is None:
        return False
    channel = getattr(client, "sock", None)
    if channel is None:
        return False
    if not getattr(channel, "active", True) or getattr(channel, "closed", False):
        return False
    get_transport = getattr(channel, "get_transport", None)
    if get_transport is None:
        return True
    transport = get_transport()
    return transport is not None and bool(transport.is_active())


# Thread-keyed (paramiko's client is bound to the thread owning its socket loop)
# and module-level, so every SFTPBackend shares it, keyed (backend, thread_id).
# Evicted and invalidated clients have their transport closed.
_CACHED_CLIENTS = _providers.LRU(
    _create_sftpclient, maxsize=128, on_evict=_close_sftpclient
)


def _close_clients_of_ended_threads() -> None:
    """Close the cached clients of threads that have ended: nothing uses
    them again, and the LRU would only close one when it evicts it."""
    running = {thread.ident for thread in _thread.enumerate()}
    for key in list(_CACHED_CLIENTS.cache):
        if key[1] not in running:
            _CACHED_CLIENTS.discard(*key)


def _rebuild_backend(cls, state):
    return cls(**state)


class SFTPBackend(_checkfile.CheckFileSFTPBackend):
    """The connections to one SFTP server and account, on `paramiko.SSHClient`:
    `connect_opts` merged with `host`, `port`, `user` and `password`.
    Nothing connects when the backend is built. `client()` caches per
    `(self, calling thread)` -- see `_CACHED_CLIENTS` above -- and replaces
    (and closes) a cached client whose connection has dropped; `close()`
    closes every connection the backend opened, on any thread, and is
    harmless when called again.

    `host` may be a name from the user's ssh_config: `HostName`, `Port`,
    `User` and `IdentityFile` of the matching block apply where the caller
    named no `port`, `user` or key. Host keys are verified by default.
    `known_hosts` (default: the user's `~/.ssh/known_hosts` plus any
    ssh_config `UserKnownHostsFile` for the host; `None` loads no file; a path
    or iterable of paths loads exactly those) is loaded read-only, and
    `hostkeypolicy` (default `paramiko.RejectPolicy()`) decides what happens
    to a key found in none of them. A key that differs from a known one
    always raises `SFTPHostKeyError`, as one the policy refuses does.
    **Opt-out**, in code only:
    `SFTPBackend(host, hostkeypolicy=paramiko.AutoAddPolicy(), known_hosts=None)`
    accepts any server key -- a network man-in-the-middle then receives the
    password.

    `timeout` (default 30 s) is the default for paramiko's `timeout`,
    `banner_timeout`, `auth_timeout` and `channel_timeout` connect options;
    a value in `connect_opts` wins, and `timeout=None` leaves them unset.
    Individual SFTP requests on an established connection are not bounded.

    ssh_config support is paramiko's own (`HostName`, `Port`, `User`,
    `IdentityFile`, `ProxyCommand`) plus `Include`. `ProxyJump` is not
    supported: a host whose config sets it raises `NotImplementedError`
    unless `connect_opts` supplies a `sock` -- use the asyncssh backend, or
    an equivalent `ProxyCommand`."""

    __slots__ = (
        "host",
        "port",
        "user",
        "password",
        "connect_opts",
        "hostkeypolicy",
        "ssh_config",
        "known_hosts",
        "timeout",
    )
    connect_opts: dict[str, str]
    hostkeypolicy: _paramiko.MissingHostKeyPolicy

    #: Default connect/banner/auth/channel-open timeout, in seconds.
    DEFAULT_TIMEOUT = 30.0

    def _wire_open_mode(self, mode: str) -> str:
        # paramiko derives SFTP_FLAG_WRITE only from "w"/"a"/"+", so a bare "x" created a
        # file that could not be written. "w" adds CREATE|TRUNC, moot under EXCL.
        if "x" in mode and "w" not in mode:
            return "w" + mode
        return mode

    def __init__(
        self,
        host: str,
        port: "int | None" = None,
        *,
        user: "str | None" = None,
        password: "str | None" = None,
        connect_opts=None,
        hostkeypolicy=None,
        ssh_config=DEFAULT_SSH_CONFIG,
        known_hosts=_DEFAULT_KNOWN_HOSTS,
        timeout: "float | None" = DEFAULT_TIMEOUT,
    ) -> None:
        if not host:
            raise ValueError("an SFTP backend needs a host")
        self.host = str(host)
        self.port = None if port is None else int(port)
        self.user = user
        self.password = password
        self.connect_opts = {} if connect_opts is None else connect_opts
        self.hostkeypolicy = (
            _paramiko.RejectPolicy() if hostkeypolicy is None else hostkeypolicy
        )
        self.ssh_config = ssh_config
        self.known_hosts = known_hosts
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
                    "hostkeypolicy": self.hostkeypolicy,
                    "ssh_config": self.ssh_config,
                    "known_hosts": self.known_hosts,
                    "timeout": self.timeout,
                },
            ),
        )

    def opts(self):
        """The keyword arguments `SSHClient.connect()` is called with: the
        `connect_opts` merged with the ssh_config of the host (a
        `ProxyCommand` starts its process here)."""
        _check_host(self.host)
        host = self.host
        config = _lookup_ssh_config(host, self.ssh_config)
        connect_ops = {
            **self.connect_opts,
            "hostname": config.get("hostname", host),
            "port": self.port or int(config.get("port", DEFAULT_PORT)),
        }
        if self.timeout is not None:
            for key in ("timeout", "banner_timeout", "auth_timeout", "channel_timeout"):
                connect_ops.setdefault(key, self.timeout)
        if self.user:
            connect_ops["username"] = self.user
        elif "username" not in connect_ops and "user" in config:
            connect_ops["username"] = str(config["user"])
        if self.password:
            connect_ops["password"] = self.password
        if "key_filename" not in connect_ops and "identityfile" in config:
            connect_ops["key_filename"] = list(config["identityfile"])
        if "sock" not in connect_ops:
            if config.get("proxycommand"):
                connect_ops["sock"] = _paramiko.ProxyCommand(
                    _expand_proxy_command(
                        str(config["proxycommand"]),
                        host=host,
                        hostname=str(connect_ops["hostname"]),
                        port=connect_ops["port"],
                        user=connect_ops.get("username"),
                    )
                )
            elif str(config.get("proxyjump") or "none").lower() != "none":
                # Connecting directly would silently bypass the jump host.
                raise NotImplementedError(
                    f"ssh_config ProxyJump ({config['proxyjump']}) for host "
                    f"{self.host!r} is not supported by the paramiko SFTP "
                    "backend -- use the asyncssh backend ('asyncssh' extra), "
                    "an equivalent ProxyCommand, or connect_opts['sock']"
                )
        return connect_ops

    def _known_hosts_files(self) -> "list[str]":
        known_hosts = self.known_hosts
        if known_hosts is None:
            return []
        if known_hosts is not _DEFAULT_KNOWN_HOSTS:
            if isinstance(known_hosts, (str, _pathlib.PurePath)):
                return [str(known_hosts)]
            return [str(path) for path in known_hosts]
        _check_host(self.host)
        home = str(_pathlib.Path.home())
        files = [str(_pathlib.Path(home, ".ssh", "known_hosts"))]
        config = _lookup_ssh_config(self.host, self.ssh_config)
        for value in str(config.get("userknownhostsfile") or "").split():
            if value.lower() != "none":
                files.append(str(_pathlib.Path(value.replace("%d", home)).expanduser()))
        # Default locations are optional; explicitly named files are not.
        return [path for path in files if _pathlib.Path(path).is_file()]

    def transport(self) -> _paramiko.Transport:
        """Connects and returns the `paramiko.Transport`."""
        opts = self.opts()
        client = _paramiko.SSHClient()
        try:
            for path in self._known_hosts_files():
                # load_system_host_keys(): read-only, never written back --
                # an AutoAddPolicy must not edit the user's files either.
                client.load_system_host_keys(path)
            client.set_missing_host_key_policy(self.hostkeypolicy)
            client.connect(**opts)
            transport = client.get_transport()
            if not transport:
                raise _NoTransport("connect() produced no transport")
        except BaseException as error:
            # A failed connect/auth still leaves a running Transport thread
            # and an open socket (or a ProxyCommand subprocess) behind.
            client.close()
            sock = opts.get("sock")
            if sock is not None and "sock" not in self.connect_opts:
                try:
                    sock.close()
                except Exception:
                    pass
            translated = _connect_error(error, self.endpoint)
            if translated is None:
                raise
            raise translated from None
        return transport

    def client(self):
        thread_id = _thread.get_ident()
        if (self, thread_id) not in _CACHED_CLIENTS.cache:
            _close_clients_of_ended_threads()
        client = _CACHED_CLIENTS(self, thread_id)
        if not _client_is_alive(client):
            client = _CACHED_CLIENTS.invalidate(self, thread_id)
        return client

    def close(self) -> None:
        """Close every cached connection this backend opened, on any thread."""
        for key in list(_CACHED_CLIENTS.cache):
            if key[0] is self:
                _CACHED_CLIENTS.discard(*key)

    def _check_file_request(self, client, file, algorithm: str) -> bytes:
        # Not paramiko public API: the `_request(CMD_EXTENDED, ...)` primitive it uses for
        # `posix-rename@openssh.com`. SSH_FX_OP_UNSUPPORTED arrives as NotImplementedError,
        # any other failure status as OSError.
        msg_type, msg = client._request(
            _paramiko_sftp.CMD_EXTENDED,
            _checkfile.EXTENSION,
            file.handle,
            algorithm,  # hash-algorithm-list: just the one wanted
            # int64(...): a plain `int` is packed as 32 bits by Message.add(), and the wire
            # format wants uint64 for start-offset and length.
            _paramiko_sftp.int64(_checkfile.START_OFFSET),
            _paramiko_sftp.int64(_checkfile.LENGTH),
            _checkfile.BLOCK_SIZE,
        )
        if msg_type != _paramiko_sftp.CMD_EXTENDED_REPLY:
            raise NotImplementedError(
                f"{_checkfile.EXTENSION}: unexpected reply type "
                f"{msg_type!r} (server likely doesn't support this extension)"
            )
        return msg.get_remainder()
