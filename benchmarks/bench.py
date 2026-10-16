"""sftppath benchmark runner.

Run `python benchmarks/bench.py --help` for the suites. `--save` writes the
run as structured JSON into `benchmarks/results/`; the schema is documented in
`benchmarks/README.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import datetime
import functools
import itertools
import json
import os
import pathlib
import platform
import re
import shutil
import statistics
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import typing as ty

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
# The checkout's sources win over an installed copy: a run measures the tree it
# is started from.
sys.path.insert(0, str(REPO_ROOT / "src"))

SCHEMA = "sftppath.bench/1"
MAX_SAMPLES = 100

#: Set to `1` to add the recursive copy rows to the default suite.
RECURSIVE_ENV = "SFTPPATH_BENCH_RECURSIVE"

#: The groups of the saved metrics: `SFTPPath` over each backend, and the same
#: operations through the `sftp:` scheme of `uripath`.
GROUP = "sftp"
URI_GROUP = "sftp-uri"

BACKENDS = ("paramiko", "asyncssh")

#: The group of the event-loop rows: an `AsyncPath` over an `SFTPPath` (each call
#: on a worker thread) against the native `AsyncSFTPPath`, both on asyncssh.
ASYNC_GROUP = "sftp-async"
ASYNC_FORMS = ("threads", "native")

#: Bound, in seconds, on one asyncssh request. A stalled request fails its case
#: instead of holding the run.
ASYNCSSH_TIMEOUT = 3.0

#: The login of the loopback server, which accepts any.
USER = "x"
PASSWORD = "x"


class _Recorder:
    """Collects every measured metric of one run for `--save`.

    A timing metric stores min/median/max milliseconds per call over its
    samples; each sample times `calls_per_sample` calls of the operation.
    """

    def __init__(self):
        self.metrics = {}
        self.counters = {}
        self.errors = {}
        self.skipped = {}

    def timing(self, name, samples, *, group, calls=1):
        per_call_ms = sorted(sample / calls * 1000.0 for sample in samples)
        self.metrics[name] = {
            "group": group,
            "min_ms": round(per_call_ms[0], 6),
            "median_ms": round(statistics.median(per_call_ms), 6),
            "max_ms": round(per_call_ms[-1], 6),
            "samples": len(per_call_ms),
            "calls_per_sample": calls,
        }

    def counter(self, name, values, *, group):
        self.counters[name] = {"group": group, **values}

    def error(self, name, message, *, group):
        self.errors[name] = {"group": group, "error": message}

    def skip(self, name, reason, *, group):
        self.skipped[name] = {"group": group, "reason": reason}


RECORDER = _Recorder()
_SAMPLES_OVERRIDE = None


def _reps(default):
    """Sample count for a case: `--samples` when given, else the case default."""
    return _SAMPLES_OVERRIDE or default


class _Numbered:
    """An operation that needs a name of its own on every call.

    `before()` numbers the next call and runs `prepare(n)`, which is not timed;
    calling the object runs `operate(n)`. The warm-up call counts as one.
    """

    def __init__(self, operate, prepare=None):
        self._operate = operate
        self._prepare = prepare
        self._numbers = itertools.count()
        self._current = -1

    def before(self):
        self._current = next(self._numbers)
        if self._prepare is not None:
            self._prepare(self._current)

    def __call__(self):
        self._operate(self._current)


def _measure(
    operation,
    *,
    repeat=5,
    warmup=True,
    name=None,
    group=None,
    calls=1,
    status=False,
):
    """Time `repeat` samples of `operation`; return the median sample seconds.

    An operation with a `before` attribute has it called, untimed, ahead of the
    warm-up call and of every sample. With `name`, the samples are also recorded
    for `--save`. With `status`, an exception is recorded as an error and
    returned as a `"Type: message"` string instead of raised.
    """
    before = getattr(operation, "before", None)

    def timed():
        if before is not None:
            before()
        start = time.perf_counter()
        operation()
        return time.perf_counter() - start

    try:
        if warmup:
            timed()
        samples = [timed() for _ in range(repeat)]
    except Exception as error:
        if not status:
            raise
        message = f"{type(error).__name__}: {error}"
        if name is not None:
            RECORDER.error(name, message, group=group)
        return message
    if name is not None:
        RECORDER.timing(name, samples, group=group, calls=calls)
    return statistics.median(samples)


def _fmt_metric(value):
    if isinstance(value, (int, float)):
        return f"{value:.4f}s"
    return str(value)


def _fmt_ratio(value):
    if isinstance(value, (int, float)):
        return f"{value:.2f}x"
    return str(value)


def _ratio(paramiko_time, asyncssh_time):
    """paramiko over asyncssh, or `"n/a"` when either case failed."""
    if not all(isinstance(t, (int, float)) for t in (paramiko_time, asyncssh_time)):
        return "n/a"
    return paramiko_time / asyncssh_time if asyncssh_time > 0 else float("inf")


# --- the server --------------------------------------------------------------


class _Server(ty.NamedTuple):
    host: str
    port: int
    root: pathlib.Path  # the directory it serves, as a local path


@contextlib.contextmanager
def _loopback_sftp_server():
    """An asyncssh SFTP server on 127.0.0.1 that accepts any login and serves a
    temporary directory, on an event loop of its own. Yields a `_Server`."""
    import asyncssh

    live = set()

    class _NoAuth(asyncssh.SSHServer):
        def connection_made(self, conn):
            self._conn = conn
            live.add(conn)

        def connection_lost(self, exc):
            live.discard(self._conn)

        def begin_auth(self, username):
            return False

    async def start(root):
        return await asyncssh.listen(
            "127.0.0.1",
            0,
            server_factory=_NoAuth,
            server_host_keys=[asyncssh.generate_private_key("ssh-rsa")],
            sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(root)),
            process_factory=None,
            # The default asks the resolver for this machine's own name.
            gss_host=None,
        )

    async def stop(server):
        server.close()
        for conn in list(live):
            conn.abort()
        try:
            await asyncio.wait_for(server.wait_closed(), 5)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0.1)

    loop = (
        asyncio.SelectorEventLoop()
        if sys.platform == "win32"
        else asyncio.new_event_loop()
    )
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        server = asyncio.run_coroutine_threadsafe(start(root), loop).result()
        try:
            yield _Server("127.0.0.1", server.sockets[0].getsockname()[1], root)
        finally:
            try:
                asyncio.run_coroutine_threadsafe(stop(server), loop).result(10)
            except Exception:
                loop.call_soon_threadsafe(server.close)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            if not thread.is_alive():
                loop.close()


def _write_tree(root, files):
    """Write `{relative path: str or bytes}` below `root`."""
    for relpath, data in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def _seed(root):
    """The files the cases read, list, walk and copy, below the server's root."""
    files = {
        "bench/stat/small.txt": "hello world\n",
        "bench/read/small.txt": "read me\n" * 64,
        "bench/copy_file/source.bin": os.urandom(256 * 1024),
    }
    files.update(
        {f"bench/listing/file_{i:03d}.txt": f"listing {i}\n" for i in range(64)}
    )
    files.update(
        {f"bench/listing/dir_{i:02d}/nested.txt": "nested\n" for i in range(8)}
    )
    files.update(
        {
            f"bench/walk/dir_{i}/sub_{j}/file_{k}.txt": f"{i}-{j}-{k}\n"
            for i in range(4)
            for j in range(5)
            for k in range(4)
        }
    )
    for prefix, dirs, count, text in (
        ("bench/copy_tree_small/source", 2, 2, "small\n" * 8),
        ("bench/rm_tree/template", 3, 3, "rm\n" * 8),
        ("bench/recursive_large/source", 8, 16, "large\n" * 8),
        ("bench/recursive_large/template", 8, 16, "large-rm\n" * 8),
    ):
        files.update(
            {
                f"{prefix}/dir_{i:02d}/file_{j:02d}.txt": text
                for i in range(dirs)
                for j in range(count)
            }
        )
    _write_tree(root, files)


