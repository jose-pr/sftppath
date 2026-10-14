"""The asyncssh backend's native recursive copy() and rm(), against a loopback
server that counts every request and can fail or reshape any of them.

What a failed, interrupted or refused operation leaves behind must not depend
on which backend ran it, so the scenarios run on both backends, through an
`SFTPPath` and through an `SFTPURIPath`, and compare what the server holds
afterwards. The asyncssh-only tests pin that a call which has raised has
stopped: the server's request counter does not move after the exception
reached the caller.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import gc
import logging
import os
import threading
import time
from types import SimpleNamespace

import pytest

from sftp_support import make_backend, make_path

asyncssh = pytest.importorskip("asyncssh")

from sftppath import SFTPPath  # noqa: E402
from sftppath._asyncssh import _backend as backend_mod  # noqa: E402
from sftppath._asyncssh import _loop, _walk  # noqa: E402

try:
    from sftppath.uri import SFTPURIPath
except ImportError:  # uripath is not installed
    SFTPURIPath = None

#: Seconds to wait after a call has raised before the server's request counter
#: is read again. A walk that kept running after the exception issues hundreds
#: of requests a second on loopback, so any survivor shows within this time.
_SETTLE = 0.5

#: The asyncssh backend's default bound on requests in flight. A request that
#: was already on the wire when a call raised still reaches the server (and
#: its counter) afterwards, so the counter may move by at most this much.
_IN_FLIGHT = 16


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """An empty home: nothing of the developer's `~/.ssh` is read."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _paths(kind, flavor, server, backends, **backend_kwargs):
    """`at(rel)`: the path `rel` below the root of `server`, of the given
    flavor, over one backend of `kind` that is closed when the test ends."""
    backend = make_backend(kind, server, **backend_kwargs)
    backends.append(backend)

    def at(rel=""):
        return make_path(flavor, server, backend, rel)

    at.backend = backend
    return at


def _body(name):
    return (name.encode() + b"|") * 800


def _make_tree(base, dirs=8, subdirs=3, files=20):
    """`dirs` x `subdirs` x `files` files under `base`, each with content of
    its own; returns {relative posix name: bytes}."""
    content = {}
    for d in range(dirs):
        for s in range(subdirs):
            folder = base / f"d{d}" / f"s{s}"
            folder.mkdir(parents=True)
            for f in range(files):
                name = f"d{d}/s{s}/f{f:02}.bin"
                (base / name).write_bytes(_body(name))
                content[name] = _body(name)
    return content


def _files(base):
    if not base.exists():
        return {}
    return {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in base.rglob("*")
        if path.is_file()
    }


def _entries(base):
    return sum(1 for _ in base.rglob("*")) if base.exists() else 0


def _deny(match, nth=1, *, onward=False, error=asyncssh.SFTPPermissionDenied):
    """A `wire.before` hook failing the `nth` request whose path `match`es
    (and every later one with `onward`)."""
    state = {"seen": 0}

    def hook(server, path, *args):
        if match(path):
            state["seen"] += 1
            if state["seen"] == nth or (onward and state["seen"] > nth):
                raise error("denied by the test")

    return hook


class _Sent:
    """When each request left the asyncssh client."""

    def __init__(self, monkeypatch):
        self.times = []
        send_packet = asyncssh.sftp.SFTPHandler.send_packet

        def recording(handler, *args, **kwargs):
            self.times.append(time.perf_counter())
            return send_packet(handler, *args, **kwargs)

        # The client's handler only: the in-process server's replies go
        # through the same base method.
        monkeypatch.setattr(asyncssh.sftp.SFTPClientHandler, "send_packet", recording)

    def after(self, moment):
        return sum(1 for sent in self.times if sent > moment)


@pytest.fixture
def sent(monkeypatch):
    return _Sent(monkeypatch)


def _walk_tasks():
    """The coroutines of the tree walks that are still alive on the bridge
    loop."""

    async def names():
        found = []
        for task in asyncio.all_tasks():
            name = getattr(task.get_coro(), "__qualname__", "")
            if "_concurrent_" in name:
                found.append(name)
        return found

    return _loop._run(names(), 10)


def _assert_stopped(sftp_wire, sent, raised_at):
    """No walk is left on the bridge loop, nothing was sent to the server after
    `raised_at`, and the server's counter moves by no more than the requests
    that were already in flight. The wait before the counter is read is for a
    walk that did not stop: it would send more within it."""
    assert _walk_tasks() == []
    at_raise = sftp_wire.total()
    time.sleep(_SETTLE)
    assert sent.after(raised_at) == 0
    assert sftp_wire.total() - at_raise <= _IN_FLIGHT


