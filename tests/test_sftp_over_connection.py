"""`AsyncSSHBackend.over_connection()`: SFTP over an asyncssh connection the
caller opened, on the event loop that owns it, against a counting loopback
server. Assertions are on what the server saw (connections, logins, requests)
and on what the caller's connection and loop are left in.
"""

from __future__ import annotations

import asyncio
import copy
import pickle
import sys
import threading

import pytest

# Before the helpers: the loopback server module imports asyncssh.
asyncssh = pytest.importorskip("asyncssh")

from server_loops import stop_server  # noqa: E402
from sftp_loopback import ANY_PASSWORD  # noqa: E402
from sftp_support import make_path  # noqa: E402
from waits import wait_until  # noqa: E402

from sftppath import AsyncSSHBackend, SFTPPath  # noqa: E402
from sftppath._asyncssh import _cache, _loop  # noqa: E402

_CALLER_THREAD = "caller-asyncssh-loop"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """An empty home: nothing of the developer's `~/.ssh` is read."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def server(sftp_root, sftp_loopbacks):
    """A login that accepts any password; `server.logins` lists every login."""
    (sftp_root / "hello.txt").write_text("hello")
    return sftp_loopbacks(sftp_root, password=ANY_PASSWORD)


class RunningLoop:
    """An event loop on a thread of its own, as an application that drives
    asyncssh runs one."""

    def __init__(self):
        self.loop = (
            asyncio.SelectorEventLoop()
            if sys.platform == "win32"
            else asyncio.new_event_loop()
        )
        self.thread = threading.Thread(
            target=self.loop.run_forever, name=_CALLER_THREAD, daemon=True
        )
        self.thread.start()
        self.connections = []

    def call(self, coro, timeout=15):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def connect(self, server):
        async def _connect():
            return await asyncssh.connect(
                server.host,
                server.port,
                username="x",
                password="x",
                known_hosts=None,
                config=None,
                gss_host=None,
            )

        connection = self.call(_connect())
        self.connections.append(connection)
        return connection

    def close_connection(self, connection):
        async def _close():
            connection.close()
            await connection.wait_closed()

        self.call(_close())

    def stop(self):
        if self.loop.is_closed():
            return
        if not self.thread.is_alive():
            # Stopped by the test: run it again so that what it holds is closed.
            self.thread = threading.Thread(
                target=self.loop.run_forever, name=_CALLER_THREAD, daemon=True
            )
            self.thread.start()
        stop_server(self.loop, self.thread, None, self.connections)


@pytest.fixture
def caller():
    running = RunningLoop()
    yield running
    running.stop()


@pytest.fixture
def connection(caller, server):
    return caller.connect(server)


@pytest.fixture
def over(caller, connection, backends):
    """`over(**keywords)`: a backend over the caller's connection, closed when
    the test ends."""

    def build(conn=None, **keywords):
        backend = AsyncSSHBackend.over_connection(
            connection if conn is None else conn, caller.loop, **keywords
        )
        backends.append(backend)
        return backend

    return build


@pytest.fixture
def no_bridge_loop(monkeypatch):
    """Fails any request sent to this package's own loop thread."""

    def refuse():
        raise AssertionError("a request went to the bridge loop")

    monkeypatch.setattr(_loop, "_ensure_loop", refuse)


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


# --- paths over the caller's connection ------------------------------------------


def test_paths_run_over_the_callers_connection_on_its_loop(
    server, sftp_root, over, no_bridge_loop
):
    backend = over()
    root = SFTPPath("/", backend=backend)

    assert (root / "hello.txt").read_text() == "hello"
    (root / "d" / "e").mkdir(parents=True)
    (root / "d" / "e" / "f.txt").write_bytes(b"payload")
    with (root / "d" / "e" / "f.txt").open("rb") as stream:
        assert stream.read(3) == b"pay"
    assert sorted(child.name for child in root.iterdir()) == ["d", "hello.txt"]
    (root / "d").copy(root / "copied", recursive=True)
    assert (sftp_root / "copied" / "e" / "f.txt").read_bytes() == b"payload"
    (root / "hello.txt").rename(root / "renamed.txt")
    (root / "copied").rm(recursive=True)

    assert not (sftp_root / "copied").exists()
    assert (sftp_root / "renamed.txt").read_text() == "hello"
    # One connection and one login, both the caller's.
    assert server.connections == 1
    assert len(server.logins) == 1