# --- the backends and the paths on them ---------------------------------------


def _backend_class_and_options(name, **extra):
    """The backend class `name` ("paramiko" or "asyncssh") and its constructor
    keywords other than the endpoint. Nothing of the developer's is read (no
    ssh_config, agent or key) and any host key is accepted: the server's key is
    new on every run, and verification stays on unless it is opted out of."""
    if name == "paramiko":
        import paramiko
        from sftppath import SFTPBackend

        return SFTPBackend, {
            "connect_opts": {"allow_agent": False, "look_for_keys": False},
            "hostkeypolicy": paramiko.AutoAddPolicy(),
            "ssh_config": None,
            "known_hosts": None,
            **extra,
        }
    from sftppath import AsyncSSHBackend

    return AsyncSSHBackend, {
        "connect_opts": {
            "client_keys": None,
            "agent_path": None,
            "public_key_auth": False,
            "kbdint_auth": False,
            "gss_kex": False,
            "gss_auth": False,
            "preferred_auth": "none,password",
            "known_hosts": None,
        },
        "ssh_config": None,
        "max_concurrency": 8,
        "sftp_version": 4,
        "timeout": ASYNCSSH_TIMEOUT,
        **extra,
    }


def _open_path(name, server, **extra):
    """`(root, close)`: the root of the server as an `SFTPPath` on a new backend
    of kind `name`, and the function that closes the backend."""
    from sftppath import SFTPPath

    cls, options = _backend_class_and_options(name, **extra)
    backend = cls(server.host, server.port, user=USER, password=PASSWORD, **options)
    return SFTPPath("/", backend=backend), backend.close