def _interrupt_first_unbounded_wait(monkeypatch, after):
    """Deliver `KeyboardInterrupt` to the first `Future.result()` made without
    a timeout (the wait of a tree operation) once `after` seconds passed: what
    Ctrl-C does to a thread blocked in it."""
    real = concurrent.futures.Future.result
    armed = [True]

    def result(self, timeout=None):
        if timeout is None and armed[0]:
            armed[0] = False
            try:
                return real(self, after)
            except concurrent.futures.TimeoutError:
                raise KeyboardInterrupt from None
        return real(self, timeout)

    monkeypatch.setattr(concurrent.futures.Future, "result", result)


def _slow(seconds):
    """A `wire.before` hook that holds the server's loop for `seconds`: the
    server answers no faster than one request per `seconds`, so a walk of
    hundreds of requests is still running when a bound of a second or two
    passes."""

    def hook(server, *args):
        time.sleep(seconds)

    return hook


# --- a call that has raised has stopped --------------------------------------


def test_copy_that_raised_sends_no_further_request(
    flavor, sftp_loopback, sftp_root, sftp_wire, backends, sent
):
    expected = _make_tree(sftp_root / "src")
    (sftp_root / "dst").mkdir()
    (sftp_root / "dst" / "untouched.txt").write_text("kept")
    at = _paths("asyncssh", flavor, sftp_loopback, backends)
    src = at("src")
    src.exists()
    sftp_wire.before["open"] = _deny(lambda p: b"/src/" in p, nth=40)

    with pytest.raises(PermissionError):
        src.copy(at("dst"), recursive=True, overwrite=True)
    raised_at = time.perf_counter()

    _assert_stopped(sftp_wire, sent, raised_at)
    # Nothing the copy created is left half done, and nothing else is touched.
    copied = _files(sftp_root / "dst")
    assert (sftp_root / "dst" / "untouched.txt").read_text() == "kept"
    copied.pop("untouched.txt")
    assert copied
    assert len(copied) < len(expected)
    for name, content in copied.items():
        assert content == expected[name], name


def test_rm_that_raised_sends_no_further_request(
    flavor, sftp_loopback, sftp_root, sftp_wire, backends, sent
):
    _make_tree(sftp_root / "tree")
    at = _paths("asyncssh", flavor, sftp_loopback, backends)
    tree = at("tree")
    tree.exists()
    denied = _deny(lambda p: p.endswith(b".bin"), nth=20)

    def slow_remove(server, path, *args):
        time.sleep(0.002)
        denied(server, path, *args)

    sftp_wire.before["remove"] = slow_remove

    with pytest.raises(PermissionError):
        tree.rm(recursive=True)
    raised_at = time.perf_counter()

    left = _entries(sftp_root / "tree")
    _assert_stopped(sftp_wire, sent, raised_at)
    assert left - _entries(sftp_root / "tree") <= _IN_FLIGHT


def test_rm_with_a_handler_that_declines_stops_at_the_first_error(
    flavor, sftp_loopback, sftp_root, sftp_wire, backends, sent
):
    _make_tree(sftp_root / "tree")
    at = _paths("asyncssh", flavor, sftp_loopback, backends)
    tree = at("tree")
    tree.exists()
    denied = _deny(lambda p: p.endswith(b".bin"), nth=20)

    def slow_remove(server, path, *args):
        time.sleep(0.002)
        denied(server, path, *args)

    sftp_wire.before["remove"] = slow_remove

    with pytest.raises(PermissionError):
        tree.rm(recursive=True, ignore_error=lambda error, path: False)
    raised_at = time.perf_counter()

    left = _entries(sftp_root / "tree")
    _assert_stopped(sftp_wire, sent, raised_at)
    assert left - _entries(sftp_root / "tree") <= _IN_FLIGHT


def test_walk_that_timed_out_has_stopped(
    sftp_loopback, sftp_root, sftp_wire, backends, sent
):
    _make_tree(sftp_root / "tree")
    total = _entries(sftp_root / "tree")
    backend = make_backend("asyncssh", sftp_loopback)
    backends.append(backend)
    tree = SFTPPath("/tree", backend=backend)
    aclient = tree._sftpclient._aclient
    # 480 file removals, one every 10 ms at the quickest: more than the bound
    # below.
    sftp_wire.before["remove"] = _slow(0.01)

    with pytest.raises(TimeoutError):
        _loop._run(
            _walk._concurrent_rm(
                tree,
                max_concurrency=16,
                missing_ok=False,
                on_error=None,
                aclient=aclient,
            ),
            2.0,
        )
    raised_at = time.perf_counter()

    left = _entries(sftp_root / "tree")
    assert 0 < left <= total
    _assert_stopped(sftp_wire, sent, raised_at)
    assert left - _entries(sftp_root / "tree") <= _IN_FLIGHT