def test_a_path_on_a_backend_over_a_connection_is_a_path_of_either_flavor(
    flavor, server, over, no_bridge_loop
):
    path = make_path(flavor, server, over(), "hello.txt")

    assert path.read_text() == "hello"
    assert path.with_name("other.txt").exists() is False
    assert server.connections == 1
    assert len(server.logins) == 1


def test_requests_run_on_the_callers_loop_thread(server, over, monkeypatch):
    backend = over()
    seen = []
    original = asyncssh.SFTPClient.stat

    async def stat(self, *args, **kwargs):
        seen.append(threading.current_thread().name)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(asyncssh.SFTPClient, "stat", stat)
    SFTPPath("/hello.txt", backend=backend).stat()

    assert seen == [_CALLER_THREAD]


def test_two_backends_share_one_connection(caller, connection, server, over):
    first, second = over(), over()

    assert SFTPPath("/hello.txt", backend=first).read_text() == "hello"
    SFTPPath("/two.txt", backend=second).write_text("two")
    assert SFTPPath("/two.txt", backend=first).read_text() == "two"

    first.close()
    assert not connection.is_closed()
    assert SFTPPath("/hello.txt", backend=second).read_text() == "hello"
    assert server.connections == 1
    assert len(server.logins) == 1


# --- lifetime: the connection stays the caller's ---------------------------------


def test_close_closes_the_channel_and_leaves_the_connection_open(
    connection, server, over
):
    backend = over()
    path = SFTPPath("/hello.txt", backend=backend)
    assert path.read_text() == "hello"
    channel = backend.client()._aclient

    with backend:
        pass

    assert wait_until(lambda: getattr(channel._handler, "_writer", None) is None)
    assert not connection.is_closed()
    assert not [key for key in _cache._CACHE._entries if key[0] is backend]
    # The next request opens another channel on the same connection.
    assert path.read_text() == "hello"
    assert backend.client()._aclient is not channel
    assert server.connections == 1
    assert len(server.logins) == 1


def test_a_connection_the_caller_closed_raises_connection_reset(
    caller, connection, server, over
):
    backend = over()
    path = SFTPPath("/hello.txt", backend=backend)
    assert path.read_text() == "hello"
    stream = path.open("rb", buffering=0)

    caller.close_connection(connection)

    for call in (path.read_text, path.stat, stream.read):
        assert isinstance(_outcome(call), ConnectionResetError)
    assert server.connections == 1
    stream.close()


def test_a_connection_closed_before_the_first_request_raises_connection_reset(
    caller, connection, over
):
    backend = over()
    caller.close_connection(connection)

    error = _outcome(SFTPPath("/hello.txt", backend=backend).read_text)

    assert isinstance(error, ConnectionResetError)
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


def test_a_connection_closed_mid_request_raises_connection_reset(
    caller, connection, server, over
):
    backend = over()
    path = SFTPPath("/hello.txt", backend=backend)
    assert path.read_text() == "hello"

    def close_first(sftp_server, *args):
        # Runs on the server's loop: the client connection is closed before
        # the request is answered.
        caller.close_connection(connection)

    server.wire.before["stat"] = close_first
    error = _outcome(path.stat)

    assert isinstance(error, ConnectionResetError)


def test_the_backend_never_closes_the_callers_connection(connection, server, over):
    backend = over()
    assert SFTPPath("/hello.txt", backend=backend).read_text() == "hello"

    backend.close()
    backend.close()
    _cache._CACHE.invalidate((backend,))

    assert not connection.is_closed()
    assert len(server.live) == 1


# --- the loop: timeouts, cancellation, re-entry, a loop that stops ---------------


def test_a_request_is_bounded_by_the_timeout_and_cancelled(server, over):
    backend = over(timeout=0.3)
    path = SFTPPath("/hello.txt", backend=backend)
    assert path.read_text() == "hello"
    release = threading.Event()

    def stall(sftp_server, *args):
        release.wait(5)

    server.wire.before["lstat"] = stall
    server.wire.before["stat"] = stall
    try:
        error = _outcome(path.stat)
    finally:
        release.set()
    server.wire.before.clear()

    assert type(error) is TimeoutError
    assert path.read_text() == "hello"