def _open_uri(name, server, **extra):
    """`(root, close)`: the root of the server as an `sftp:` `URIPath` on a new
    `SFTPURIBackend`, and the function that closes it."""
    from sftppath.uri import SFTPURIBackend
    from uripath import URIPath

    cls, options = _backend_class_and_options(name, **extra)
    backend = SFTPURIBackend(cls, **options)
    url = f"sftp://{USER}:{PASSWORD}@{server.host}:{server.port}/"
    return URIPath(url, backend=backend), backend.close


class _Target:
    """One backend of one group, which a case runs on: `root` is the server's
    root as a path, `key` names the directories below `bench/` that are this
    target's own, and `local` is the served directory on disk."""

    def __init__(self, group, name, key, server, opener):
        self.group = group
        self.name = name
        self.key = key
        self.local = server.root
        self._opener = functools.partial(opener, name, server)
        self.root, self._close = self._opener()
        for directory in ("write", "mkdir", "rename", "unlink"):
            (self.local / "bench" / directory / key).mkdir(parents=True, exist_ok=True)

    def open(self):
        """`(root, close)` on a backend of the same kind with no connection."""
        return self._opener()

    def close(self):
        self._close()


# --- the cases ---------------------------------------------------------------


def _stat(target):
    return lambda: (target.root / "bench/stat/small.txt").stat()


def _iterdir(target):
    return lambda: list((target.root / "bench/listing").iterdir())


def _walk(target):
    return lambda: list((target.root / "bench/walk").walk())


def _glob(target):
    return lambda: list((target.root / "bench/walk").glob("**/*.txt"))


def _read_small(target):
    return lambda: (target.root / "bench/read/small.txt").read_bytes()


def _read_batch(target):
    return lambda: [
        (target.root / f"bench/listing/file_{i:03d}.txt").read_bytes()
        for i in range(64)
    ]


def _stat_batch(target):
    return lambda: [
        (target.root / f"bench/listing/file_{i:03d}.txt").stat() for i in range(64)
    ]


def _write(target):
    return _Numbered(
        lambda n: (
            target.root / f"bench/write/{target.key}/out_{n:03d}.bin"
        ).write_bytes(b"x" * (256 * 1024))
    )


def _mkdir(target):
    return _Numbered(
        lambda n: (target.root / f"bench/mkdir/{target.key}/dir_{n:03d}").mkdir(
            exist_ok=False
        )
    )


def _rename(target):
    directory = f"bench/rename/{target.key}"
    return _Numbered(
        lambda n: (target.root / f"{directory}/src_{n:03d}.txt").rename(
            target.root / f"{directory}/dst_{n:03d}.txt"
        ),
        lambda n: (target.local / f"{directory}/src_{n:03d}.txt").write_bytes(
            b"rename\n"
        ),
    )