def test_copy_that_timed_out_has_stopped_and_left_no_partial_file(
    sftp_loopback, sftp_root, sftp_wire, backends, sent
):
    expected = _make_tree(sftp_root / "src")
    (sftp_root / "dst").mkdir()
    backend = make_backend("asyncssh", sftp_loopback)
    backends.append(backend)
    src = SFTPPath("/src", backend=backend)
    aclient = src._sftpclient._aclient
    # 480 writes, one every 10 ms at the quickest: more than the bound
    # below.
    sftp_wire.before["write"] = _slow(0.01)

    with pytest.raises(TimeoutError):
        _loop._run(
            _walk._concurrent_copy(
                src,
                SFTPPath("/dst", backend=backend),
                overwrite=False,
                follow_symlinks=True,
                preserve_metadata=False,
                max_concurrency=16,
                ignore_error=None,
                aclient=aclient,
            ),
            2.0,
        )
    raised_at = time.perf_counter()

    _assert_stopped(sftp_wire, sent, raised_at)
    # Each file that is there is whole.
    copied = _files(sftp_root / "dst")
    assert len(copied) < len(expected)
    for name, content in copied.items():
        assert content == expected[name], name


@pytest.mark.parametrize("operation", ["copy", "rm"])
def test_walk_interrupted_in_the_calling_thread_has_stopped(
    operation, flavor, sftp_loopback, sftp_root, sftp_wire, backends, monkeypatch, sent
):
    expected = _make_tree(sftp_root / "src")
    at = _paths("asyncssh", flavor, sftp_loopback, backends)
    src = at("src")
    src.exists()
    # Every write (copy) or removal (rm) takes the server 10 ms at the
    # quickest, and there are hundreds: the walk is under way when the
    # interrupt arrives.
    sftp_wire.before["write" if operation == "copy" else "remove"] = _slow(0.01)
    _interrupt_first_unbounded_wait(monkeypatch, after=0.5)

    with pytest.raises(KeyboardInterrupt):
        if operation == "copy":
            src.copy(at("dst"), recursive=True)
        else:
            src.rm(recursive=True)
    raised_at = time.perf_counter()

    _assert_stopped(sftp_wire, sent, raised_at)
    if operation == "copy":
        copied = _files(sftp_root / "dst")
        assert len(copied) < len(expected)
        for name, content in copied.items():
            assert content == expected[name], name
    else:
        assert _entries(sftp_root / "src") > 0


def test_failures_behind_the_raised_one_are_not_left_unretrieved(
    flavor, sftp_loopback, sftp_root, sftp_wire, backends, caplog
):
    _make_tree(sftp_root / "src")
    at = _paths("asyncssh", flavor, sftp_loopback, backends)
    src = at("src")
    src.exists()
    sftp_wire.before["open"] = _deny(lambda p: b"/src/" in p, nth=30, onward=True)

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        with pytest.raises(PermissionError):
            src.copy(at("dst"), recursive=True)
        time.sleep(0.3)
        gc.collect()

    # Only the walk's own tasks: asyncssh's read-ahead tasks for a read that
    # was cancelled are its own to retrieve, and it logs the same message.
    assert [
        message
        for message in (record.getMessage() for record in caplog.records)
        if "never retrieved" in message and "_concurrent_" in message
    ] == []


# --- the wait of _run() ---------------------------------------------------------


def test_run_waits_for_a_cancelled_coroutine_to_unwind_after_an_interrupt(
    monkeypatch,
):
    unwound = threading.Event()

    async def stalled():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)  # the cleanup requests of a real walk
            unwound.set()
            raise

    _interrupt_first_unbounded_wait(monkeypatch, after=0.1)

    with pytest.raises(KeyboardInterrupt):
        _loop._run(stalled(), None)
    assert unwound.is_set()


def test_run_waits_for_a_cancelled_coroutine_to_unwind_after_a_timeout():
    unwound = threading.Event()

    async def stalled():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)
            unwound.set()
            raise

    with pytest.raises(TimeoutError):
        _loop._run(stalled(), 0.1)
    assert unwound.is_set()


def test_run_returns_what_the_coroutine_returns_and_raises_what_it_raises():
    async def answer():
        return 42

    async def refuse():
        raise PermissionError("no")

    assert _loop._run(answer(), 5) == 42
    with pytest.raises(PermissionError):
        _loop._run(refuse(), 5)


# --- cancellation inside the walk --------------------------------------------------


