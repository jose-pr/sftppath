"""The asyncssh backend: error translation, the SFTPAttrs -> st_*-shaped stat
adapter, the connect options, backend selection (the `SFTPPATH_BACKEND`
variable, `_default_backend_cls`, `backend=`), the lazy imports, and the
concurrent recursive copy and remove, against fake clients and against the
in-process server of the `sftp_server` fixture.

The end-to-end behaviour of a path (read, write, mkdir, rename, ...) on a live
server is covered by the contract suite in `test_contract.py`.
"""

import asyncio
import contextlib
import errno
import importlib.util
import os
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

asyncssh = pytest.importorskip("asyncssh")

import sftppath  # noqa: E402
from sftp_support import make_backend, make_path  # noqa: E402
from sftppath import (  # noqa: E402
    AsyncSSHBackend,
    BaseSFTPBackend,
    SFTPEndpoint,
    SFTPPath,
    _select,
)
from sftppath._asyncssh import _backend as backend_mod  # noqa: E402
from sftppath._asyncssh import _loop as loop_mod  # noqa: E402
from sftppath._asyncssh._attrs import _StatAdapter  # noqa: E402
from sftppath._asyncssh._client import _SyncSFTPClient  # noqa: E402
from sftppath._asyncssh._connect import _aconnect  # noqa: E402
from sftppath._asyncssh._translate import (  # noqa: E402
    _reraise_sftp_errors,
    _translate,
)
from sftppath._asyncssh._walk import _concurrent_copy, _concurrent_rm  # noqa: E402

_HAS_PARAMIKO = importlib.util.find_spec("paramiko") is not None
_needs_paramiko = pytest.mark.skipif(
    not _HAS_PARAMIKO, reason="paramiko not installed (asyncssh-only install)"
)

#: How long a test waits for a walk that is held on purpose before it fails
#: instead of hanging the run. It is a limit on a deadlock, not on speed.
_HANG_GUARD = 60

# --- error translation -------------------------------------------------


@pytest.mark.parametrize(
    "error, expected_type",
    [
        (asyncssh.SFTPNoSuchFile("no such file"), FileNotFoundError),
        (asyncssh.SFTPNoSuchPath("no such path"), FileNotFoundError),
        (asyncssh.SFTPFileAlreadyExists("exists"), FileExistsError),
        (asyncssh.SFTPPermissionDenied("denied"), PermissionError),
        (asyncssh.SFTPOpUnsupported("unsupported"), NotImplementedError),
        (asyncssh.SFTPFailure("generic v3 failure"), OSError),
    ],
)
def test_translate_maps_typed_errors(error, expected_type):
    result = _translate(error)
    assert isinstance(result, expected_type)


def test_translate_dir_not_empty_sets_enotempty_errno():
    result = _translate(asyncssh.SFTPDirNotEmpty("not empty"))
    assert isinstance(result, OSError)
    assert result.errno == errno.ENOTEMPTY


def test_reraise_sftp_errors_translates_without_chaining():
    @_reraise_sftp_errors
    def _raises():
        raise asyncssh.SFTPNoSuchFile("gone")

    with pytest.raises(FileNotFoundError) as excinfo:
        _raises()
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


def test_reraise_sftp_errors_passes_through_other_exceptions():
    @_reraise_sftp_errors
    def _raises():
        raise ValueError("unrelated")

    with pytest.raises(ValueError):
        _raises()


# --- stat adapter --------------------------------------------------------


def _attrs(**kwargs):
    return asyncssh.SFTPAttrs(**kwargs)


def test_stat_adapter_v3_style_combined_permissions():
    # v3 servers (and asyncssh's own bundled SFTPServer) pack the S_IFMT type
    # bits directly into `.permissions`.
    attrs = _attrs(type=asyncssh.FILEXFER_TYPE_REGULAR, permissions=0o100644, size=42)
    adapted = _StatAdapter(attrs)
    assert adapted.st_mode == 0o100644
    assert stat.S_ISREG(adapted.st_mode)
    assert adapted.st_size == 42


def test_stat_adapter_v4_style_bare_permissions_combined_with_type():
    # A v4+ server may report only the bare permission bits, with the type
    # carried separately in `.type`.
    attrs = _attrs(type=asyncssh.FILEXFER_TYPE_DIRECTORY, permissions=0o755)
    adapted = _StatAdapter(attrs)
    assert stat.S_ISDIR(adapted.st_mode)
    assert stat.S_IMODE(adapted.st_mode) == 0o755


def test_stat_adapter_symlink_type_bit():
    attrs = _attrs(type=asyncssh.FILEXFER_TYPE_SYMLINK, permissions=0o120777)
    adapted = _StatAdapter(attrs)
    assert stat.S_ISLNK(adapted.st_mode)


def test_stat_adapter_none_fields_default_to_zero_not_none():
    # SFTPAttrs' numeric fields can be None (not just absent) -- a naive
    # `getattr(attrs, 'size', 0)` would return None here, not 0.
    attrs = _attrs()
    adapted = _StatAdapter(attrs)
    assert adapted.st_size == 0
    assert adapted.st_uid == 0
    assert adapted.st_gid == 0
    assert adapted.st_nlink == 1
    assert adapted.st_atime == 0
    assert adapted.st_mtime == 0
    assert adapted.st_ctime == 0