def _unlink(target):
    directory = f"bench/unlink/{target.key}"
    return _Numbered(
        lambda n: (target.root / f"{directory}/victim_{n:03d}.txt").unlink(),
        lambda n: (target.local / f"{directory}/victim_{n:03d}.txt").write_bytes(
            b"unlink\n"
        ),
    )


def _copy_file(target):
    return _Numbered(
        lambda n: (target.root / "bench/copy_file/source.bin").copy(
            target.root / f"bench/copy_file/{target.key}_out_{n:03d}.bin",
            overwrite=True,
        )
    )


def _copy_tree(source, destination):
    """A case that copies the directory `source` recursively to a new name
    `<destination>_<key>_<n>` each time."""

    def make(target):
        return _Numbered(
            lambda n: (target.root / source).copy(
                target.root / f"{destination}_{target.key}_{n:03d}",
                overwrite=True,
                recursive=True,
            )
        )

    return make


def _rm_tree(template, destination):
    """A case that removes recursively a copy of `template` named
    `<destination>_<key>_<n>`. The copy is made on disk before the timed call."""

    def make(target):
        def copied(n):
            return f"{destination}_{target.key}_{n:03d}"

        return _Numbered(
            lambda n: (target.root / copied(n)).rm(recursive=True),
            lambda n: shutil.copytree(
                target.local / template, target.local / copied(n)
            ),
        )

    return make


def _cold(target):
    """Connect on a new backend, log in and stat one file."""

    def operation():
        root, close = target.open()
        try:
            (root / "bench/stat/small.txt").stat()
        finally:
            close()

    return operation


@dataclasses.dataclass(frozen=True)
class _Case:
    """A measured operation, run on both backends of a group. `make(target)`
    returns the callable to time. A `status` case records its exception as an
    error and goes on; any other case raises it."""

    name: str
    make: ty.Callable
    modes: tuple
    repeat: int
    warmup: bool = True
    status: bool = False


def _cases():
    """The cases in run order. `modes` lists the benchmark modes (the suites)
    each belongs to."""
    copy_modes = ("recursive-copy",)
    if os.environ.get(RECURSIVE_ENV) == "1":
        copy_modes += ("all",)
    return [
        _Case("warm stat()", _stat, ("all",), 5),
        _Case("iterdir() 72 entries", _iterdir, ("all",), 3),
        _Case("walk() 80 files", _walk, ("all",), 2),
        _Case("glob('**/*.txt') 80 files", _glob, ("all",), 2),
        _Case("read_bytes() small file", _read_small, ("all",), 5),
        _Case("read_bytes() 64-file batch", _read_batch, ("all", "batch"), 2),
        _Case("stat() 64-file batch", _stat_batch, ("all", "batch"), 2),
        _Case("write_bytes() 256 KiB", _write, ("all",), 3, warmup=False),
        _Case("mkdir() leaf dir", _mkdir, ("all",), 3, warmup=False),
        _Case("rename() file", _rename, ("all",), 3, warmup=False),
        _Case("unlink() file", _unlink, ("all", "batch"), 3, warmup=False),
        _Case("copy() single 256 KiB file", _copy_file, ("all",), 3, warmup=False),
        _Case(
            "rm(recursive=True) 9-file tree",
            _rm_tree("bench/rm_tree/template", "bench/rm_tree/rm"),
            ("all", "recursive"),
            1,
            warmup=False,
            status=True,
        ),
        _Case(
            "copy(recursive=True) 4-file tree",
            _copy_tree("bench/copy_tree_small/source", "bench/copy_tree_small/copy"),
            copy_modes,
            1,
            warmup=False,
            status=True,
        ),
        _Case(
            "copy(recursive=True) 128-file tree",
            _copy_tree("bench/recursive_large/source", "bench/recursive_large/copy"),
            ("recursive-large",),
            1,
            warmup=False,
            status=True,
        ),
        _Case(
            "rm(recursive=True) 128-file tree",
            _rm_tree("bench/recursive_large/template", "bench/recursive_large/rm"),
            ("recursive-large",),
            1,
            warmup=False,
            status=True,
        ),
        _Case("cold connect + stat()", _cold, ("all",), 3, warmup=False),
    ]


