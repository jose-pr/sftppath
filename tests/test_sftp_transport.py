"""SFTP connection behaviour, against a loopback asyncssh server (never an
external host): host keys verified by default with an explicit in-code opt-out,
bounded paramiko connect timeouts, the asyncssh `_run()` request timeouts and
bridge-loop re-entry, dead, evicted and failed connections closed and replaced,
the asyncssh connection cache under concurrent first calls, open-handle capping
during a recursive copy, the `ssh_config` a backend reads when it connects
(`Include`, `ProxyJump`, `ProxyCommand`), and `providers.LRU`'s eviction
callback. Assertions are on what the server saw (credentials, live
connections) or what the backend received.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from types import SimpleNamespace

import pytest

# Before the helpers: the loopback server module imports asyncssh.
asyncssh = pytest.importorskip("asyncssh")

from child import run  # noqa: E402
from sftp_loopback import ANY_PASSWORD  # noqa: E402
from sftp_support import make_path  # noqa: E402
from waits import wait_until  # noqa: E402

from pathapi import providers  # noqa: E402
from sftppath import (  # noqa: E402
    DEFAULT_SSH_CONFIG,
    AsyncSSHBackend,
    BaseSFTPBackend,
    SFTPAuthenticationError,
    SFTPHostKeyError,
    SFTPPath,
)
from sftppath._asyncssh import _cache, _client, _connect  # noqa: E402
from sftppath._asyncssh import _file, _loop, _walk  # noqa: E402

try:
    import paramiko

    from sftppath import SFTPBackend
except ImportError:  # asyncssh-only install
    paramiko = None
    SFTPBackend = None

needs_paramiko = pytest.mark.skipif(paramiko is None, reason="paramiko not installed")

_LOOP_THREAD_NAME = "sftppath-asyncssh-loop"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """An empty home: nothing of the developer's `~/.ssh` is read."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def server_root(sftp_root):
    (sftp_root / "hello.txt").write_text("hello")
    return sftp_root


@pytest.fixture
def server(server_root, sftp_loopbacks):
    """A login that accepts any password; `server.logins` lists the ones
    offered."""
    return sftp_loopbacks(server_root, password=ANY_PASSWORD)


def _asyncssh_backend(backends, server, **kwargs):
    kwargs.setdefault("user", "x")
    kwargs.setdefault("password", "x")
    backend = AsyncSSHBackend(server.host, server.port, **kwargs)
    backends.append(backend)
    return backend


def _paramiko_backend(backends, server, *, connect_opts=None, **kwargs):
    opts = {"allow_agent": False, "look_for_keys": False}
    opts.update(connect_opts or {})
    kwargs.setdefault("user", "x")
    kwargs.setdefault("password", "x")
    backend = SFTPBackend(server.host, server.port, connect_opts=opts, **kwargs)
    backends.append(backend)
    return backend


def _trusting_paramiko_backend(backends, server, **kwargs):
    """A paramiko backend that accepts any server key."""
    return _paramiko_backend(
        backends,
        server,
        hostkeypolicy=paramiko.AutoAddPolicy(),
        known_hosts=None,
        **kwargs,
    )


def _read(backend):
    return SFTPPath("/hello.txt", backend=backend).read_text()


def _outcome(call, deadline=60.0):
    """What `call()` raised on a thread of its own, or None when it returned.
    The deadline only keeps a call that never ends from holding the run."""
    outcome = []

    def work():
        try:
            call()
        except BaseException as error:
            outcome.append(error)
        else:
            outcome.append(None)

    worker = threading.Thread(target=work, name="sftppath-test-call", daemon=True)
    worker.start()
    worker.join(deadline)
    assert not worker.is_alive(), "the call did not end"
    return outcome[0]


# --- host keys: verified by default, explicit opt-out ---------------------------


def test_asyncssh_rejects_unknown_host_key_by_default(flavor, server, backends):
    backend = _asyncssh_backend(backends, server)
    with pytest.raises(SFTPHostKeyError):
        make_path(flavor, server, backend, "hello.txt").read_text()
    # Rejected before authentication: the password never reached the server.
    assert server.logins == []