def test_stat_adapter_filename_from_readdir_entry():
    attrs = _attrs(type=asyncssh.FILEXFER_TYPE_REGULAR, permissions=0o100644)
    adapted = _StatAdapter(attrs, filename="a.txt")
    assert adapted.filename == "a.txt"


def test_filestat_from_stat_adapter_roundtrip():
    from pathapi import FileStat

    attrs = _attrs(type=asyncssh.FILEXFER_TYPE_DIRECTORY, permissions=0o40755, size=0)
    adapted = _StatAdapter(attrs)
    fs = FileStat.from_stat(adapted)
    assert fs.is_dir()
    assert not fs.is_file()


# --- backend selection ----------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_backend_resolution(monkeypatch):
    # `_resolve_default_backend_cls()` caches its result module-globally:
    # isolate each test from that cache, from what an earlier test left in it,
    # and from the real process environment.
    monkeypatch.delenv(_select._ENV_VAR, raising=False)
    monkeypatch.setattr(_select, "_resolved_backend_cls", None)


def test_resolve_default_backend_auto_prefers_asyncssh():
    cls = _select._resolve_default_backend_cls(reload=True)
    assert cls is AsyncSSHBackend


@_needs_paramiko
def test_resolve_default_backend_explicit_paramiko(monkeypatch):
    monkeypatch.setenv(_select._ENV_VAR, "paramiko")
    cls = _select._resolve_default_backend_cls(reload=True)
    assert cls is sftppath.SFTPBackend


def test_resolve_default_backend_explicit_asyncssh(monkeypatch):
    monkeypatch.setenv(_select._ENV_VAR, "asyncssh")
    cls = _select._resolve_default_backend_cls(reload=True)
    assert cls is AsyncSSHBackend


def test_resolve_default_backend_invalid_value_raises(monkeypatch):
    monkeypatch.setenv(_select._ENV_VAR, "not-a-backend")
    with pytest.raises(ValueError, match="not-a-backend"):
        _select._resolve_default_backend_cls(reload=True)


def test_resolve_default_backend_asyncssh_unavailable_raises_importerror(monkeypatch):
    monkeypatch.setenv(_select._ENV_VAR, "asyncssh")
    monkeypatch.delitem(_select._BACKEND_REGISTRY, "asyncssh", raising=False)
    monkeypatch.setattr(_select, "_asyncssh_probed", True)  # skip the real probe
    with pytest.raises(ImportError, match=r"sftppath\[asyncssh\]"):
        _select._resolve_default_backend_cls(reload=True)


def test_resolve_default_backend_result_is_cached(monkeypatch):
    # asyncssh first, then flip the variable and confirm that a resolve
    # without `reload` returns the cached result.
    monkeypatch.setenv(_select._ENV_VAR, "asyncssh")
    first = _select._resolve_default_backend_cls(reload=True)
    monkeypatch.setenv(_select._ENV_VAR, "auto")
    second = _select._resolve_default_backend_cls()  # no reload -- cached
    assert first is second is AsyncSSHBackend


@pytest.mark.parametrize("value", [None, "auto", "asyncssh", "paramiko"])
def test_a_path_builds_the_backend_class_the_environment_names(monkeypatch, value):
    if value == "paramiko":
        pytest.importorskip("paramiko")
    if value is not None:
        monkeypatch.setenv(_select._ENV_VAR, value)

    path = SFTPPath("/a", host="Example", port=2222, user="u")

    expected = sftppath.SFTPBackend if value == "paramiko" else AsyncSSHBackend
    assert type(path.backend) is expected
    assert path.backend.endpoint == SFTPEndpoint("example", 2222, "u")


def test_a_path_with_an_unrecognized_environment_value_raises(monkeypatch):
    monkeypatch.setenv(_select._ENV_VAR, "not-a-backend")
    with pytest.raises(ValueError, match="not-a-backend"):
        SFTPPath("/a", host="h")


@_needs_paramiko
def test_default_backend_cls_class_attribute_wins_over_env(monkeypatch):
    monkeypatch.setenv(_select._ENV_VAR, "asyncssh")

    class _PinnedPath(SFTPPath):
        _default_backend_cls = sftppath.SFTPBackend

    path = _PinnedPath("/a", host="h")

    assert isinstance(path.backend, sftppath.SFTPBackend)
    assert type(SFTPPath("/a", host="h").backend) is AsyncSSHBackend


def test_explicit_backend_kwarg_wins_over_everything(monkeypatch):
    class _Unbuildable(BaseSFTPBackend):
        def __init__(self, *args, **kwargs):
            raise AssertionError("a backend was built although backend= was given")

    class _PinnedPath(SFTPPath):
        _default_backend_cls = _Unbuildable

    monkeypatch.setenv(_select._ENV_VAR, "asyncssh")
    explicit = AsyncSSHBackend("host")

    assert _PinnedPath("/a", backend=explicit).backend is explicit
    assert SFTPPath("/a", backend=explicit).backend is explicit


def test_asyncssh_getattr_reexports_the_backend():
    assert sftppath.AsyncSSHBackend is backend_mod.AsyncSSHBackend