def _compare(case, targets):
    """Run `case` on each target; return `(name, paramiko, asyncssh, ratio)`."""
    timings = {}
    for name, target in targets.items():
        timings[name] = _measure(
            case.make(target),
            repeat=_reps(case.repeat),
            warmup=case.warmup,
            name=f"{target.group}: {name} {case.name}",
            group=target.group,
            status=case.status,
        )
    return (
        case.name,
        timings["paramiko"],
        timings["asyncssh"],
        _ratio(timings["paramiko"], timings["asyncssh"]),
    )


def _copy_scaling(server, label, source, destination, counts):
    """The recursive copy of `source` on asyncssh backends of each
    `max_concurrency` in `counts`: `(name, median seconds or error)` rows."""
    rows = []
    for max_concurrency in counts:
        target = _Target(
            GROUP,
            "asyncssh",
            f"asyncssh_mc{max_concurrency}",
            server,
            functools.partial(_open_path, max_concurrency=max_concurrency),
        )
        name = f"asyncssh {label} mc={max_concurrency}"
        try:
            metric = _measure(
                _copy_tree(source, destination)(target),
                repeat=_reps(1),
                warmup=False,
                name=f"{GROUP}: {name}",
                group=GROUP,
                status=True,
            )
        finally:
            target.close()
        rows.append((name, metric))
    return rows


class SFTPResults(ty.NamedTuple):
    """What `benchmark_sftp_backends()` measured. `rows` and `uri_rows` are
    `(name, paramiko, asyncssh, ratio)` rows, or None where the group did not
    run (`info` and `uri_info` say why otherwise); `scaling_rows` are
    `(name, median seconds or error)`."""

    rows: list
    uri_rows: list
    scaling_rows: list
    info: str
    uri_info: str


def benchmark_sftp_backends(mode="all"):
    """Benchmark paramiko against asyncssh on a broad SFTP operation matrix,
    through `SFTPPath` and through the `sftp:` scheme of `uripath`.

    Both backends hit the same in-process loopback asyncssh server with
    identical data and no external host, so the numbers emphasize the overhead
    of a backend and of the path layer and its request pattern more than WAN
    latency. The `sftp:` group needs `uripath` and is skipped, with the reason
    recorded, without it. The recursive copy stress case is opt-in
    (`SFTPPATH_BENCH_RECURSIVE=1` or its own suite) so the default suite stays
    fast.
    """
    modes = ("all", "batch", "recursive", "recursive-copy", "recursive-large")
    if mode not in modes:
        raise ValueError(f"unknown SFTP benchmark mode: {mode!r}")
    try:
        import asyncssh  # noqa: F401
        import paramiko  # noqa: F401
    except ImportError as error:
        reason = f"{type(error).__name__}: {error}"
        for group in (GROUP, URI_GROUP):
            RECORDER.skip(group, reason, group=group)
        skipped = f"skipped: {reason}"
        return SFTPResults(None, None, [], skipped, skipped)

    info = f"loopback asyncssh server, {mode} mode, median seconds"
    groups = [(GROUP, "", _open_path)]
    uri_info = info
    try:
        import sftppath.uri  # noqa: F401
        import uripath  # noqa: F401
    except ImportError as error:
        reason = f"{type(error).__name__}: {error}"
        RECORDER.skip(URI_GROUP, reason, group=URI_GROUP)
        uri_info = f"skipped: {reason}"
    else:
        groups.append((URI_GROUP, "uri_", _open_uri))

    cases = [case for case in _cases() if mode in case.modes]
    rows = {}
    scaling_rows = []
    with _loopback_sftp_server() as server:
        _seed(server.root)
        for group, prefix, opener in groups:
            with contextlib.ExitStack() as stack:
                targets = {}
                for name in BACKENDS:
                    target = _Target(group, name, f"{prefix}{name}", server, opener)
                    stack.callback(target.close)
                    targets[name] = target
                for target in targets.values():
                    target.root.exists()
                rows[group] = [_compare(case, targets) for case in cases]
        if mode == "recursive-copy" or (
            mode == "all" and os.environ.get(RECURSIVE_ENV) == "1"
        ):
            scaling_rows += _copy_scaling(
                server,
                "recursive copy",
                "bench/copy_tree_small/source",
                "bench/copy_tree_small/scaling",
                (1, 4),
            )
        if mode == "recursive-large":
            scaling_rows += _copy_scaling(
                server,
                "large recursive copy",
                "bench/recursive_large/source",
                "bench/recursive_large/scaling",
                (1, 4, 8),
            )
    return SFTPResults(rows[GROUP], rows.get(URI_GROUP), scaling_rows, info, uri_info)