def test_asyncssh_accepts_unknown_host_key_with_explicit_opt_out(server, backends):
    backend = _asyncssh_backend(backends, server, connect_opts={"known_hosts": None})
    assert _read(backend) == "hello"
    assert server.logins == [("x", "x")]


def test_asyncssh_verifies_against_user_known_hosts(server, home, backends):
    (home / ".ssh" / "known_hosts").write_text(server.known_hosts_line())
    backend = _asyncssh_backend(backends, server)
    assert _read(backend) == "hello"


@needs_paramiko
def test_paramiko_rejects_unknown_host_key_by_default(flavor, server, backends):
    backend = _paramiko_backend(backends, server)
    assert isinstance(backend.hostkeypolicy, paramiko.RejectPolicy)
    with pytest.raises(SFTPHostKeyError):
        make_path(flavor, server, backend, "hello.txt").read_text()
    assert server.logins == []
    # The refused connection is closed, not left to a live Transport thread.
    assert wait_until(lambda: not server.live)


@needs_paramiko
def test_paramiko_default_backend_rejects_unknown_host_keys():
    from sftppath import _paramiko

    backend = SFTPBackend("host")
    assert isinstance(backend.hostkeypolicy, paramiko.RejectPolicy)
    assert backend.known_hosts is _paramiko._DEFAULT_KNOWN_HOSTS


@needs_paramiko
def test_paramiko_accepts_unknown_host_key_with_explicit_opt_out(
    server, home, backends
):
    backend = _trusting_paramiko_backend(backends, server)
    assert _read(backend) == "hello"
    assert server.logins == [("x", "x")]
    # Host keys are loaded read-only: nothing was written to the user's files.
    assert not (home / ".ssh" / "known_hosts").exists()


@needs_paramiko
def test_paramiko_changed_host_key_is_rejected_even_with_autoadd(
    server, home, backends
):
    other_key = asyncssh.generate_private_key("ssh-rsa")
    (home / ".ssh" / "known_hosts").write_text(server.known_hosts_line(other_key))
    backend = _paramiko_backend(
        backends, server, hostkeypolicy=paramiko.AutoAddPolicy()
    )
    with pytest.raises(SFTPHostKeyError):
        _read(backend)
    assert server.logins == []


@needs_paramiko
def test_paramiko_trusts_ssh_config_user_known_hosts_file(server, home, backends):
    (home / "pinned_hosts").write_text(server.known_hosts_line())
    config = home / ".ssh" / "config"
    config.write_text("Host 127.0.0.1\n  UserKnownHostsFile ~/pinned_hosts\n")
    backend = _paramiko_backend(backends, server, ssh_config=str(config))
    assert _read(backend) == "hello"


def _unrelated_hosts_file(path):
    """A known_hosts file that names a host this test never connects to."""
    algorithm, blob = (
        asyncssh.generate_private_key("ssh-rsa")
        .export_public_key("openssh")
        .decode()
        .split()[:2]
    )
    path.write_text(f"unrelated.invalid {algorithm} {blob}\n")
    return path


@needs_paramiko
@pytest.mark.parametrize("sequence", [list, tuple], ids=["list", "tuple"])
def test_paramiko_reads_every_known_hosts_file_of_a_sequence(
    server, tmp_path, backends, sequence
):
    first = _unrelated_hosts_file(tmp_path / "first_hosts")
    second = tmp_path / "second_hosts"
    second.write_text(server.known_hosts_line())
    backend = _paramiko_backend(backends, server, known_hosts=sequence([first, second]))

    assert _read(backend) == "hello"


@needs_paramiko
def test_paramiko_rejects_a_host_that_no_known_hosts_file_of_a_list_names(
    server, tmp_path, backends
):
    files = [_unrelated_hosts_file(tmp_path / f"hosts{n}") for n in range(2)]
    backend = _paramiko_backend(backends, server, known_hosts=files)

    with pytest.raises(SFTPHostKeyError):
        _read(backend)

    assert server.logins == []


# --- timeouts --------------------------------------------------------------------