class _Handle:
    def __init__(self, client, path, mode):
        self.client, self.path, self.mode = client, path, mode
        self.sent = False

    async def read(self, size=-1):
        if self.sent:
            return b""
        self.sent = True
        return b"data"

    async def write(self, data):
        pass

    async def close(self):
        self.client.closed.append((self.path, self.mode))


class _SlowOpenClient:
    """A client whose open of `/dst/a.txt` is on the wire until released."""

    def __init__(self):
        self.release = None
        self.requested = None
        self.closed = []
        self.removed = []
        self.chmods = []

    async def stat(self, path):
        if path == "/src":
            return asyncssh.SFTPAttrs(permissions=0o40755)
        if path == "/src/a.txt":
            return asyncssh.SFTPAttrs(permissions=0o100644)
        raise asyncssh.SFTPNoSuchFile(path)

    lstat = stat

    async def readdir(self, path):
        return [SimpleNamespace(filename="a.txt")]

    async def open(self, path, mode, encoding=None):
        if path == "/dst/a.txt":
            self.requested.set()
            await self.release.wait()
        return _Handle(self, path, mode)

    async def remove(self, path):
        self.removed.append(path)

    async def chmod(self, path, mode):
        self.chmods.append((path, mode))


class _FakePath:
    """What the walks read of a path: `_wirepath`, `name` and `/`."""

    def __init__(self, path):
        self._wirepath = path
        self.name = path.rsplit("/", 1)[1]

    def __truediv__(self, name):
        return _FakePath(f"{self._wirepath}/{name}")

    def __str__(self):
        return self._wirepath


def test_cancelled_open_of_a_destination_is_completed_then_undone():
    async def scenario():
        client = _SlowOpenClient()
        client.release = asyncio.Event()
        client.requested = asyncio.Event()
        walk = asyncio.ensure_future(
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
        await client.requested.wait()
        walk.cancel()
        asyncio.get_running_loop().call_later(0.1, client.release.set)
        with pytest.raises(asyncio.CancelledError):
            await walk
        return client

    client = asyncio.run(scenario())
    # The request was already on the wire: it finished, its handle was closed
    # and the file it created was removed.
    assert ("/dst/a.txt", "wb") in client.closed
    assert client.removed == ["/dst/a.txt"]


def test_failures_that_finish_with_the_raised_one_are_retrieved_by_the_walk():
    class _Refusing(_SlowOpenClient):
        """Five children whose destination opens are all refused at once."""

        count = 5

        def __init__(self):
            super().__init__()
            self.waiting = 0

        async def stat(self, path):
            if path == "/src":
                return asyncssh.SFTPAttrs(permissions=0o40755)
            if path.startswith("/src/"):
                return asyncssh.SFTPAttrs(permissions=0o100644)
            raise asyncssh.SFTPNoSuchFile(path)

        lstat = stat

        async def readdir(self, path):
            return [SimpleNamespace(filename=f"{n}.txt") for n in range(self.count)]

        async def open(self, path, mode, encoding=None):
            if not path.startswith("/dst/"):
                return _Handle(self, path, mode)
            self.waiting += 1
            if self.waiting == self.count:
                self.requested.set()
            await self.release.wait()
            raise asyncssh.SFTPPermissionDenied("refused by the test")

    async def scenario():
        unhandled = []
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: unhandled.append(context)
        )
        client = _Refusing()
        client.release = asyncio.Event()
        client.requested = asyncio.Event()
        walk = asyncio.ensure_future(
            _walk._concurrent_copy(
                _FakePath("/src"),
                _FakePath("/dst"),
                overwrite=False,
                follow_symlinks=True,
                preserve_metadata=False,
                max_concurrency=8,
                ignore_error=None,
                aclient=client,
            )
        )
        await client.requested.wait()
        client.release.set()
        raised = False
        try:
            await walk
        except PermissionError:
            raised = True
        # The walk and its callbacks are let go, then the tasks are collected:
        # a task that is collected still holding a failure nobody read is
        # reported to the loop's exception handler.
        del walk
        for _ in range(3):
            await asyncio.sleep(0)
        gc.collect()
        return raised, unhandled

    raised, unhandled = asyncio.run(scenario())
    assert raised
    assert unhandled == []


