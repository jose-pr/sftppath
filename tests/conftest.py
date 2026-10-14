import sys
import tempfile
import threading

import pytest

import hermetic
import sftp_support
from server_loops import stop_server
from hermetic import NetworkAccessBlocked  # noqa: F401  (re-exported for tests)


@pytest.fixture
def fixture_tree(tmp_path):
    """A small fixture tree for glob/walk/contract tests:

    root/
      a.txt
      b.py
      .hidden.txt
      sub/
        c.py
        nested/
          d.py
      empty_dir/

    Built by the shipped `pathlib_next.testing.populate_fixture_tree()`, so
    the in-repo contracts and downstream users share one definition.
    """
    from pathlib_next.testing import populate_fixture_tree

    return populate_fixture_tree(tmp_path)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_network: lift the autouse non-loopback network guard for a test "
        "that deliberately resolves or connects to a real host",
    )
    config.addinivalue_line(
        "markers",
        "allow_program(*names): let a test start these programs; any other "
        "except the interpreter and the host probes raises ProcessSpawnBlocked",
    )
    # The session's environment is set before the first test's snapshot of it
    # is taken, and put back when pytest exits.
    home = tempfile.TemporaryDirectory(prefix="sftppath-home-")
    patch = pytest.MonkeyPatch()
    hermetic.isolate_environment(patch, home.name)
    config.add_cleanup(home.cleanup)
    config.add_cleanup(patch.undo)


_ENV_BEFORE = pytest.StashKey[dict]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    item.stash[_ENV_BEFORE] = hermetic.environment_snapshot()
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    result = yield
    before = item.stash.get(_ENV_BEFORE, None)
    changed = (
        hermetic.changed_variables(before, hermetic.environment_snapshot())
        if before is not None
        else {}
    )
    if changed:
        hermetic.restore_environment(before)
        raise AssertionError(
            "the test left the process environment changed (use monkeypatch): "
            + ", ".join(f"{k}: {old!r} -> {new!r}" for k, (old, new) in changed.items())
        )
    return result


@pytest.fixture(autouse=True)
def _block_non_loopback_network(request, monkeypatch):
    """Hermetic tests: any connect/sendto/name lookup aimed at a host other
    than loopback raises `NetworkAccessBlocked`, a program other than the
    interpreter raises `ProcessSpawnBlocked`, and a blocked attempt fails the
    test at teardown even if the code under test swallowed the error.
    Autouse rather than opt-in, because the failure it prevents (a refactor
    that quietly sends a test to a real SSH server, or a DNS lookup that
    stalls an offline CI runner) is only caught by a guard the test author
    did not have to remember. `@pytest.mark.allow_network` lifts the network
    half for a test that means to use the network."""
    blocked = []
    if not request.node.get_closest_marker("allow_network"):
        hermetic.install_network_guard(monkeypatch, blocked)
    allowed = [
        name
        for marker in request.node.iter_markers("allow_program")
        for name in marker.args
    ]
    hermetic.install_process_guard(monkeypatch, blocked, allowed)
    # Yielded so a test of the guard itself can inspect and clear it.
    yield blocked
    if blocked:
        pytest.fail(f"non-loopback network access attempted: {blocked}")