@needs_paramiko
def test_paramiko_connect_timeouts_default_to_30s_and_are_overridable():
    opts = SFTPBackend("host", 22, ssh_config=None).opts()
    for key in ("timeout", "banner_timeout", "auth_timeout", "channel_timeout"):
        assert opts[key] == 30.0
    opts = SFTPBackend(
        "host", 22, connect_opts={"banner_timeout": 7}, ssh_config=None, timeout=3
    ).opts()
    assert opts["banner_timeout"] == 7
    assert opts["timeout"] == opts["auth_timeout"] == 3
    opts = SFTPBackend("host", 22, ssh_config=None, timeout=None).opts()
    assert "timeout" not in opts and "banner_timeout" not in opts


@needs_paramiko
def test_paramiko_connect_to_silent_server_times_out(backends):
    # Accepts the TCP connection and never sends an SSH banner: without a
    # bound, connect() blocks forever.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        silent = SimpleNamespace(host="127.0.0.1", port=listener.getsockname()[1])
        backend = _trusting_paramiko_backend(backends, silent, timeout=0.5)

        # A banner that never arrives ends the connect one of two ways,
        # whichever of paramiko's own timers fires first: the timeout itself,
        # or the session it gave up on.
        error = _outcome(backend.client)

    assert isinstance(error, (TimeoutError, ConnectionAbortedError))


def test_run_timeout_cancels_request_and_raises_builtin_timeouterror():
    cancelled = threading.Event()

    async def _stalled():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(TimeoutError) as excinfo:
        _loop._run(_stalled(), 0.2)
    # The builtin, on every Python version (concurrent.futures.TimeoutError
    # is a different class before 3.11).
    assert type(excinfo.value) is TimeoutError
    # The request was cancelled on the loop, not left running.
    assert cancelled.is_set()


def test_run_on_bridge_loop_thread_raises_instead_of_deadlocking():
    async def _reenter():
        try:
            _loop._run(asyncio.sleep(0), 30)
        except RuntimeError as error:
            return error, threading.current_thread().name
        return None, None

    error, thread_name = _loop._run(_reenter(), 10)
    assert isinstance(error, RuntimeError)
    assert thread_name == _LOOP_THREAD_NAME


class _SlowAsyncFile:
    def __init__(self, delay):
        self.delay = delay

    async def read(self, size=-1):
        await asyncio.sleep(self.delay)
        return b"payload"

    async def write(self, data):
        await asyncio.sleep(self.delay)
        return len(data)

    async def seek(self, offset, whence=0):
        await asyncio.sleep(self.delay)
        return offset

    async def close(self):
        pass


def test_streaming_reads_and_writes_are_not_bounded_by_request_timeout():
    # Each request takes half a second, the request bound is a tenth of one.
    sync_file = _file._SyncSFTPFile(_SlowAsyncFile(0.5), timeout=0.1)
    assert sync_file.read() == b"payload"
    assert sync_file.write(b"abc") == 3
    # A single request on the same file is bounded.
    with pytest.raises(TimeoutError):
        sync_file.seek(0)
    sync_file.close()


def test_single_client_request_is_bounded_by_backend_timeout():
    class _SlowClient:
        async def stat(self, path):
            await asyncio.sleep(1)

    client = _client._SyncSFTPClient(_SlowClient(), timeout=0.1)
    with pytest.raises(TimeoutError):
        client.stat("/x")


def test_asyncssh_backend_timeout_reaches_the_sync_client(server, backends):
    backend = _asyncssh_backend(
        backends, server, connect_opts={"known_hosts": None}, timeout=7
    )
    assert backend.client()._timeout == 7


# --- asyncssh recursive rm/copy: connection resolved off the loop ---------------


def test_recursive_rm_as_first_call_on_a_fresh_asyncssh_backend(
    flavor, server, server_root, backends
):
    tree = server_root / "tree"
    (tree / "sub").mkdir(parents=True)
    for name in ("a.txt", "sub/b.txt", "sub/c.txt"):
        (tree / name).write_text(name)
    backend = _asyncssh_backend(backends, server, connect_opts={"known_hosts": None})
    make_path(flavor, server, backend, "tree").rm(recursive=True)
    assert not tree.exists()