class _AsyncTarget:
    """One form of the event-loop path on a backend of its own: `root` is the
    server's root as an `AsyncPath` over an `SFTPPath` (`threads`) or an
    `AsyncSFTPPath` (`native`). The loop stays open for the whole run, so the
    connection a form opens is kept between samples."""

    def __init__(self, form, server, loop):
        from pathapi import AsyncPath
        from sftppath import AsyncSFTPPath, SFTPPath

        cls, options = _backend_class_and_options("asyncssh")
        self.backend = cls(
            server.host, server.port, user=USER, password=PASSWORD, **options
        )
        sync = SFTPPath("/", backend=self.backend)
        self.group = ASYNC_GROUP
        self.name = self.key = form
        self.local = server.root
        self.loop = loop
        self.root = AsyncSFTPPath(sync) if form == "native" else AsyncPath(sync)

    def run(self, awaitable):
        return self.loop.run_until_complete(awaitable)

    def close(self):
        try:
            if self.name == "native":
                self.run(self.root.aclose())
        finally:
            self.backend.close()


def _async_walk(target):
    async def walk():
        count = 0
        top = target.root / "bench/recursive_large/source"
        async for _path, _dirs, files in top.walk():
            count += len(files)
        return count

    return lambda: target.run(walk())


def _async_read_batch(target):
    async def read():
        paths = [target.root / f"bench/listing/file_{i:03d}.txt" for i in range(64)]
        return await asyncio.gather(*(path.read_bytes() for path in paths))

    return lambda: target.run(read())


def _async_copy_tree(target):
    return _Numbered(
        lambda n: target.run(
            (target.root / "bench/recursive_large/source").copy(
                target.root / f"bench/recursive_large/async_{target.key}_{n:03d}",
                overwrite=True,
                recursive=True,
            )
        )
    )


def _async_rm_tree(target):
    def copied(n):
        return f"bench/recursive_large/async_rm_{target.key}_{n:03d}"

    return _Numbered(
        lambda n: target.run((target.root / copied(n)).rm(recursive=True)),
        lambda n: shutil.copytree(
            target.local / "bench/recursive_large/template", target.local / copied(n)
        ),
    )


#: (name, make, samples): the cases of the event-loop group. Each tree is 8
#: directories of 16 small files.
_ASYNC_CASES = (
    ("walk of a 128-file tree", _async_walk, 5),
    ("read_bytes() of 64 files at once", _async_read_batch, 5),
    ("copy(recursive=True) of a 128-file tree", _async_copy_tree, 3),
    ("rm(recursive=True) of a 128-file tree", _async_rm_tree, 3),
)


def benchmark_sftp_async():
    """`AsyncPath(SFTPPath(...))` against `AsyncSFTPPath` on the loopback
    server: `(rows, info)` with rows `(name, threads, native, threads/native)`,
    or None rows (the reason recorded) where `pathapi` has no `AsyncPath` or
    asyncssh is missing."""
    try:
        import asyncssh  # noqa: F401
        from pathapi import AsyncPath  # noqa: F401
        from sftppath import AsyncSFTPPath  # noqa: F401
    except ImportError as error:
        reason = f"{type(error).__name__}: {error}"
        RECORDER.skip(ASYNC_GROUP, reason, group=ASYNC_GROUP)
        return None, f"skipped: {reason}"
    loop = (
        asyncio.SelectorEventLoop()
        if sys.platform == "win32"
        else asyncio.new_event_loop()
    )
    rows = []
    try:
        with _loopback_sftp_server() as server, contextlib.ExitStack() as stack:
            _seed(server.root)
            targets = {}
            for form in ASYNC_FORMS:
                target = _AsyncTarget(form, server, loop)
                stack.callback(target.close)
                targets[form] = target
            for name, make, repeat in _ASYNC_CASES:
                timings = {
                    form: _measure(
                        make(target),
                        repeat=_reps(repeat),
                        name=f"{ASYNC_GROUP}: {form} {name}",
                        group=ASYNC_GROUP,
                        status=True,
                    )
                    for form, target in targets.items()
                }
                rows.append(
                    (
                        name,
                        timings["threads"],
                        timings["native"],
                        _ratio(timings["threads"], timings["native"]),
                    )
                )
    finally:
        loop.close()
    return rows, "loopback asyncssh server, median seconds"