@_needs_paramiko
def test_paramiko_getattr_reexports_the_backend():
    from sftppath import _paramiko

    assert sftppath.SFTPBackend is _paramiko.SFTPBackend


def test_asyncssh_getattr_unknown_name_raises_attributeerror():
    with pytest.raises(AttributeError):
        sftppath.NotARealAttribute
    with pytest.raises(AttributeError):
        sftppath.__getattr__("NotARealAttribute")


# --- lazy imports, in a child interpreter ---------------------------------


def _tree_of(module):
    """The directory a module's top-level package sits in."""
    return os.path.dirname(os.path.dirname(module.__file__))


def _child(*args, **env_overrides):
    """Run `python -c <args[0]> <args[1:]>` in a fresh interpreter that imports
    the same `sftppath` (and `pathapi`) this test runs against and has the
    PYTHONPATH of this process after them; `SFTPPATH_BACKEND` is unset unless
    `env_overrides` gives it."""
    import pathapi

    env = dict(os.environ)
    env.pop(_select._ENV_VAR, None)
    env.update(env_overrides)
    trees = [_tree_of(sftppath), _tree_of(pathapi)]
    if importlib.util.find_spec("uripath") is not None:
        import uripath

        trees.append(_tree_of(uripath))
    inherited = [env["PYTHONPATH"]] if env.get("PYTHONPATH") else []
    env["PYTHONPATH"] = os.pathsep.join([*dict.fromkeys(trees), *inherited])
    return subprocess.run(
        [sys.executable, "-c", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=_HANG_GUARD * 2,
    )


_LOADED_SCRIPT = """
import importlib, sys
importlib.import_module(sys.argv[1])
roots = {name.split(".")[0] for name in sys.modules}
print("loaded:", sorted(roots.intersection(sys.argv[2:])))
"""


@pytest.mark.parametrize(
    "module, forbidden",
    [
        ("sftppath", ["paramiko", "asyncssh", "uripath"]),
        ("sftppath.uri", ["paramiko", "asyncssh"]),
    ],
)
def test_importing_sftppath_loads_no_ssh_library(module, forbidden):
    if module == "sftppath.uri":
        pytest.importorskip("uripath")

    result = _child(_LOADED_SCRIPT, module, *forbidden)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "loaded: []"


_BUILD_SCRIPT = """
import sys
import sftppath
path = sftppath.SFTPPath("/etc/hosts", host="h", port=22, user="root")
roots = {name.split(".")[0] for name in sys.modules}
print(type(path.backend).__name__, sorted(roots.intersection(("paramiko", "asyncssh"))))
"""


@pytest.mark.parametrize(
    "value, backend_name, library",
    [
        ("asyncssh", "AsyncSSHBackend", "asyncssh"),
        ("paramiko", "SFTPBackend", "paramiko"),
    ],
)
def test_building_a_path_imports_only_the_library_of_its_backend(
    value, backend_name, library
):
    pytest.importorskip(library)

    result = _child(_BUILD_SCRIPT, **{_select._ENV_VAR: value})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{backend_name} ['{library}']"


_WITHOUT_PARAMIKO_SCRIPT = """
import sys
# Make `import paramiko` fail, as on an install without the paramiko extra.
sys.modules["paramiko"] = None
import sftppath
path = sftppath.SFTPPath("/etc/hosts", host="h", port=22, user="root")
assert type(path.backend).__name__ == "AsyncSSHBackend", type(path.backend)
assert path.backend.endpoint == sftppath.SFTPEndpoint("h", 22, "root")
print("OK")
"""


def test_a_path_builds_the_asyncssh_backend_without_paramiko_installed():
    """Runs with `paramiko` masked in `sys.modules`, so the guarantee holds
    even where paramiko happens to be installed."""
    result = _child(_WITHOUT_PARAMIKO_SCRIPT)

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# --- capability flags -------------------------------------------------


def test_asyncssh_backend_supports_lchmod_hardlink_and_tree():
    backend = AsyncSSHBackend("h")
    assert backend.supports_lchmod is True
    assert backend.supports_hardlink is True
    assert backend.supports_tree is True


@_needs_paramiko
def test_paramiko_backend_does_not_support_lchmod_hardlink_or_tree():
    assert sftppath.SFTPBackend.supports_lchmod is False
    assert sftppath.SFTPBackend.supports_hardlink is False
    assert sftppath.SFTPBackend.supports_tree is False


@pytest.mark.parametrize("target", ["/b", "path"])
def test_hardlink_to_raises_immediately_without_hardlink_support(target):
    class _NoHardlinkBackend(BaseSFTPBackend):
        def __init__(self):
            self.calls = []

        def client(self):
            self.calls.append("client")
            raise AssertionError("client() should not be called")

    backend = _NoHardlinkBackend()
    path = SFTPPath("/a", backend=backend)
    if target == "path":
        target = SFTPPath("/b", backend=backend)

    with pytest.raises(NotImplementedError):
        path.hardlink_to(target)
    assert backend.calls == []


# --- backend options ---------------------------------------------------------


def test_asyncssh_backend_has_max_concurrency():
    backend = AsyncSSHBackend("h", max_concurrency=16)
    assert backend.max_concurrency == 16


def test_asyncssh_backend_max_concurrency_defaults_to_16():
    backend = AsyncSSHBackend("h")
    assert backend.max_concurrency == 16
    assert backend.max_concurrency == AsyncSSHBackend.DEFAULT_MAX_CONCURRENCY
    assert "config" not in backend.connect_opts


def test_asyncssh_backend_accepts_connect_opts_and_sftp_version():
    backend = AsyncSSHBackend(
        "h",
        connect_opts={"config": None, "client_keys": None},
        max_concurrency=12,
        sftp_version=3,
    )
    assert backend.connect_opts == {"config": None, "client_keys": None}
    assert backend.max_concurrency == 12
    assert backend.sftp_version == 3


def test_asyncssh_backend_ssh_config_kwarg_is_backend_agnostic():
    backend = AsyncSSHBackend("h", ssh_config=None)
    assert backend.connect_opts["config"] is None
    backend = AsyncSSHBackend("h", ssh_config=("a", "b"))
    assert backend.connect_opts["config"] == ("a", "b")


def test_explicit_max_concurrency_overrides_default():
    backend = AsyncSSHBackend("h", connect_opts={"config": None}, max_concurrency=4)
    assert backend.max_concurrency == 4
    assert (
        AsyncSSHBackend(
            "h", connect_opts={"config": None}, max_concurrency=1
        ).max_concurrency
        == 1
    )
    # 0 is honored as given (the async helpers clamp to >=1 at use time via
    # `max(1, max_concurrency)`), not silently replaced by the default.
    assert (
        AsyncSSHBackend(
            "h", connect_opts={"config": None}, max_concurrency=0
        ).max_concurrency
        == 0
    )


def test_aconnect_merges_endpoint_credentials_and_connect_opts(monkeypatch):
    calls = {}

    class _FakeConn:
        async def start_sftp_client(self, *, sftp_version, path_errors):
            calls["sftp_version"] = sftp_version
            return object()

    async def _fake_connect(host, **kwargs):
        calls["host"] = host
        calls["kwargs"] = kwargs
        return _FakeConn()

    monkeypatch.setattr(asyncssh, "connect", _fake_connect)

    entry = asyncio.run(
        _aconnect(
            "host",
            2222,
            "user",
            "pass",
            connect_opts={
                "config": None,
                "client_keys": None,
                "agent_path": None,
                "username": "ignored",
            },
            sftp_version=4,
        )
    )
    assert isinstance(entry.client, _SyncSFTPClient)
    assert calls["host"] == "host"
    assert calls["kwargs"]["port"] == 2222
    assert calls["kwargs"]["config"] is None
    assert calls["kwargs"]["client_keys"] is None
    assert calls["kwargs"]["agent_path"] is None
    # Host-key verification stays on: nothing may inject known_hosts=None
    # (which disables asyncssh's check) unless the caller asked for it.
    assert "known_hosts" not in calls["kwargs"]
    assert calls["kwargs"]["username"] == "user"
    assert calls["kwargs"]["password"] == "pass"
    assert calls["sftp_version"] == 4


def test_aconnect_passes_explicit_known_hosts_opt_out_through(monkeypatch):
    calls = {}

    class _FakeConn:
        async def start_sftp_client(self, *, sftp_version, path_errors):
            return object()

    async def _fake_connect(host, **kwargs):
        calls["kwargs"] = kwargs
        return _FakeConn()

    monkeypatch.setattr(asyncssh, "connect", _fake_connect)
    backend = AsyncSSHBackend("host", connect_opts={"known_hosts": None})
    asyncio.run(_aconnect("host", 22, connect_opts=backend.connect_opts))
    assert "known_hosts" in calls["kwargs"]
    assert calls["kwargs"]["known_hosts"] is None


# --- fake clients for the concurrent walks -----------------------------------


def _sftp_attrs(is_dir):
    permissions = 0o40755 if is_dir else 0o100644
    file_type = (
        asyncssh.FILEXFER_TYPE_DIRECTORY if is_dir else asyncssh.FILEXFER_TYPE_REGULAR
    )
    return asyncssh.SFTPAttrs(type=file_type, permissions=permissions)


class _Requests:
    """What a fake asyncssh client does around each request: counts the ones in
    flight, fails or holds chosen ones, and can hold the requests of one kind
    until enough of them have arrived.

    `rendezvous=(kind, count)` makes every request of `kind` wait until `count`
    of them are in flight together, so a walk that cannot get there stalls and
    one that does is shown to allow it. `hang` is the set of `(kind, path)`
    pairs that never return on their own, `fail` the set that raise a failure
    status."""

    def __init__(self, *, rendezvous=None, hang=(), fail=()):
        self.rendezvous = rendezvous
        self.hang = set(hang)
        self.fail = set(fail)
        self.active = 0
        self.max_active = 0
        self.entered = []
        self._arrivals = 0
        self._reached = None

    @contextlib.asynccontextmanager
    async def around(self, kind, path):
        """One request: the fake's own work runs inside the `with`."""
        key = (kind, path)
        self.entered.append(key)
        if key in self.fail:
            raise asyncssh.SFTPFailure(path)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if key in self.hang:
                await asyncio.Event().wait()
            if self.rendezvous is not None and self.rendezvous[0] == kind:
                await self._meet(self.rendezvous[1])
            yield
        finally:
            self.active -= 1

    async def _meet(self, count):
        if self._reached is None:
            self._reached = asyncio.Event()
        self._arrivals += 1
        if self._arrivals >= count:
            self._reached.set()
        await self._reached.wait()


def _run_walk(coro):
    """Run `coro` on a fresh loop. A walk still waiting after `_HANG_GUARD`
    seconds for something the test never releases is cancelled and fails the
    test with an `AssertionError`, whatever it was meant to raise."""

    async def guarded():
        task = asyncio.ensure_future(coro)
        done, _ = await asyncio.wait({task}, timeout=_HANG_GUARD)
        if not done:
            task.cancel()
            await asyncio.wait({task}, timeout=_HANG_GUARD)
            raise AssertionError(f"the walk was still waiting after {_HANG_GUARD}s")
        return task.result()

    return asyncio.run(guarded())


class _FakeAsyncCopyFile:
    def __init__(self, client, path, mode):
        self.client = client
        self.path = path
        self.offset = 0

    async def read(self, size=-1):
        data = self.client.files[self.path][self.offset :]
        self.offset += len(data)
        return data

    async def write(self, data):
        self.client.files[self.path] = self.client.files.get(self.path, b"") + data

    async def close(self):
        pass


class _FakeAsyncCopyClient:
    def __init__(self, **requests):
        self.files = {
            "/src/a.txt": b"a",
            "/src/b.txt": b"b",
            "/src/c.txt": b"c",
            "/src/d.txt": b"d",
        }
        self.dirs = {"/src": ["a.txt", "b.txt", "c.txt", "d.txt"], "/dst": []}
        self.requests = _Requests(**requests)

    async def stat(self, path):
        async with self.requests.around("stat", path):
            if path in self.dirs:
                return _sftp_attrs(True)
            if path in self.files:
                return _sftp_attrs(False)
            raise asyncssh.SFTPNoSuchFile(path)

    async def lstat(self, path):
        return await self.stat(path)

    async def readdir(self, path):
        async with self.requests.around("readdir", path):
            return [SimpleNamespace(filename=name) for name in self.dirs[path]]

    async def mkdir(self, path, attrs):
        async with self.requests.around("mkdir", path):
            self.dirs[path] = []

    async def remove(self, path):
        async with self.requests.around("remove", path):
            self.files.pop(path, None)

    async def chmod(self, path, mode):
        async with self.requests.around("chmod", path):
            pass

    async def open(self, path, mode, encoding=None):
        async with self.requests.around("open", path):
            if "w" in mode:
                self.files[path] = b""
            return _FakeAsyncCopyFile(self, path, mode)


class _FakeAsyncPath:
    """The part of a path the walks use: its wire name, its last name and
    children."""

    def __init__(self, wirepath):
        self._wirepath = wirepath
        self.name = wirepath.rstrip("/").rsplit("/", 1)[-1]

    def __truediv__(self, name):
        return type(self)(f"{self._wirepath}/{name}")


def _copy(client, **kwargs):
    options = dict(
        overwrite=False,
        follow_symlinks=True,
        preserve_metadata=True,
        max_concurrency=4,
        ignore_error=None,
    )
    options.update(kwargs)
    return _run_walk(
        _concurrent_copy(
            _FakeAsyncPath("/src"), _FakeAsyncPath("/dst"), aclient=client, **options
        )
    )


def test_concurrent_copy_native_allows_max_concurrency_and_no_more():
    client = _FakeAsyncCopyClient(rendezvous=("stat", 3))

    _copy(client, max_concurrency=3)

    assert client.files["/dst/a.txt"] == b"a"
    assert client.files["/dst/d.txt"] == b"d"
    assert client.requests.max_active <= 3


def test_concurrent_copy_ignore_error_allows_partial_failure():
    client = _FakeAsyncCopyClient(fail={("stat", "/src/a.txt"), ("stat", "/src/c.txt")})
    errors = []

    _copy(client, ignore_error=errors.append)

    assert sorted(path for path in client.files if path.startswith("/dst/")) == [
        "/dst/b.txt",
        "/dst/d.txt",
    ]
    assert len(errors) == 2
    assert all(isinstance(error, OSError) for error in errors)


def test_concurrent_copy_fail_fast_cancels_queued_children():
    # One request at a time: the first child fails, the second is held in
    # flight and the others are queued behind it. The copy ends with the
    # failure, so it cannot be waiting for the held request.
    client = _FakeAsyncCopyClient(
        fail={("stat", "/src/a.txt")},
        hang={("stat", f"/src/{name}.txt") for name in "bcd"},
    )

    with pytest.raises(OSError):
        _copy(client, max_concurrency=1)

    requests = client.requests
    assert ("stat", "/src/b.txt") in requests.entered
    assert requests.active == 0
    assert ("stat", "/src/c.txt") not in requests.entered
    assert ("stat", "/src/d.txt") not in requests.entered
    assert [path for path in client.files if path.startswith("/dst/")] == []


def test_sftppath_copy_recursive_uses_concurrent_helper(monkeypatch):
    recorded = {}

    async def _fake_concurrent_copy(path, target, **kwargs):
        recorded["path"] = path
        recorded["target"] = target
        recorded["kwargs"] = kwargs

    run_timeouts = []

    def _fake_run(coro, timeout="unset"):
        run_timeouts.append(timeout)
        return asyncio.run(coro)

    aclient = object()
    monkeypatch.setattr(backend_mod, "_concurrent_copy", _fake_concurrent_copy)
    monkeypatch.setattr(loop_mod, "_run", _fake_run)
    monkeypatch.setattr(SFTPPath, "is_dir", lambda self: True)
    monkeypatch.setattr(
        AsyncSSHBackend, "client", lambda self: SimpleNamespace(_aclient=aclient)
    )

    src = SFTPPath("/src", backend=AsyncSSHBackend("host", max_concurrency=5))
    target = SFTPPath("/dst", backend=src.backend)
    monkeypatch.setattr(type(target), "exists", lambda self: False)
    monkeypatch.setattr(
        type(target),
        "mkdir",
        lambda self, mode=0o777, parents=False, exist_ok=False: None,
    )

    src.copy(target, recursive=True, overwrite=True)

    assert recorded["path"] is src
    assert recorded["target"] is target
    assert recorded["kwargs"]["max_concurrency"] == 5
    # The client was resolved on the calling thread, not inside the coroutine.
    assert recorded["kwargs"]["aclient"] is aclient
    # A whole-tree operation carries no wall-clock bound.
    assert run_timeouts == [None]


# --- concurrent remove tests ---------------------------------------------


class _FakeAsyncRmClient:
    def __init__(self, tree, **requests):
        self.tree = tree
        self.requests = _Requests(**requests)
        self.removed = []
        self.rmdirs = []

    async def stat(self, path):
        async with self.requests.around("stat", path):
            if path not in self.tree:
                raise asyncssh.SFTPNoSuchFile(path)
            return _sftp_attrs(self.tree[path] is not None)

    async def lstat(self, path):
        return await self.stat(path)

    async def readdir(self, path):
        async with self.requests.around("readdir", path):
            children = self.tree[path] or []
            return [SimpleNamespace(filename=name) for name in children]

    async def remove(self, path):
        async with self.requests.around("remove", path):
            self.removed.append(path)

    async def rmdir(self, path):
        async with self.requests.around("rmdir", path):
            self.rmdirs.append(path)


def _rm(client, **kwargs):
    options = dict(max_concurrency=4, missing_ok=False, on_error=None)
    options.update(kwargs)
    return _run_walk(_concurrent_rm(_FakeAsyncPath("/root"), aclient=client, **options))


def test_concurrent_rm_uses_native_asyncssh_calls_and_allows_max_concurrency():
    tree = {
        "/root": ["a.txt", "b.txt", "c.txt", "d.txt"],
        "/root/a.txt": None,
        "/root/b.txt": None,
        "/root/c.txt": None,
        "/root/d.txt": None,
    }
    client = _FakeAsyncRmClient(tree, rendezvous=("remove", 2))

    _rm(client, max_concurrency=2)

    assert set(client.removed) == {
        "/root/a.txt",
        "/root/b.txt",
        "/root/c.txt",
        "/root/d.txt",
    }
    assert client.rmdirs == ["/root"]
    assert client.requests.max_active <= 2


def test_concurrent_rm_ignore_error_receives_error_and_path():
    tree = {
        "/root": ["ok.txt", "bad.txt"],
        "/root/ok.txt": None,
        "/root/bad.txt": None,
    }
    client = _FakeAsyncRmClient(tree, fail={("remove", "/root/bad.txt")})
    ignored = []

    _rm(
        client,
        on_error=lambda error, path: ignored.append((type(error), path._wirepath))
        or True,
    )

    assert client.removed == ["/root/ok.txt"]
    assert ignored == [(OSError, "/root/bad.txt")]
    assert client.rmdirs == ["/root"]


def test_concurrent_rm_fail_fast_cancels_queued_children():
    # One request at a time: the first child fails to be removed, the second
    # is held in flight and the others are queued behind it. The walk ends with
    # the failure, so it cannot be waiting for the held request.
    tree = {
        "/root": ["bad.txt", "a.txt", "b.txt", "c.txt"],
        "/root/bad.txt": None,
        "/root/a.txt": None,
        "/root/b.txt": None,
        "/root/c.txt": None,
    }
    client = _FakeAsyncRmClient(
        tree,
        fail={("remove", "/root/bad.txt")},
        hang={("remove", f"/root/{name}.txt") for name in "abc"},
    )

    with pytest.raises(OSError):
        _rm(client, max_concurrency=1)

    assert ("remove", "/root/a.txt") in client.requests.entered
    assert client.requests.active == 0
    assert ("stat", "/root/b.txt") not in client.requests.entered
    assert client.removed == []
    assert client.rmdirs == []


def test_concurrent_rm_missing_root_honors_missing_ok():
    client = _FakeAsyncRmClient({})

    _rm(client, missing_ok=True)

    assert client.removed == []
    assert client.rmdirs == []


def test_sftppath_rm_recursive_uses_concurrent_helper(monkeypatch):
    recorded = {}

    async def _fake_concurrent_rm(path, **kwargs):
        recorded["path"] = path
        recorded["kwargs"] = kwargs

    run_timeouts = []

    def _fake_run(coro, timeout="unset"):
        run_timeouts.append(timeout)
        return asyncio.run(coro)

    aclient = object()
    monkeypatch.setattr(backend_mod, "_concurrent_rm", _fake_concurrent_rm)
    monkeypatch.setattr(loop_mod, "_run", _fake_run)
    monkeypatch.setattr(
        AsyncSSHBackend, "client", lambda self: SimpleNamespace(_aclient=aclient)
    )

    src = SFTPPath("/src", backend=AsyncSSHBackend("host", max_concurrency=6))
    src.rm(recursive=True, missing_ok=True, ignore_error=True)

    assert recorded["path"] is src
    assert recorded["kwargs"]["max_concurrency"] == 6
    assert recorded["kwargs"]["missing_ok"] is True
    assert recorded["kwargs"]["on_error"](ValueError("ignored"), src) is True
    assert recorded["kwargs"]["aclient"] is aclient
    assert run_timeouts == [None]


def _recursive_rm_with(monkeypatch, **policies):
    """Call `SFTPPath.rm(recursive=True, **policies)` on an asyncssh backend
    whose native walker and generic removal are both recorded."""
    from pathapi import Path

    native = []
    generic = []

    async def _fake_concurrent_rm(path, **kwargs):
        native.append(kwargs)

    def _fake_generic_rm(self, *args, **kwargs):
        generic.append(kwargs)

    monkeypatch.setattr(backend_mod, "_concurrent_rm", _fake_concurrent_rm)
    monkeypatch.setattr(
        loop_mod, "_run", lambda coro, timeout="unset": asyncio.run(coro)
    )
    monkeypatch.setattr(
        AsyncSSHBackend, "client", lambda self: SimpleNamespace(_aclient=object())
    )
    monkeypatch.setattr(Path, "rm", _fake_generic_rm)
    src = SFTPPath("/src", backend=AsyncSSHBackend("host", max_concurrency=2))
    src.rm(recursive=True, **policies)
    return native, generic


@pytest.mark.parametrize(
    "policies",
    [
        {},
        {"follow_symlinks": False, "follow_binds": False},
        {"follow_symlinks": False, "follow_binds": None},
        {"follow_binds": lambda path: True},
    ],
)
def test_sftppath_rm_takes_the_native_walk_for_the_default_symlink_policy(
    monkeypatch, policies
):
    native, generic = _recursive_rm_with(monkeypatch, **policies)
    assert len(native) == 1
    assert generic == []


@pytest.mark.parametrize("policy", [True, None, lambda path: False])
def test_sftppath_rm_walks_generically_for_any_other_symlink_policy(
    monkeypatch, policy
):
    native, generic = _recursive_rm_with(
        monkeypatch, follow_symlinks=policy, follow_binds=None
    )
    assert native == []
    assert len(generic) == 1
    assert generic[0]["follow_symlinks"] is policy
    assert generic[0]["follow_binds"] is None


# --- concurrent recursive copy against a real server --------------------------
# The fakes above hold one flat directory. These run the native fan-out
# (`_concurrent_copy`) over conftest's in-process asyncssh server, whose root is
# `fixture_tree`, for an `SFTPPath` and an `SFTPURIPath`, and read the result
# back from local disk.


@pytest.fixture
def asyncssh_tree(flavor, sftp_server, fixture_tree, backends):
    backend = make_backend("asyncssh", sftp_server)
    backends.append(backend)
    return make_path(flavor, sftp_server, backend), fixture_tree


def _bounded(call, timeout=_HANG_GUARD):
    """Run `call` on a worker thread: a fan-out deadlock fails the test
    instead of hanging the run (sync calls made on the asyncssh bridge loop can
    deadlock)."""
    outcome = {}

    def run():
        try:
            outcome["value"] = call()
        except BaseException as error:  # re-raised on the test thread
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), f"the call did not return within {timeout}s"
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _local_tree(root):
    return {
        p.relative_to(root).as_posix(): (None if p.is_dir() else p.read_bytes())
        for p in root.rglob("*")
    }