def test_recursive_copy_as_first_call_on_a_fresh_asyncssh_backend(
    flavor, server, server_root, backends
):
    (server_root / "src" / "sub").mkdir(parents=True)
    (server_root / "src" / "sub" / "x.txt").write_text("x")
    backend = _asyncssh_backend(backends, server, connect_opts={"known_hosts": None})
    make_path(flavor, server, backend, "src").copy(
        make_path(flavor, server, backend, "dst"), recursive=True
    )
    assert (server_root / "dst" / "sub" / "x.txt").read_text() == "x"


def test_tree_copy_as_first_request_on_a_fresh_asyncssh_backend(
    server, server_root, backends
):
    # `copy()` asks about both paths before the walk, so the walk is the first
    # use of the connection only when the backend is asked for it directly.
    (server_root / "src" / "sub").mkdir(parents=True)
    (server_root / "src" / "sub" / "x.txt").write_text("x")
    (server_root / "dst").mkdir()
    backend = _asyncssh_backend(backends, server, connect_opts={"known_hosts": None})
    assert server.connections == 0

    backend.tree_copy(
        SFTPPath("/src", backend=backend),
        SFTPPath("/dst", backend=backend),
        overwrite=False,
        follow_symlinks=True,
        preserve_metadata=False,
        ignore_error=None,
    )

    assert (server_root / "dst" / "sub" / "x.txt").read_text() == "x"


def test_rm_ignore_error_callback_may_call_sync_path_methods(
    flavor, server_root, sftp_loopbacks, backends
):
    from asyncssh import SFTPPermissionDenied, SFTPServer

    class _LockedRemoveServer(SFTPServer):
        def remove(self, path):
            if path.endswith(b"locked.txt"):
                raise SFTPPermissionDenied("locked")
            return super().remove(path)

    tree = server_root / "tree"
    tree.mkdir()
    (tree / "locked.txt").write_text("keep")
    (tree / "ok.txt").write_text("go")
    srv = sftp_loopbacks(
        server_root, password=ANY_PASSWORD, server_class=_LockedRemoveServer
    )
    backend = _asyncssh_backend(backends, srv, connect_opts={"known_hosts": None})
    seen = []

    def _ignore_if_gone(error, path):
        # A sync Path method: it must not run on the bridge loop, where it
        # would block the loop its own request needs.
        seen.append((path.name, path.exists(), threading.current_thread().name))
        return True

    make_path(flavor, srv, backend, "tree").rm(
        recursive=True, ignore_error=_ignore_if_gone
    )
    assert (tree / "locked.txt").exists()
    assert not (tree / "ok.txt").exists()
    assert ("locked.txt", True) in [(name, exists) for name, exists, _ in seen]
    assert all(thread != _LOOP_THREAD_NAME for _, _, thread in seen)


class _HandleCountingFile:
    def __init__(self, client, path, mode):
        self.client, self.path, self.mode = client, path, mode
        self.done = False

    async def read(self, size=-1):
        await asyncio.sleep(0)
        if self.done:
            return b""
        self.done = True
        return self.client.files[self.path]

    async def write(self, data):
        await asyncio.sleep(0)
        self.client.files[self.path] = data

    async def close(self):
        self.client.open_now -= 1


class _HandleCountingClient:
    def __init__(self, count):
        self.files = {f"/src/f{i}.txt": b"x" for i in range(count)}
        self.open_now = 0
        self.peak = 0

    async def stat(self, path):
        await asyncio.sleep(0)
        if path == "/src":
            return asyncssh.SFTPAttrs(permissions=0o40755)
        if path in self.files:
            return asyncssh.SFTPAttrs(permissions=0o100644)
        raise asyncssh.SFTPNoSuchFile(path)

    lstat = stat

    async def readdir(self, path):
        names = [p.rsplit("/", 1)[1] for p in self.files if p.startswith("/src/")]
        return [SimpleNamespace(filename=name) for name in names]

    async def open(self, path, mode, encoding=None):
        await asyncio.sleep(0)
        self.open_now += 1
        self.peak = max(self.peak, self.open_now)
        return _HandleCountingFile(self, path, mode)

    async def chmod(self, path, mode):
        pass