def test_run_on_a_callers_loop_cancels_on_timeout(caller):
    cancelled = threading.Event()

    async def stalled():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(TimeoutError) as excinfo:
        _loop._run(stalled(), 0.2, _loop._CallerLoop(caller.loop))

    assert type(excinfo.value) is TimeoutError
    assert cancelled.is_set()


def test_a_sync_call_on_the_callers_loop_thread_raises(caller, over):
    backend = over()

    async def reenter():
        try:
            SFTPPath("/hello.txt", backend=backend).stat()
        except RuntimeError as error:
            return error
        return None

    assert isinstance(caller.call(reenter()), RuntimeError)


def test_a_request_on_a_stopped_loop_raises_instead_of_hanging(caller, over):
    backend = over()
    path = SFTPPath("/hello.txt", backend=backend)
    assert path.read_text() == "hello"

    caller.loop.call_soon_threadsafe(caller.loop.stop)
    caller.thread.join(10)

    error = _outcome(path.read_text, deadline=30)
    assert isinstance(error, RuntimeError)
    assert "not running" in str(error)


def test_a_wait_on_a_loop_that_stops_raises_instead_of_hanging(caller):
    started = threading.Event()

    async def forever():
        started.set()
        await asyncio.sleep(3600)

    target = _loop._CallerLoop(caller.loop)

    def stop_when_started():
        started.wait(10)
        caller.loop.call_soon_threadsafe(caller.loop.stop)

    threading.Thread(target=stop_when_started, daemon=True).start()
    error = _outcome(lambda: _loop._run(forever(), None, target), deadline=30)

    assert isinstance(error, RuntimeError)
    assert "not running" in str(error)


def test_a_backend_over_a_connection_is_refused_after_fork(over, monkeypatch):
    backend = over()
    assert SFTPPath("/hello.txt", backend=backend).read_text() == "hello"

    monkeypatch.setattr(_loop._os, "getpid", lambda: backend._caller.pid + 1)

    with pytest.raises(RuntimeError, match="fork"):
        SFTPPath("/hello.txt", backend=backend).read_text()


# --- building one ----------------------------------------------------------------


def test_the_endpoint_comes_from_the_connection(server, over):
    backend = over()

    assert backend.endpoint == (server.host, server.port, "x")
    assert repr(SFTPPath("/a", backend=backend)) == (
        f"SFTPPath('/a', host='{server.host}', port={server.port}, user='x')"
    )
    assert SFTPPath("/a", backend=backend).as_uri() == (
        f"sftp://x@{server.host}:{server.port}/a"
    )


def test_host_names_the_server(server, over):
    backend = over(host="files.example", max_concurrency=3, sftp_version=3)

    assert backend.endpoint == ("files.example", server.port, "x")
    assert backend.max_concurrency == 3
    assert SFTPPath("/hello.txt", backend=backend).read_text() == "hello"


def test_a_connection_without_a_peer_address_needs_host(caller, connection):
    # A real connection that reports no peer, as one through a tunnel may.
    original = connection.get_extra_info

    def without_peer(name, default=None):
        return None if name == "peername" else original(name, default)

    connection.get_extra_info = without_peer
    try:
        with pytest.raises(ValueError, match="host="):
            AsyncSSHBackend.over_connection(connection, caller.loop)
        backend = AsyncSSHBackend.over_connection(
            connection, caller.loop, host="h.example"
        )
    finally:
        del connection.get_extra_info
    assert backend.endpoint == ("h.example", None, "x")


def test_arguments_are_checked(caller, connection):
    with pytest.raises(TypeError, match="SSHClientConnection"):
        AsyncSSHBackend.over_connection(object(), caller.loop)
    with pytest.raises(TypeError, match="event loop"):
        AsyncSSHBackend.over_connection(connection, object())
    other = asyncio.new_event_loop()
    try:
        with pytest.raises(ValueError, match="another event loop"):
            AsyncSSHBackend.over_connection(connection, other)
    finally:
        other.close()


def test_building_one_sends_nothing(server, over):
    server.wire.reset()
    over()

    assert server.wire.total() == 0


def test_a_backend_over_a_connection_does_not_pickle(over):
    backend = over()
    path = SFTPPath("/a", backend=backend)

    with pytest.raises(TypeError, match="pickled"):
        pickle.dumps(backend)
    with pytest.raises(TypeError, match="pickled"):
        pickle.dumps(path)
    assert copy.deepcopy(path).backend is backend