def test_concurrent_copy_nested_tree_and_overwrite_real_server(asyncssh_tree):
    root, local = asyncssh_tree
    (local / "sub" / "nested" / "deeper").mkdir()
    (local / "sub" / "nested" / "deeper" / "e.bin").write_bytes(b"\x00e" * 3000)

    _bounded(lambda: (root / "sub").copy(root / "copy", recursive=True))
    assert _local_tree(local / "copy") == _local_tree(local / "sub")

    # overwrite=True descends into the existing tree: an existing nested
    # directory is reused and an existing file replaced.
    (local / "sub" / "nested" / "d.py").write_bytes(b"changed")
    (local / "copy" / "stale.txt").write_bytes(b"kept")
    _bounded(lambda: (root / "sub").copy(root / "copy", recursive=True, overwrite=True))
    assert (local / "copy" / "nested" / "d.py").read_bytes() == b"changed"
    assert (local / "copy" / "nested" / "deeper" / "e.bin").read_bytes() == (
        b"\x00e" * 3000
    )
    assert (local / "copy" / "stale.txt").read_bytes() == b"kept"


def test_concurrent_copy_type_conflicts_in_existing_tree_real_server(asyncssh_tree):
    root, local = asyncssh_tree
    target = local / "conflict"
    target.mkdir()
    (target / "nested").write_bytes(b"a file where src has a directory")
    (target / "c.py").mkdir()  # a directory where src has a file

    errors = []
    _bounded(
        lambda: (root / "sub").copy(
            root / "conflict",
            recursive=True,
            overwrite=True,
            ignore_error=errors.append,
        )
    )
    assert sorted(type(error).__name__ for error in errors) == [
        "FileExistsError",
        "IsADirectoryError",
    ]
    # Neither conflicting entry was replaced.
    assert (target / "nested").read_bytes() == b"a file where src has a directory"
    assert (target / "c.py").is_dir()

    with pytest.raises((FileExistsError, IsADirectoryError)):
        _bounded(
            lambda: (root / "sub").copy(
                root / "conflict", recursive=True, overwrite=True
            )
        )