class _FakePath:
    """What the walks read of a path: `_wirepath`, `name` and `/`."""

    def __init__(self, path):
        self._wirepath = path
        self.name = path.rsplit("/", 1)[1]

    def __truediv__(self, name):
        return _FakePath(f"{self._wirepath}/{name}")

    def __str__(self):
        return self._wirepath


def test_concurrent_copy_caps_open_handles_at_max_concurrency():
    client = _HandleCountingClient(60)
    asyncio.run(
        _walk._concurrent_copy(
            _FakePath("/src"),
            _FakePath("/dst"),
            overwrite=False,
            follow_symlinks=True,
            preserve_metadata=False,
            max_concurrency=4,
            ignore_error=None,
            aclient=client,
        )
    )
    assert sum(1 for path in client.files if path.startswith("/dst/")) == 60
    # Two handles (source and destination) per file being copied: a queued
    # file holds none until it has a slot.
    assert client.peak <= 2 * 4
    assert client.open_now == 0


# --- dead, evicted and failed connections ---------------------------------------


@needs_paramiko
def test_paramiko_reconnects_after_the_server_drops_the_connection(server, backends):
    backend = _trusting_paramiko_backend(backends, server)
    path = SFTPPath("/hello.txt", backend=backend)
    assert path.read_text() == "hello"
    old_client = backend.client()
    old_transport = old_client.sock.get_transport()

    server.drop_connections()
    assert wait_until(lambda: not old_transport.is_active())

    assert path.read_text() == "hello"
    assert path.exists()
    assert backend.client() is not old_client


def test_asyncssh_reconnects_after_the_sftp_channel_closes(server, backends):
    backend = _asyncssh_backend(backends, server, connect_opts={"known_hosts": None})
    path = SFTPPath("/hello.txt", backend=backend)
    assert path.read_text() == "hello"
    old_entry = _cache._CACHE._entries[(backend,)]

    async def _close_channel():
        old_entry.client._aclient.exit()
        await old_entry.client._aclient.wait_closed()

    _loop._run(_close_channel(), 10)
    # Only the SFTP channel is gone; the SSH connection is still up.
    assert not old_entry.conn.is_closed()

    assert path.read_text() == "hello"
    assert backend.client() is not old_entry.client
    # The stale connection was closed, not orphaned.
    assert wait_until(lambda: old_entry.conn.is_closed())
    assert wait_until(lambda: len(server.live) == 1)


@needs_paramiko
def test_paramiko_evicted_connections_are_closed(server, backends, monkeypatch):
    """One cache slot, five distinct keys: each new client evicts the
    previous one, whose connection must be closed rather than orphaned.

    The keys come from five backends, not five threads: `threading` reuses
    a finished thread's `get_ident()` value on POSIX, so sequential threads
    share one cache key there and only one connection is ever opened.
    """
    from sftppath._paramiko import _CACHED_CLIENTS

    monkeypatch.setattr(_CACHED_CLIENTS, "maxsize", 1)
    for _ in range(5):
        backend = _trusting_paramiko_backend(backends, server)
        assert _read(backend) == "hello"

    assert server.connections == 5
    # One cache slot: every evicted client's connection was closed.
    assert wait_until(lambda: len(server.live) <= 1)
    for backend in list(backends):
        backend.close()
    assert wait_until(lambda: not server.live)


@needs_paramiko
def test_paramiko_client_is_reused_by_a_later_thread(server, backends):
    """A cached client outlives the thread that opened it: paramiko's
    transport runs on its own thread, so a later thread reusing the entry
    (the same `get_ident()` value, which POSIX recycles) must get a working
    client, not a dead one."""
    backend = _trusting_paramiko_backend(backends, server)
    path = SFTPPath("/hello.txt", backend=backend)
    read = []
    for _ in range(2):
        worker = threading.Thread(target=lambda: read.append(path.read_text()))
        worker.start()
        worker.join(30)

    assert read == ["hello", "hello"]