@pytest.fixture
def unused_tcp_port():
    """A port nobody is listening on, for connection-refused coverage."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def sftp_server(fixture_tree, tmp_path_factory, monkeypatch):
    """In-process SFTP server backed by fixture_tree, using asyncssh's own
    `SFTPServer` (chrooted) -- a full local-filesystem-backed SFTP server
    for free. A CLIENT backend choice (paramiko or asyncssh) is independent
    of which library the test SERVER uses -- an asyncssh server talks standard
    SFTP to a paramiko client fine. Runs on its own dedicated event
    loop/thread, independent of `AsyncSSHBackend`'s shared bridge loop
    (server and client are logically separate machines in reality; keeping
    them on separate loops here mirrors that instead of coupling test
    infrastructure to backend-internal state).

    Both client backends verify host keys by default, so for the test's
    duration the home directory is a fresh one whose `.ssh/known_hosts`
    trusts exactly this server's per-test key (and which has no
    `.ssh/config`): clients connect with verification ON, and nothing from
    the developer's real `~/.ssh` leaks in.

    The value is the server's `SFTPServerAddress`: `host`, `port`, `user`,
    `password`, `options()` (the keywords of an `SFTPPath` for it) and `url()`."""
    asyncssh = pytest.importorskip("asyncssh")
    import asyncio

    from sftp_loopback import SFTPServerAddress

    root = str(fixture_tree)
    host_key = asyncssh.generate_private_key("ssh-rsa")
    home = tmp_path_factory.mktemp("sftp-home")
    (home / ".ssh").mkdir()
    # Path.home()/expanduser read USERPROFILE on Windows, HOME elsewhere.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    live = set()

    class _NoAuth(asyncssh.SSHServer):
        def connection_made(self, conn):
            self._conn = conn
            live.add(conn)

        def connection_lost(self, exc):
            live.discard(self._conn)

        def begin_auth(self, username):
            return False

    def _sftp_factory(chan):
        return asyncssh.SFTPServer(chan, chroot=root)

    async def _start():
        return await asyncssh.listen(
            "127.0.0.1",
            0,
            server_factory=_NoAuth,
            server_host_keys=[host_key],
            sftp_factory=_sftp_factory,
            process_factory=None,
            # No GSS: its default asks the resolver for this machine's own name.
            gss_host=None,
        )

    # SelectorEventLoop on Windows, not the WindowsProactorEventLoopPolicy
    # default -- avoids a benign-but-noisy Proactor pipe-transport __del__
    # warning (the real backend's bridge loop does the same); we don't need
    # subprocess pipes here.
    loop = (
        asyncio.SelectorEventLoop()
        if sys.platform == "win32"
        else asyncio.new_event_loop()
    )
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    server = asyncio.run_coroutine_threadsafe(_start(), loop).result()
    port = server.sockets[0].getsockname()[1]
    public_key = host_key.export_public_key("openssh").decode().split()
    (home / ".ssh" / "known_hosts").write_text(
        f"[127.0.0.1]:{port} {public_key[0]} {public_key[1]}\n"
    )
    try:
        # A password is included even though the server's begin_auth()
        # accepts everything unconditionally: paramiko's SSHClient.connect()
        # only sends an auth attempt at all if at least one of
        # key/keyfiles/password is available -- with allow_agent=False,
        # look_for_keys=False and no password, it raises "No authentication
        # methods available" client-side before ever reaching the server.
        yield SFTPServerAddress("127.0.0.1", port)
    finally:
        # asyncio and asyncssh objects are not thread-safe, so the server and
        # its connections are closed on the loop's own thread. A connection a
        # cached client left open is aborted, not waited for.
        stop_server(loop, thread, server, live)


@pytest.fixture
def sftp_root(tmp_path):
    """An empty directory for a counting loopback SFTP server to serve."""
    root = tmp_path / "root"
    root.mkdir()
    return root


@pytest.fixture
def sftp_wire():
    """The counters and hooks of a counting loopback SFTP server (`Wire` in
    `tests/sftp_loopback.py`): per-method counts and a request log, a hook that
    fails or reshapes any request, rows a listing also holds."""
    pytest.importorskip("asyncssh")
    from sftp_loopback import Wire

    return Wire()


@pytest.fixture
def sftp_loopbacks():
    """`sftp_loopbacks(root, wire=None, **options)` starts a counting SFTP
    server on 127.0.0.1 serving `root` and returns it; every one started is
    stopped when the test ends. `options` are those of `Loopback`: `password`,
    `sftp_version` (4 for a v4 session), `server_class`, `escaped_names`."""
    pytest.importorskip("asyncssh")
    from sftp_loopback import Loopback

    started = []

    def start(root, wire=None, **options):
        server = Loopback(root, wire, **options).start()
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


@pytest.fixture
def sftp_loopback(sftp_root, sftp_wire, sftp_loopbacks):
    """A counting SFTP server serving `sftp_root`, counted into `sftp_wire`."""
    return sftp_loopbacks(sftp_root, sftp_wire)


@pytest.fixture(params=sftp_support.KINDS)
def kind(request):
    """The SSH library of the backend under test."""
    pytest.importorskip(request.param)
    return request.param


@pytest.fixture(params=sftp_support.FLAVORS)
def flavor(request):
    """The class of the path under test: an `SFTPPath` or an `SFTPURIPath`."""
    if request.param == "uri":
        pytest.importorskip("uripath")
        pytest.importorskip("sftppath.uri")
    return request.param


@pytest.fixture
def backends():
    """The backends a test opened, closed when it ends."""
    opened = []
    yield opened
    for backend in opened:
        backend.close()