def _link_target(link, flavor):
    """The target of `link` as the server stores it."""
    target = link.readlink()
    return target._wirepath if flavor == "path" else target.path


def test_concurrent_copy_symlink_child_uses_sync_fallback_real_server(
    asyncssh_tree, flavor
):
    root, local = asyncssh_tree
    real = local / "sub" / "c.py"
    try:
        # Absolute: asyncssh's chrooted SFTPServer cannot map a relative link
        # target back into the chroot (readlink raises "File not found").
        os.symlink(str(real), local / "sub" / "link.py")
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")
    try:
        if _link_target(root / "sub" / "link.py", flavor) != "/sub/c.py":
            raise OSError("unexpected link target")
    except OSError as error:
        pytest.skip(f"this server does not serve symlinks: {error}")

    _bounded(
        lambda: (root / "sub").copy(
            root / "linked_copy", recursive=True, follow_symlinks=False
        )
    )
    copied = local / "linked_copy" / "link.py"
    assert copied.is_symlink()
    # The same target, compared as the server reports it: a local readlink
    # on Windows adds an extended-length path prefix.
    assert _link_target(root / "linked_copy" / "link.py", flavor) == "/sub/c.py"
    assert copied.read_bytes() == b"c"
    assert (local / "linked_copy" / "nested" / "d.py").read_bytes() == b"d"