@needs_paramiko
def test_paramiko_failed_login_closes_the_connection(
    server_root, sftp_loopbacks, backends
):
    srv = sftp_loopbacks(server_root, password="right")

    def _transports():
        # The transport threads themselves, not a count: another test's
        # connection may still be shutting down, and comparing totals then
        # fails on whichever side wins the race.
        return {t for t in threading.enumerate() if isinstance(t, paramiko.Transport)}

    before = _transports()
    backend = _trusting_paramiko_backend(backends, srv)
    for _ in range(3):
        with pytest.raises(SFTPAuthenticationError):
            _read(backend)
    assert srv.connections == 3
    assert wait_until(lambda: not srv.live)
    # Every transport this test opened is gone; older ones are not ours.
    assert wait_until(lambda: not (_transports() - before), timeout=30)


@needs_paramiko
def test_paramiko_closes_the_connection_when_the_sftp_subsystem_is_refused(
    server_root, sftp_loopbacks, backends
):
    # The server keeps the SSH connection open after refusing the subsystem:
    # only the client closing it ends it.
    srv = sftp_loopbacks(server_root, password=ANY_PASSWORD, sftp=False)
    backend = _trusting_paramiko_backend(backends, srv)

    with pytest.raises(OSError):
        backend.client()

    assert srv.connections == 1
    assert wait_until(lambda: not srv.live)


class _Proxy:
    """Stands for `paramiko.ProxyCommand`, which would start a program."""

    started: "list[_Proxy]" = []

    def __init__(self, command_line):
        self.closed = False
        self.started.append(self)

    def close(self):
        self.closed = True


@pytest.fixture
def refused_connect(monkeypatch):
    """`paramiko.SSHClient.connect` fails after the options are built."""
    _Proxy.started = []
    monkeypatch.setattr(paramiko, "ProxyCommand", _Proxy)

    def refuse(self, **options):
        raise paramiko.SSHException("refused")

    monkeypatch.setattr(paramiko.SSHClient, "connect", refuse)


@needs_paramiko
def test_paramiko_closes_the_proxy_it_started_when_the_connection_fails(
    tmp_path, refused_connect
):
    config = tmp_path / "config"
    config.write_text("Host *\n  ProxyCommand prog %h %p\n")
    backend = SFTPBackend("box", 22, ssh_config=str(config), known_hosts=None)

    with pytest.raises(ConnectionAbortedError):
        backend.transport()

    assert [proxy.closed for proxy in _Proxy.started] == [True]


@needs_paramiko
def test_paramiko_leaves_a_socket_the_caller_supplied_open_when_the_connection_fails(
    refused_connect,
):
    mine = _Proxy("supplied")
    backend = SFTPBackend(
        "box", 22, connect_opts={"sock": mine}, ssh_config=None, known_hosts=None
    )

    with pytest.raises(ConnectionAbortedError):
        backend.transport()

    assert mine.closed is False


def test_asyncssh_aconnect_closes_connection_when_sftp_start_fails(monkeypatch):
    closed = []

    class _FakeConn:
        async def start_sftp_client(self, *, sftp_version, path_errors):
            raise asyncssh.ChannelOpenError(1, "no sftp subsystem")

        def close(self):
            closed.append(True)

    async def _fake_connect(host, **kwargs):
        return _FakeConn()

    monkeypatch.setattr(_connect._asyncssh, "connect", _fake_connect)
    with pytest.raises(asyncssh.ChannelOpenError):
        asyncio.run(_connect._aconnect("host", 22))
    assert closed == [True]


def test_asyncssh_backend_close_closes_its_connections(server, backends):
    backend = _asyncssh_backend(backends, server, connect_opts={"known_hosts": None})
    assert _read(backend) == "hello"
    assert wait_until(lambda: len(server.live) == 1)
    backend.close()
    assert wait_until(lambda: not server.live)
    assert not [key for key in _cache._CACHE._entries if key[0] is backend]
    # Harmless when repeated, and the backend connects again on its next use.
    backend.close()
    assert _read(backend) == "hello"
    assert wait_until(lambda: len(server.live) == 1)