def print_async_results(rows, info, *, markdown=False):
    if rows is None:
        print(f"SFTP async comparison: {info}")
        return
    if markdown:
        for name, threads, native, ratio in rows:
            print(
                f"| SFTP async {name} | AsyncPath(SFTPPath): {_fmt_metric(threads)}, "
                f"AsyncSFTPPath: {_fmt_metric(native)} "
                f"(threads/native: {_fmt_ratio(ratio)}) |"
            )
        return
    print(f"SFTP async comparison ({info}):")
    for name, threads, native, ratio in rows:
        print(
            f"   - {name}: AsyncPath(SFTPPath)={_fmt_metric(threads)}, "
            f"AsyncSFTPPath={_fmt_metric(native)} "
            f"(threads/native={_fmt_ratio(ratio)})"
        )


def print_sftp_results(results, *, markdown=False):
    tables = (
        ("SFTP", "backend comparison", results.rows, results.info),
        ("SFTP URI", "path comparison", results.uri_rows, results.uri_info),
    )
    if markdown:
        print("| Benchmark Case | Time / Metric |")
        print("|---|---|")
        for label, title, rows, info in tables:
            if rows is None:
                print(f"| {label} {title} | {info} |")
                continue
            for name, t_paramiko, t_asyncssh, ratio in rows:
                print(
                    f"| {label} {name} | paramiko: {_fmt_metric(t_paramiko)}, "
                    f"asyncssh: {_fmt_metric(t_asyncssh)} "
                    f"(paramiko/asyncssh: {_fmt_ratio(ratio)}) |"
                )
        for name, metric in results.scaling_rows:
            print(f"| SFTP {name} | {_fmt_metric(metric)} |")
        return

    for label, title, rows, info in tables:
        if rows is None:
            print(f"{label} {title}: {info}")
            continue
        print(f"{label} {title} ({info}):")
        for name, t_paramiko, t_asyncssh, ratio in rows:
            if isinstance(ratio, (int, float)):
                winner = "asyncssh" if ratio > 1 else "paramiko"
            else:
                winner = "n/a"
            print(
                f"   - {name}: paramiko={_fmt_metric(t_paramiko)}, "
                f"asyncssh={_fmt_metric(t_asyncssh)} "
                f"(paramiko/asyncssh={_fmt_ratio(ratio)}, faster={winner})"
            )
    if results.scaling_rows:
        print("Asyncssh recursive copy scaling:")
        for name, metric in results.scaling_rows:
            print(f"   - {name}: {_fmt_metric(metric)}")


def main():
    print("Running benchmarks...")
    results = benchmark_sftp_backends()
    print_sftp_results(results)
    async_rows, async_info = benchmark_sftp_async()
    print_async_results(async_rows, async_info)
    # Markdown table format, for copy-pasting.
    print()
    print_sftp_results(results, markdown=True)
    print_async_results(async_rows, async_info, markdown=True)


# --- the saved result --------------------------------------------------------


def _pyproject_version():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def _git_state():
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()

    try:
        commit = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.SubprocessError):
        return None
    return {"commit": commit, "dirty": dirty}


def _interpreter_tag():
    """`<major.minor>-<os>-<arch>`, with the arch the interpreter was built for."""
    arch = sysconfig.get_platform().rsplit("-", 1)[-1]
    os_name = "darwin" if sys.platform == "darwin" else os.name
    return f"{sys.version_info[0]}.{sys.version_info[1]}-{os_name}-{arch}"