def test_cancelled_walk_waits_for_a_running_error_handler():
    handled = threading.Event()
    started = threading.Event()

    def handler(error):
        started.set()
        time.sleep(0.3)
        handled.set()

    class _FailingClient(_SlowOpenClient):
        async def open(self, path, mode, encoding=None):
            if path == "/dst/a.txt":
                raise asyncssh.SFTPPermissionDenied("no")
            return _Handle(self, path, mode)

    async def scenario():
        walk = asyncio.ensure_future(
            _walk._concurrent_copy(
                _FakePath("/src"),
                _FakePath("/dst"),
                overwrite=False,
                follow_symlinks=True,
                preserve_metadata=False,
                max_concurrency=4,
                ignore_error=handler,
                aclient=_FailingClient(),
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.01)
        walk.cancel()
        with pytest.raises(asyncio.CancelledError):
            await walk
        # Checked before asyncio.run() joins its worker threads: the walk
        # itself has to have waited for the handler.
        assert handled.is_set()

    asyncio.run(scenario())


# --- what a failed or refused operation leaves behind, on either backend ------


@pytest.fixture
def native_calls(monkeypatch):
    """How often the asyncssh walks ran, to tell the native path from the
    generic one."""
    calls = collections.Counter()
    for name in ("_concurrent_copy", "_concurrent_rm"):
        real = getattr(backend_mod, name)

        def counted(*args, _name=name, _real=real, **kwargs):
            calls[_name] += 1
            return _real(*args, **kwargs)

        monkeypatch.setattr(backend_mod, name, counted)
    return calls


def _native(kind, calls, name, times=1):
    """A scenario that qualifies for the native walk takes it on asyncssh and
    never on paramiko."""
    assert calls[name] == (times if kind == "asyncssh" else 0)


def test_unreadable_source_leaves_the_existing_destination_intact(
    kind, flavor, sftp_loopback, sftp_root, sftp_wire, backends, native_calls
):
    (sftp_root / "src").mkdir()
    (sftp_root / "src" / "keep.txt").write_bytes(b"new")
    (sftp_root / "dst").mkdir()
    (sftp_root / "dst" / "keep.txt").write_bytes(b"PRECIOUS OLD CONTENT")
    at = _paths(kind, flavor, sftp_loopback, backends)
    sftp_wire.before["open"] = _deny(lambda p: p.endswith(b"/src/keep.txt"))

    with pytest.raises(PermissionError):
        at("src").copy(at("dst"), recursive=True, overwrite=True)

    assert _files(sftp_root / "dst") == {"keep.txt": b"PRECIOUS OLD CONTENT"}
    _native(kind, native_calls, "_concurrent_copy")


def test_a_copy_that_fails_while_reading_leaves_no_file_behind(
    kind, flavor, sftp_loopback, sftp_root, sftp_wire, backends, native_calls
):
    (sftp_root / "src").mkdir()
    (sftp_root / "src" / "big.bin").write_bytes(b"x" * 3_000_000)
    at = _paths(kind, flavor, sftp_loopback, backends)
    reads = [0]
    failed = []

    def fail_second_read(server, *args):
        reads[0] += 1
        if reads[0] == 2:
            failed.append(True)
            raise asyncssh.SFTPFailure("failed by the test")

    sftp_wire.before["read"] = fail_second_read

    with pytest.raises(OSError) as raised:
        at("src").copy(at("dst"), recursive=True)

    assert not isinstance(raised.value, FileExistsError)
    assert failed == [True]
    assert _files(sftp_root / "dst") == {}
    _native(kind, native_calls, "_concurrent_copy")


def _chain(sftp_root):
    """root/tree/a/b/c/locked.txt, the only thing the tests make fail."""
    folder = sftp_root / "tree" / "a" / "b" / "c"
    folder.mkdir(parents=True)
    (folder / "locked.txt").write_bytes(b"x")
    return folder


def test_error_the_handler_declined_is_offered_once(
    kind, flavor, sftp_loopback, sftp_root, sftp_wire, backends, native_calls
):
    _chain(sftp_root)
    at = _paths(kind, flavor, sftp_loopback, backends)
    sftp_wire.before["remove"] = _deny(lambda p: p.endswith(b"locked.txt"))
    offered = []

    def decline(error, path):
        offered.append((type(error).__name__, path.name, type(path)))
        return False

    tree = at("tree")
    with pytest.raises(PermissionError):
        tree.rm(recursive=True, ignore_error=decline)

    # The handler is given the paths of the flavor the call was made on.
    assert offered == [("PermissionError", "locked.txt", type(tree))]
    assert (sftp_root / "tree" / "a" / "b" / "c" / "locked.txt").exists()
    _native(kind, native_calls, "_concurrent_rm")


def test_errors_the_handler_swallows_are_offered_one_by_one(
    kind, flavor, sftp_loopback, sftp_root, sftp_wire, backends, native_calls
):
    _chain(sftp_root)
    at = _paths(kind, flavor, sftp_loopback, backends)
    sftp_wire.before["remove"] = _deny(lambda p: p.endswith(b"locked.txt"))
    offered = []

    def swallow(error, path):
        offered.append((path.name, type(path)))
        return True

    tree = at("tree")
    tree.rm(recursive=True, ignore_error=swallow)

    # The file, then each directory above it that could not be removed.
    assert offered == [
        (name, type(tree)) for name in ("locked.txt", "c", "b", "a", "tree")
    ]
    assert (sftp_root / "tree" / "a" / "b" / "c" / "locked.txt").exists()
    _native(kind, native_calls, "_concurrent_rm")


class _Stop(Exception):
    """What a predicate raises to end the operation."""


def _copy_tree(sftp_root):
    """root/src/a/b/c/{locked.txt, fine.txt}: the copy of `locked.txt` is the
    one the tests make fail."""
    folder = sftp_root / "src" / "a" / "b" / "c"
    folder.mkdir(parents=True)
    (folder / "locked.txt").write_bytes(b"locked")
    (folder / "fine.txt").write_bytes(b"fine")
    (sftp_root / "dst").mkdir()


@pytest.mark.parametrize("decision", ["swallow", "decline", "raise"])
def test_copy_on_error_decides_each_failing_child_once(
    decision, kind, flavor, sftp_loopback, sftp_root, sftp_wire, backends, native_calls
):
    _copy_tree(sftp_root)
    at = _paths(kind, flavor, sftp_loopback, backends)
    sftp_wire.before["open"] = _deny(
        lambda p: p.endswith(b"/dst/a/b/c/locked.txt"), onward=True
    )
    offered = []

    def predicate(error, path):
        offered.append((type(error).__name__, path.name, type(path)))
        if decision == "raise":
            raise _Stop()
        return decision == "swallow"

    src = at("src")

    def copy():
        src.copy(at("dst"), recursive=True, overwrite=True, on_error=predicate)

    if decision == "swallow":
        copy()
        assert _files(sftp_root / "dst") == {"a/b/c/fine.txt": b"fine"}
    else:
        with pytest.raises(PermissionError if decision == "decline" else _Stop):
            copy()

    # The source child that failed, offered once, whatever the directories
    # above it did with the error; the path is of the flavor the call was
    # made on.
    assert offered == [("PermissionError", "locked.txt", type(src))]
    _native(kind, native_calls, "_concurrent_copy")


def test_copy_that_declines_an_error_stops_after_the_children_already_running(
    kind, flavor, sftp_loopback, sftp_root, sftp_wire, backends, native_calls
):
    expected = _make_tree(sftp_root / "src")
    (sftp_root / "dst").mkdir()
    at = _paths(kind, flavor, sftp_loopback, backends)
    sftp_wire.before["open"] = _deny(lambda p: b"/dst/" in p, nth=5, onward=True)
    offered = []

    def decline(error, path):
        offered.append(path.name)
        return False

    with pytest.raises(PermissionError):
        at("src").copy(at("dst"), recursive=True, overwrite=True, on_error=decline)

    # The sequential walk stops at the first error it is told not to skip.
    # The concurrent one asks each child that fails before the others are
    # cancelled about its own error: how many depends on the machine, never
    # all of them.
    if kind == "paramiko":
        assert len(offered) == 1
    else:
        assert 0 < len(offered) < len(expected)
    assert len(_files(sftp_root / "dst")) < len(expected)
    _native(kind, native_calls, "_concurrent_copy")


def test_copy_with_both_error_keywords_raises_before_any_request(
    kind, flavor, sftp_loopback, sftp_root, sftp_wire, backends, native_calls
):
    _copy_tree(sftp_root)
    at = _paths(kind, flavor, sftp_loopback, backends)

    with pytest.raises(TypeError):
        at("src").copy(
            at("dst"),
            recursive=True,
            ignore_error=True,
            on_error=lambda error, path: True,
        )

    assert sftp_wire.total() == 0
    assert _files(sftp_root / "dst") == {}
    _native(kind, native_calls, "_concurrent_copy", times=0)


def test_rm_handler_that_raises_is_not_offered_its_own_exception(
    kind, flavor, sftp_loopback, sftp_root, sftp_wire, backends, native_calls
):
    _chain(sftp_root)
    at = _paths(kind, flavor, sftp_loopback, backends)
    sftp_wire.before["remove"] = _deny(lambda p: p.endswith(b"locked.txt"))
    offered = []

    def stop(error, path):
        offered.append(type(error).__name__)
        raise _Stop()

    with pytest.raises(_Stop):
        at("tree").rm(recursive=True, ignore_error=stop)

    assert offered == ["PermissionError"]
    _native(kind, native_calls, "_concurrent_rm")


def test_missing_ok_covers_the_path_itself_and_not_what_vanished_below_it(
    kind, flavor, sftp_loopback, sftp_root, sftp_wire, backends, native_calls
):
    at = _paths(kind, flavor, sftp_loopback, backends)
    at("absent").rm(recursive=True, missing_ok=True)

    (sftp_root / "tree" / "sub").mkdir(parents=True)
    (sftp_root / "tree" / "sub" / "ghost.txt").write_bytes(b"x")
    sftp_wire.before["remove"] = _deny(
        lambda p: p.endswith(b"ghost.txt"), error=asyncssh.SFTPNoSuchFile
    )

    with pytest.raises(FileNotFoundError):
        at("tree").rm(recursive=True, missing_ok=True)

    assert (sftp_root / "tree" / "sub" / "ghost.txt").exists()
    # Both calls took the native walk: the missing path is found by the walk's
    # own stat of its root.
    _native(kind, native_calls, "_concurrent_rm", times=2)


def test_missing_root_is_an_error_without_missing_ok(
    kind, flavor, sftp_loopback, backends
):
    at = _paths(kind, flavor, sftp_loopback, backends)

    with pytest.raises(FileNotFoundError):
        at("absent").rm(recursive=True)


# --- which copies take the native walk ---------------------------------------


def _small_tree(root):
    (root / "src" / "sub").mkdir(parents=True)
    (root / "src" / "a.txt").write_bytes(b"a")
    (root / "src" / "sub" / "b.txt").write_bytes(b"b")
    return {"a.txt": b"a", "sub/b.txt": b"b"}


def test_a_copy_and_a_remove_inside_one_server_take_the_native_walk(
    kind, flavor, sftp_loopback, sftp_root, backends, native_calls
):
    expected = _small_tree(sftp_root)
    at = _paths(kind, flavor, sftp_loopback, backends)

    at("src").copy(at("out"), recursive=True)
    assert _files(sftp_root / "out") == expected
    _native(kind, native_calls, "_concurrent_copy")

    at("out").rm(recursive=True)
    assert not (sftp_root / "out").exists()
    _native(kind, native_calls, "_concurrent_rm")


def test_a_target_on_another_server_is_copied_by_the_generic_copy(
    kind,
    flavor,
    sftp_loopback,
    sftp_loopbacks,
    sftp_root,
    tmp_path,
    backends,
    native_calls,
):
    expected = _small_tree(sftp_root)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = sftp_loopbacks(other_root)
    source = _paths(kind, flavor, sftp_loopback, backends)
    target = _paths(kind, flavor, other, backends)

    source("src").copy(target("out"), recursive=True)

    assert _files(other_root / "out") == expected
    assert not (sftp_root / "out").exists()
    assert native_calls["_concurrent_copy"] == 0


def test_a_target_of_another_account_is_copied_by_the_generic_copy(
    kind, flavor, sftp_loopback, sftp_root, backends, native_calls
):
    expected = _small_tree(sftp_root)
    source = _paths(kind, flavor, sftp_loopback, backends)
    target = _paths(kind, flavor, sftp_loopback, backends, user="y")

    source("src").copy(target("out"), recursive=True)

    assert _files(sftp_root / "out") == expected
    assert native_calls["_concurrent_copy"] == 0


def test_two_backends_of_one_endpoint_are_one_tree_for_a_path_and_two_for_a_uri(
    kind, flavor, sftp_loopback, sftp_root, backends, native_calls
):
    expected = _small_tree(sftp_root)
    source = _paths(kind, flavor, sftp_loopback, backends)
    target = _paths(kind, flavor, sftp_loopback, backends)
    assert source.backend is not target.backend

    source("src").copy(target("out"), recursive=True)

    assert _files(sftp_root / "out") == expected
    # An `SFTPPath` takes equal endpoints for one server and walks it over the
    # source's connection; a URI path takes only a shared backend as proof.
    native = kind == "asyncssh" and flavor == "path"
    assert native_calls["_concurrent_copy"] == (1 if native else 0)


def _link(path, target):
    """A symbolic link `path` to the directory `target`, or skip where the
    platform does not allow one."""
    try:
        os.symlink(target, path, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"cannot create a symbolic link here: {error}")


def test_a_link_copied_as_a_link_is_the_generic_copy_and_a_followed_one_is_not(
    kind, flavor, sftp_loopback, sftp_root, backends, native_calls
):
    real = sftp_root / "real"
    real.mkdir()
    (real / "f.txt").write_bytes(b"f")
    # Absolute: the server reads a relative target against its own working
    # directory, outside the tree it serves.
    _link(sftp_root / "link", str(real))
    at = _paths(kind, flavor, sftp_loopback, backends)

    at("link").copy(at("as_link"), recursive=True, follow_symlinks=False)

    assert os.path.islink(sftp_root / "as_link")
    assert os.path.realpath(sftp_root / "as_link") == os.path.realpath(real)
    assert native_calls["_concurrent_copy"] == 0

    at("link").copy(at("followed"), recursive=True)

    assert not os.path.islink(sftp_root / "followed")
    assert _files(sftp_root / "followed") == {"f.txt": b"f"}
    _native(kind, native_calls, "_concurrent_copy")


# --- modes -----------------------------------------------------------------------

#: What the server reports for each entry of the tree the mode tests copy:
#: type bits plus a mode with a special bit where one is meant to be tested.
_REPORTED = {
    b"/src": 0o40751,
    b"/src/a.txt": 0o104755,
    b"/src/sub": 0o41777,
    b"/src/sub/b.txt": 0o102640,
}


class _OtherSFTPPath(SFTPPath):
    """A second class of the same kind: `type(src) is not type(dst)`."""


if SFTPURIPath is not None:

    class _OtherSFTPURIPath(SFTPURIPath):
        """A second class of the same kind, registered for no scheme."""

        __SCHEMES = ()


def _mode_tree(sftp_root, sftp_wire):
    (sftp_root / "src" / "sub").mkdir(parents=True)
    (sftp_root / "src" / "a.txt").write_bytes(b"a")
    (sftp_root / "src" / "sub" / "b.txt").write_bytes(b"b")

    def reshape(result, path, *args):
        if not isinstance(result, asyncssh.SFTPAttrs):
            result = asyncssh.SFTPAttrs.from_local(result)
        if path in _REPORTED:
            result.permissions = _REPORTED[path]
        return result

    def reshape_entry(directory, entry):
        # What the walks read: the listing says the same as stat and lstat.
        name = entry.filename
        name = name if isinstance(name, bytes) else name.encode()
        full = directory.rstrip(b"/") + b"/" + name
        if full in _REPORTED and entry.attrs is not None:
            entry.attrs.permissions = _REPORTED[full]
        return entry

    sftp_wire.after["stat"] = sftp_wire.after["lstat"] = reshape
    sftp_wire.listed = reshape_entry
    sent = []

    def record(server, path, attrs, *args):
        if attrs.permissions is not None:
            sent.append((path.decode(), attrs.permissions))

    sftp_wire.before["setstat"] = record
    return sent


def test_copy_sends_permission_bits_only(
    kind, flavor, sftp_loopback, sftp_root, sftp_wire, backends
):
    sent = _mode_tree(sftp_root, sftp_wire)
    at = _paths(kind, flavor, sftp_loopback, backends)

    at("src").copy(at("dst"), recursive=True)

    assert sent
    assert [mode for _, mode in sent if mode > 0o7777] == []


def test_directory_modes_are_settled_after_what_is_in_them(
    kind, flavor, sftp_loopback, sftp_root, sftp_wire, backends
):
    sent = _mode_tree(sftp_root, sftp_wire)
    at = _paths(kind, flavor, sftp_loopback, backends)

    at("src").copy(at("dst"), recursive=True)

    order = [path for path, _ in sent]
    for directory, child in (
        ("/dst/sub", "/dst/sub/b.txt"),
        ("/dst", "/dst/sub"),
        ("/dst", "/dst/a.txt"),
    ):
        assert order.index(directory) > order.index(child), (directory, order)


def test_special_mode_bits_are_kept_between_two_paths_of_one_class(
    kind, flavor, sftp_loopback, sftp_root, sftp_wire, backends
):
    sent = _mode_tree(sftp_root, sftp_wire)
    at = _paths(kind, flavor, sftp_loopback, backends)

    at("src").copy(at("dst"), recursive=True)

    assert dict(sent) == {
        "/dst/a.txt": 0o4755,
        "/dst/sub/b.txt": 0o2640,
        "/dst/sub": 0o1777,
        "/dst": 0o751,
    }


def test_special_mode_bits_are_dropped_between_two_classes(
    kind, flavor, sftp_loopback, sftp_root, sftp_wire, backends
):
    sent = _mode_tree(sftp_root, sftp_wire)
    at = _paths(kind, flavor, sftp_loopback, backends)
    if flavor == "path":
        target = _OtherSFTPPath("/dst", backend=at.backend)
    else:
        target = _OtherSFTPURIPath(sftp_loopback.url("dst"), backend=at.backend)

    at("src").copy(target, recursive=True)

    assert dict(sent) == {
        "/dst/a.txt": 0o755,
        "/dst/sub/b.txt": 0o640,
        "/dst/sub": 0o777,
        "/dst": 0o751,
    }