@needs_paramiko
def test_paramiko_backend_close_closes_its_connections(server, backends):
    backend = _trusting_paramiko_backend(backends, server)
    assert _read(backend) == "hello"
    assert wait_until(lambda: len(server.live) == 1)
    backend.close()
    assert wait_until(lambda: not server.live)
    # Harmless when repeated, and the backend connects again on its next use.
    backend.close()
    assert _read(backend) == "hello"
    assert wait_until(lambda: len(server.live) == 1)


def test_asyncssh_concurrent_first_calls_share_one_connection(server, backends):
    backend = _asyncssh_backend(backends, server, connect_opts={"known_hosts": None})
    barrier = threading.Barrier(8)
    clients = []

    def _connect_first():
        barrier.wait()
        clients.append(backend.client())

    workers = [threading.Thread(target=_connect_first) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)

    assert len(clients) == 8
    assert len({id(client) for client in clients}) == 1
    assert server.connections == 1
    assert [key for key in _cache._CACHE._entries if key[0] is backend] == [(backend,)]


# --- paramiko-only install: copy() must not need asyncssh -----------------------


@needs_paramiko
def test_copy_works_without_asyncssh_installed():
    result = run(
        """
        import io, stat, sys
        from pathapi import FileStat
        from pathapi.mempath import MemPath
        from sftppath import BaseSFTPBackend, SFTPPath, _select

        class Client:
            def stat(self, path):
                return FileStat(st_mode=stat.S_IFREG | 0o644, st_size=5)
            lstat = stat
            def open(self, path, mode="r", buffering=-1):
                return io.BytesIO(b"hello")

        class Backend(BaseSFTPBackend):
            def client(self):
                return Client()

        resolved = _select._resolve_default_backend_cls(reload=True)
        assert resolved.__name__ == "SFTPBackend", resolved
        target = MemPath("/local.txt")
        SFTPPath("/remote.txt", backend=Backend()).copy(target)
        assert target.read_bytes() == b"hello", target.read_bytes()
        assert sys.modules["asyncssh"] is None
        print("OK")
        """,
        ["asyncssh"],
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# --- ssh_config: direct construction, Include, ProxyJump -------------------------


class _RecordingBackend(BaseSFTPBackend):
    def __init__(self, host, port=None, *, ssh_config=DEFAULT_SSH_CONFIG, **options):
        self.host = host
        self.ssh_config = ssh_config


class _RecordingPath(SFTPPath):
    _default_backend_cls = _RecordingBackend


def test_direct_construction_keeps_the_ssh_config_argument():
    def built_backend(**options):
        return _RecordingPath("/dir/x.txt", host="host", **options).backend

    assert built_backend().ssh_config is DEFAULT_SSH_CONFIG
    assert built_backend(ssh_config="/cfg").ssh_config == "/cfg"
    assert built_backend(ssh_config=None).ssh_config is None

    # Derived paths carry it, like the backend.
    base = _RecordingPath("/dir/x.txt", host="host", ssh_config="/cfg")
    assert (base / "child").backend is base.backend
    assert base.parent.backend is base.backend
    assert base.backend.ssh_config == "/cfg"


def test_direct_construction_of_a_uri_path_keeps_the_ssh_config_argument():
    pytest.importorskip("uripath")
    from sftppath.uri import SFTPURIPath

    class _RecordingURIPath(SFTPURIPath):
        _default_backend_cls = _RecordingBackend
        __SCHEMES = ()

    def built_backend(path):
        return path._core_path.backend

    url = "sftp://host/dir/x.txt"
    assert built_backend(_RecordingURIPath(url)).ssh_config is DEFAULT_SSH_CONFIG
    assert built_backend(_RecordingURIPath(url, ssh_config="/cfg")).ssh_config == "/cfg"
    assert built_backend(_RecordingURIPath(url, ssh_config=None)).ssh_config is None

    parsed_first = _RecordingURIPath(url, ssh_config="/cfg")
    assert parsed_first.path == "/dir/x.txt"  # the lazy parse runs first
    assert built_backend(parsed_first).ssh_config == "/cfg"

    # Derived paths carry it, like the backend.
    base = _RecordingURIPath(url, ssh_config="/cfg")
    assert (base / "child")._ssh_config == "/cfg"
    assert base.parent._ssh_config == "/cfg"
    assert _RecordingURIPath("sftp://other/y", ssh_config="/cfg")._ssh_config == "/cfg"


@needs_paramiko
def test_paramiko_ssh_config_honours_include(tmp_path, home):
    (home / ".ssh" / "inc.conf").write_text(
        "Host myalias\n  HostName 10.9.8.7\n  Port 2222\n  User deploy\n"
    )
    extra = tmp_path / "conf.d"
    extra.mkdir()
    (extra / "10-jump.conf").write_text("Host viaglob\n  HostName 10.1.2.3\n")
    config = home / ".ssh" / "config"
    config.write_text(
        "Include inc.conf\n"
        f"Include {extra.as_posix()}/*.conf\n"
        "Host scoped\n"
        "  Include scoped.conf\n"
        "  User outer\n"
    )
    # Lines after an Include stay in the block that contains it.
    (home / ".ssh" / "scoped.conf").write_text("Port 2200\nHost unrelated\n  User x\n")

    opts = SFTPBackend("myalias", ssh_config=str(config)).opts()
    assert (opts["hostname"], opts["port"], opts["username"]) == (
        "10.9.8.7",
        2222,
        "deploy",
    )
    assert SFTPBackend("viaglob", ssh_config=str(config)).opts()["hostname"] == (
        "10.1.2.3"
    )
    scoped = SFTPBackend("scoped", ssh_config=str(config)).opts()
    assert (scoped["port"], scoped["username"]) == (2200, "outer")


@needs_paramiko
def test_paramiko_ssh_config_proxyjump_fails_loudly(home):
    config = home / ".ssh" / "config"
    config.write_text("Host jumped\n  HostName 10.1.1.1\n  ProxyJump bastion\n")
    with pytest.raises(NotImplementedError, match="ProxyJump"):
        SFTPBackend("jumped", ssh_config=str(config)).opts()
    # An explicit sock is the caller routing the connection themselves.
    sock = object()
    opts = SFTPBackend(
        "jumped", connect_opts={"sock": sock}, ssh_config=str(config)
    ).opts()
    assert opts["sock"] is sock


# --- providers.LRU eviction callback ----------------------------------------------


def test_lru_on_evict_receives_every_dropped_value():
    evicted = []
    lru = providers.LRU(
        lambda x: [x], maxsize=2, on_evict=lambda k, v: evicted.append((k, v))
    )
    one, two = lru(1), lru(2)
    three = lru(3)  # overflow drops the oldest
    assert evicted == [((1,), one)]
    assert lru.discard(2) is True and lru.discard(2) is False
    assert evicted[-1] == ((2,), two)
    lru(4)
    lru.maxsize = 1  # shrink
    assert evicted[-1] == ((3,), three)
    replaced = lru(4)
    assert lru.invalidate(4) is not replaced
    assert evicted[-1] == ((4,), replaced)


def test_lru_on_evict_errors_do_not_break_lookups():
    def _explode(key, value):
        raise OSError("close failed")

    lru = providers.LRU(lambda x: x * 2, maxsize=1, on_evict=_explode)
    assert lru(1) == 2
    assert lru(2) == 4
    assert lru.invalidate(2) == 4


def test_lru_concurrent_misses_share_the_first_result_and_evict_the_rest():
    barrier = threading.Barrier(4)
    evicted = []
    lru = providers.LRU(
        lambda key: (barrier.wait(), object())[1],
        on_evict=lambda k, v: evicted.append(v),
    )
    results = []
    workers = [
        threading.Thread(target=lambda: results.append(lru("k"))) for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
    assert len(results) == 4 and len({id(r) for r in results}) == 1
    assert len(evicted) == 3 and results[0] not in evicted