# --- errno and filename of translated errors -----------------------------------


@pytest.mark.parametrize(
    "error, expected_type, code",
    [
        (asyncssh.SFTPNoSuchFile("No such file"), FileNotFoundError, "ENOENT"),
        (asyncssh.SFTPNoSuchPath("No such path"), FileNotFoundError, "ENOENT"),
        (asyncssh.SFTPFileAlreadyExists("exists"), FileExistsError, "EEXIST"),
        (asyncssh.SFTPPermissionDenied("denied"), PermissionError, "EACCES"),
        (asyncssh.SFTPDirNotEmpty("not empty"), OSError, "ENOTEMPTY"),
    ],
)
def test_translate_sets_errno_and_filename(error, expected_type, code):
    result = _translate(error, "/srv/a.txt")
    assert type(result) is expected_type
    assert result.errno == getattr(errno, code)
    assert result.filename == "/srv/a.txt"
    assert result.strerror == str(error)


def test_translate_bare_failure_keeps_errno_unset_but_names_the_path():
    # SFTPv3 has no ENOTEMPTY/EISDIR status: `SFTPPath` reads a missing errno
    # as "consult the entry", as it does on the paramiko backend.
    result = _translate(asyncssh.SFTPFailure("failure"), "/srv/d")
    assert type(result) is OSError
    assert result.errno is None
    assert result.filename == "/srv/d"
    assert result.strerror == "failure"
    assert "/srv/d" in str(result)
    assert _translate(asyncssh.SFTPFailure("failure")).errno is None