def build_result(suite, *, name, note=None):
    """The JSON document `--save` writes (schema: benchmarks/README.md)."""
    return {
        "schema": SCHEMA,
        "name": name,
        "suite": suite,
        "source": "ci" if os.environ.get("GITHUB_ACTIONS") == "true" else "local",
        "note": note,
        "created_utc": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "package": {"name": "sftppath", "version": _pyproject_version()},
        "git": _git_state(),
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "interpreter": _interpreter_tag(),
        "processor": f"{platform.system()}-{platform.machine()}",
        "cpu_count": os.cpu_count(),
        "env": {RECURSIVE_ENV: os.environ.get(RECURSIVE_ENV, "")},
        "metrics": dict(sorted(RECORDER.metrics.items())),
        "counters": dict(sorted(RECORDER.counters.items())),
        "errors": dict(sorted(RECORDER.errors.items())),
        "skipped": dict(sorted(RECORDER.skipped.items())),
    }


def default_result_name(suite):
    name = f"{_pyproject_version()}-py{_interpreter_tag()}"
    return name if suite == "default" else f"{name}-{suite}"


def save_result(suite, *, name=None, results_dir=None, note=None):
    name = name or default_result_name(suite)
    result = build_result(suite, name=name, note=note)
    directory = (
        pathlib.Path(results_dir)
        if results_dir
        else REPO_ROOT / "benchmarks" / "results"
    )
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.json"
    with open(target, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(f"\nSaved {len(result['metrics'])} metrics to {target}")
    return target


def _samples_arg(value):
    count = int(value)
    if not 1 <= count <= MAX_SAMPLES:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_SAMPLES}")
    return count


#: Suite name -> (benchmark mode, help text).
SUITES = {
    "sftp-recursive": ("recursive", "run only recursive SFTP remove probe rows"),
    "sftp-recursive-copy": ("recursive-copy", "run recursive SFTP copy probe rows"),
    "sftp-recursive-large": (
        "recursive-large",
        "run larger recursive SFTP copy/remove probe rows",
    ),
    "sftp-batch": ("batch", "run only batch SFTP probe rows"),
    "sftp-async": (
        "async",
        "AsyncPath(SFTPPath) against AsyncSFTPPath: walk, batch read, recursive copy and rm",
    ),
}


def cli(argv=None):
    global _SAMPLES_OVERRIDE

    parser = argparse.ArgumentParser(
        description="Run sftppath benchmarks (the default suite when no suite is named).",
        epilog=(
            "Options go before the suite name, e.g. "
            "`python benchmarks/bench.py --save --samples 5 sftp-batch`."
        ),
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="write the run as JSON to benchmarks/results/<name>.json",
    )
    parser.add_argument(
        "--name",
        help="result name for --save, without .json "
        "(default: <version>-py<major.minor>-<os>-<arch>[-<suite>])",
    )
    parser.add_argument(
        "--results-dir",
        help="directory for --save (default: the benchmarks/results directory beside this script)",
    )
    parser.add_argument(
        "--samples",
        type=_samples_arg,
        help=f"samples per metric for every case, 1-{MAX_SAMPLES} "
        "(default: each case's own count, 1-5)",
    )
    parser.add_argument("--note", help="free-text note stored in the saved JSON")
    subparsers = parser.add_subparsers(dest="command", metavar="suite")
    for suite_name, (_mode, help_text) in SUITES.items():
        subparsers.add_parser(suite_name, help=help_text)
    args = parser.parse_args(argv)
    _SAMPLES_OVERRIDE = args.samples

    run_suite(args.command)
    if args.save:
        save_result(
            args.command or "default",
            name=args.name,
            results_dir=args.results_dir,
            note=args.note,
        )


def run_suite(command):
    if command is None:
        main()
        return
    mode, help_text = SUITES[command]
    print(f"Running {command} ({help_text})...")
    if mode == "async":
        print_async_results(*benchmark_sftp_async(), markdown=True)
        return
    print_sftp_results(benchmark_sftp_backends(mode=mode), markdown=True)


if __name__ == "__main__":
    cli()