def test_client_method_errors_name_their_paths():
    class _AClient:
        async def stat(self, path):
            raise asyncssh.SFTPNoSuchFile("No such file")

        async def rename(self, old, new):
            raise asyncssh.SFTPPermissionDenied("denied")

    client = _SyncSFTPClient(_AClient())
    with pytest.raises(FileNotFoundError) as missing:
        client.stat("/srv/missing.txt")
    assert missing.value.errno == errno.ENOENT
    assert missing.value.filename == "/srv/missing.txt"
    with pytest.raises(PermissionError) as denied:
        client.rename("/srv/a", "/srv/b")
    assert (denied.value.filename, denied.value.filename2) == ("/srv/a", "/srv/b")


def test_asyncssh_backend_errors_carry_errno_and_filename(
    flavor, sftp_server, backends
):
    backend = make_backend("asyncssh", sftp_server)
    backends.append(backend)
    root = make_path(flavor, sftp_server, backend)

    with pytest.raises(FileNotFoundError) as missing:
        (root / "missing.txt").read_bytes()
    assert missing.value.errno == errno.ENOENT
    assert missing.value.filename == str(root / "missing.txt")
    with pytest.raises(FileExistsError) as exists:
        (root / "sub").mkdir()
    assert exists.value.errno == errno.EEXIST
    assert exists.value.filename == str(root / "sub")
