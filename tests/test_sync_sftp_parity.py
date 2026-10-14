"""PathSyncer and SFTPPath parity against the loopback SFTP server, for a plain
path and an `sftp:` URI path, on both backends.

Sync onto an SFTP target keeps the previous version of a file until the new one
is complete; dry runs and `remove_missing` events on an SFTP tree; rename over
an existing file, unlink of a dangling link and readlink; the asyncssh file
object contract; the guards and `ignore_error` handling of asyncssh's recursive
copy; an interpreter that exits with a file left open; and the
`check-file-handle` round trip on both backends. Every test asserts what the
server's filesystem (`fixture_tree`, the server root) holds afterwards, or what
was sent.
"""

import errno
import hashlib
import inspect
import io
import os
import pathlib
import subprocess
import sys

import pytest

pytest.importorskip("asyncssh")

import asyncssh  # noqa: E402
import asyncssh.sftp as asftp  # noqa: E402
import sftppath  # noqa: E402
from asyncssh.packet import String  # noqa: E402
from pathapi.mempath import MemPath  # noqa: E402
from pathapi.sync import PathSyncer, SyncEvent  # noqa: E402
from sftp_support import make_backend, make_path  # noqa: E402
from sftppath import _checkfile  # noqa: E402
from sftppath._asyncssh import _file, _loop  # noqa: E402


def _size(entry):
    return entry.stat.st_size


def _remote(kind, flavor, server, tree, backends):
    backend = make_backend(kind, server)
    backends.append(backend)
    return make_path(flavor, server, backend), tree


@pytest.fixture
def remote(kind, flavor, sftp_server, fixture_tree, backends):
    """(path at the server root, local path of that root), over a backend of
    each library, as an `SFTPPath` and as an `sftp:` URI path."""
    return _remote(kind, flavor, sftp_server, fixture_tree, backends)


@pytest.fixture
def aremote(flavor, sftp_server, fixture_tree, backends):
    """`remote`, over asyncssh only."""
    return _remote("asyncssh", flavor, sftp_server, fixture_tree, backends)


def _symlink(target, link):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")


def _names(path):
    return sorted(p.name for p in path.iterdir())


def _link_text(flavor, link):
    """The text of a link as `readlink()` of the flavor returns it."""
    return link.as_posix() if flavor == "path" else link.path


# --- PathSyncer onto an SFTP target ------------------------------------------


class _ResetStream(io.RawIOBase):
    def __init__(self, data):
        self._data = data
        self._reads = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        self._reads += 1
        if self._reads > 1:
            raise ConnectionResetError(errno.ECONNRESET, "connection reset")
        chunk = self._data[:4]
        buffer[: len(chunk)] = chunk
        return len(chunk)


class _FlakyMemPath(MemPath):
    def _open(self, mode="r", buffering=-1):
        handle = super()._open(mode, buffering)
        if mode == "r" and self.name == "data.bin":
            return _ResetStream(handle.read())
        return handle


def test_sync_failed_copy_keeps_previous_remote_version(remote):
    root, local = remote
    (local / "dst").mkdir()
    (local / "dst" / "data.bin").write_bytes(b"previous good version")
    source = _FlakyMemPath("/src")
    source.mkdir()
    (source / "data.bin").write_bytes(b"NEW! content of another size")

    with pytest.raises(ConnectionResetError):
        PathSyncer(_size).sync(source, root / "dst")

    assert (local / "dst" / "data.bin").read_bytes() == b"previous good version"
    assert _names(local / "dst") == ["data.bin"]


def test_sync_replaces_changed_remote_file(remote):
    root, local = remote
    (local / "dst").mkdir()
    (local / "dst" / "a.txt").write_text("old")
    source = MemPath("/src")
    source.mkdir()
    (source / "a.txt").write_text("new, longer content")

    PathSyncer(_size).sync(source, root / "dst")

    assert (local / "dst" / "a.txt").read_text() == "new, longer content"
    assert _names(local / "dst") == ["a.txt"]


def test_sync_dry_run_onto_remote_new_subdirectory_changes_nothing(remote):
    root, local = remote
    (local / "dst").mkdir()
    (local / "dst" / "stale.txt").write_text("stale")
    source = MemPath("/src")
    (source / "newsub" / "deep").mkdir(parents=True)
    (source / "newsub" / "deep" / "f.txt").write_text("f")
    events = []

    def hook(source_entry, target_entry, event, dry_run):
        if event is SyncEvent.RemovedMissing:
            events.append(target_entry.path.name)

    PathSyncer(_size, remove_missing=True, hook=hook).sync(
        source, root / "dst", dry_run=True
    )

    assert _names(local / "dst") == ["stale.txt"]
    assert events == ["stale.txt"]


# --- rename, unlink, readlink ------------------------------------------------


def test_rename_over_existing_file_replaces_it(remote):
    # POSIX rename semantics through posix-rename@openssh.com, which the
    # loopback server advertises.
    root, local = remote
    (local / "old.txt").write_text("new content")
    (local / "new.txt").write_text("stale content")

    (root / "old.txt").rename(root / "new.txt")

    assert (local / "new.txt").read_text() == "new content"
    assert not (local / "old.txt").exists()


def test_rename_to_missing_target_still_works(remote):
    root, local = remote
    (local / "old.txt").write_text("content")

    (root / "old.txt").rename("fresh.txt")

    assert (local / "fresh.txt").read_text() == "content"
    assert not (local / "old.txt").exists()


def test_unlink_missing_ok_removes_dangling_symlink(remote):
    root, local = remote
    _symlink("release-1", local / "current")
    link = root / "current"
    assert link.is_symlink() and not link.exists()

    link.unlink(missing_ok=True)

    assert not os.path.lexists(local / "current")
    (root / "gone").unlink(missing_ok=True)
    with pytest.raises(FileNotFoundError):
        (root / "gone").unlink()


def test_symlink_force_repoints_dangling_link(remote):
    root, local = remote
    _symlink("release-1", local / "current")

    (root / "current").symlink_to("release-2", force=True)

    assert os.readlink(local / "current") == "release-2"


def test_readlink_absolute_target_keeps_the_server(remote, flavor):
    # A relative target cannot be read back through this chrooted asyncssh
    # server (it resolves links against its own cwd), so the relative case is
    # covered with a fake client in tests/test_sftp.py.
    root, local = remote
    (local / "releases" / "42").mkdir(parents=True)
    (local / "releases" / "42" / "f.txt").write_text("in 42")
    _symlink(str(local / "releases" / "42"), local / "current")

    link = (root / "current").readlink()

    assert _link_text(flavor, link) == "/releases/42"
    assert str(link).endswith("/releases/42")
    if flavor == "path":
        assert link.backend is root.backend
    else:
        assert link.source == root.source
    # The target is read from the server the link is on.
    assert (link / "f.txt").read_text() == "in 42"


# --- asyncssh file objects -------------------------------------------------------


def test_asyncssh_binary_readline_is_buffered(aremote, monkeypatch):
    # readline() is served from a buffer: one request per refill, not one per
    # byte.
    root, local = aremote
    (local / "big.log").write_bytes(b"x" * 1024 + b"\n" + b"tail\n")
    calls = []
    real_run = _loop._run

    def counting_run(coro, *args, **kwargs):
        calls.append(coro)
        return real_run(coro, *args, **kwargs)

    with (root / "big.log").open("rb") as handle:
        with monkeypatch.context() as patch:
            patch.setattr(_loop, "_run", counting_run)
            assert handle.readline() == b"x" * 1024 + b"\n"
            assert handle.readline() == b"tail\n"

    assert len(calls) < 5


def test_asyncssh_file_modes_are_reported(aremote):
    root, local = aremote
    (local / "f.bin").write_bytes(b"data")

    with (root / "f.bin").open("rb") as handle:
        assert handle.readable() and not handle.writable()
        with pytest.raises(io.UnsupportedOperation):
            handle.write(b"nope")
    with (root / "g.bin").open("wb") as handle:
        assert handle.writable() and not handle.readable()
        handle.write(b"written")
    assert (local / "g.bin").read_bytes() == b"written"
    with (root / "f.bin").open("r+b") as handle:
        assert handle.readable() and handle.writable()
    assert (local / "f.bin").read_bytes() == b"data"


def test_asyncssh_file_readinto_and_read_bytes(aremote):
    root, local = aremote
    payload = os.urandom(300 * 1024)
    (local / "blob.bin").write_bytes(payload)

    assert (root / "blob.bin").read_bytes() == payload
    with (root / "blob.bin").open("rb", buffering=0) as raw:
        buffer = bytearray(10)
        assert raw.readinto(buffer) == 10
        assert bytes(buffer) == payload[:10]
    if hasattr(hashlib, "file_digest"):
        with (root / "blob.bin").open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        assert digest == hashlib.sha256(payload).hexdigest()


class _FailingAsyncFile:
    async def close(self):
        raise asyncssh.SFTPFailure("close failed")


def test_asyncssh_close_errors_are_translated_and_close_the_object():
    raw = _file._SyncSFTPFile(_FailingAsyncFile(), timeout=5, mode="r")

    with pytest.raises(OSError):
        raw.close()

    assert raw.closed


_LEAKING_PATH = """
from sftppath import AsyncSSHBackend, SFTPPath

backend = AsyncSSHBackend(
    {host!r},
    {port},
    user="x",
    password="x",
    connect_opts={{"known_hosts": None}},
    ssh_config=None,
    timeout=None,
)
path = SFTPPath("/log.txt", backend=backend)
"""

_LEAKING_URI = """
from uripath import URIPath

from sftppath import AsyncSSHBackend
from sftppath.uri import SFTPURIBackend

path = URIPath(
    {url!r},
    backend=SFTPURIBackend(
        AsyncSSHBackend,
        connect_opts={{"known_hosts": None}},
        ssh_config=None,
        timeout=None,
    ),
)
"""

_LEAKING_READ = """
f = path.open("rb")
print(f.readline().decode().strip())
"""


# The child exits holding the handle, so the loopback server's handler task may be
# cancelled with the file still open on its side; that is the server, not the client.
@pytest.mark.filterwarnings(
    # The wording before 3.14 is "Exception ignored in: <_io.FileIO ...".
    r"ignore:Exception ignored (in|while finalizing file).? <_io\.FileIO name=.*log\.txt"
    ":pytest.PytestUnraisableExceptionWarning"
)
def test_asyncssh_file_left_open_does_not_hang_interpreter_exit(
    flavor, sftp_server, fixture_tree, tmp_path
):
    # A module-global handle is finalized at shutdown, when the loop thread can
    # no longer run: closing it must not wait for a reply. The child has no
    # request timeout, so a close that waited would never return.
    (fixture_tree / "log.txt").write_text("line\n")
    prelude = (
        _LEAKING_PATH.format(host=sftp_server.host, port=sftp_server.port)
        if flavor == "path"
        else _LEAKING_URI.format(url=sftp_server.url("log.txt"))
    )
    script = tmp_path / "leak_handle.py"
    script.write_text(prelude + _LEAKING_READ)
    env = os.environ.copy()
    # The child imports the same sftppath this test runs against.
    package_root = str(pathlib.Path(sftppath.__file__).parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(
        [package_root] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )

    # The timeout only ends a run that hangs.
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "line", result.stderr
    assert "TimeoutError" not in result.stderr


# --- asyncssh recursive copy -----------------------------------------------------


def _copy_tree(local):
    (local / "src" / "sub").mkdir(parents=True)
    (local / "src" / "1.txt").write_text("one")
    (local / "src" / "sub" / "2.txt").write_text("two")


@pytest.mark.parametrize("ignore_error", [True, False])
def test_asyncssh_recursive_copy_accepts_bool_ignore_error(aremote, ignore_error):
    root, local = aremote
    _copy_tree(local)
    (local / "dst" / "1.txt").mkdir(parents=True)  # forces a child error

    if ignore_error:
        (root / "src").copy(
            root / "dst", recursive=True, overwrite=True, ignore_error=True
        )
        assert (local / "dst" / "sub" / "2.txt").read_text() == "two"
    else:
        with pytest.raises(IsADirectoryError):
            (root / "src").copy(
                root / "dst", recursive=True, overwrite=True, ignore_error=False
            )


def test_asyncssh_recursive_copy_callable_ignore_error_is_notified(aremote):
    root, local = aremote
    _copy_tree(local)
    (local / "dst" / "1.txt").mkdir(parents=True)
    errors = []

    (root / "src").copy(
        root / "dst", recursive=True, overwrite=True, ignore_error=errors.append
    )

    assert len(errors) == 1 and isinstance(errors[0], IsADirectoryError)
    assert (local / "dst" / "sub" / "2.txt").read_text() == "two"


def test_asyncssh_recursive_copy_on_error_is_offered_each_failing_child(aremote):
    root, local = aremote
    _copy_tree(local)
    (local / "dst" / "1.txt").mkdir(parents=True)
    offered = []

    def on_error(error, path):
        offered.append((type(error), path.name))
        return True

    (root / "src").copy(root / "dst", recursive=True, overwrite=True, on_error=on_error)

    assert offered == [(IsADirectoryError, "1.txt")]
    assert (local / "dst" / "sub" / "2.txt").read_text() == "two"


def test_asyncssh_recursive_copy_stops_at_a_child_on_error_declines(aremote):
    root, local = aremote
    _copy_tree(local)
    (local / "dst" / "1.txt").mkdir(parents=True)

    with pytest.raises(IsADirectoryError):
        (root / "src").copy(
            root / "dst",
            recursive=True,
            overwrite=True,
            on_error=lambda error, path: False,
        )


def test_asyncssh_recursive_copy_into_own_subtree_raises_einval(aremote):
    root, local = aremote
    _copy_tree(local)

    with pytest.raises(OSError) as excinfo:
        (root / "src").copy(root / "src" / "sub" / "inner", recursive=True)

    assert excinfo.value.errno == errno.EINVAL
    assert not (local / "src" / "sub" / "inner").exists()
    assert _names(local / "src") == ["1.txt", "sub"]


def test_asyncssh_recursive_copy_keeps_symlinks_as_links(aremote):
    root, local = aremote
    _copy_tree(local)
    # Absolute link targets: this chrooted server cannot read back relative
    # ones (see test_readlink_absolute_target_keeps_the_server).
    _symlink(str(local / "src" / "1.txt"), local / "src" / "link.txt")
    _symlink(str(local / "src" / "sub"), local / "src" / "dirlink")

    (root / "src").copy(root / "dst", recursive=True, follow_symlinks=False)

    assert os.path.islink(local / "dst" / "link.txt")
    assert os.path.islink(local / "dst" / "dirlink")
    assert os.path.samefile(
        os.readlink(local / "dst" / "link.txt"), local / "src" / "1.txt"
    )
    assert os.path.samefile(
        os.readlink(local / "dst" / "dirlink"), local / "src" / "sub"
    )
    assert (local / "dst" / "sub" / "2.txt").read_text() == "two"


def test_asyncssh_copy_of_a_directory_link_without_following_copies_the_link(
    aremote,
):
    root, local = aremote
    _copy_tree(local)
    _symlink(str(local / "src"), local / "srclink")

    (root / "srclink").copy(root / "copied", recursive=True, follow_symlinks=False)

    assert os.path.islink(local / "copied")
    assert os.path.samefile(os.readlink(local / "copied"), local / "src")


# --- check-file-handle round trip ----------------------------------------------


class _CheckFileServer:
    """What the loopback server's `check-file-handle` handler received:
    `(algorithm-list, start-offset, length, block-size)` per request. With
    `refuse` set it answers SSH_FX_OP_UNSUPPORTED, as OpenSSH does."""

    def __init__(self):
        self.requests = []
        self.refuse = False


@pytest.fixture
def check_file_server(monkeypatch):
    """Adds `check-file-handle` to the loopback server for one test,
    answering with the draft's `"check-file"`-prefixed reply."""
    server = _CheckFileServer()

    class _Reply:
        def __init__(self, payload):
            self.payload = payload

        def encode(self, version):
            return self.payload

    async def process_check_file(handler, packet):
        handle = packet.get_string()
        algorithms = packet.get_string().decode()
        start = packet.get_uint64()
        length = packet.get_uint64()
        block_size = packet.get_uint32()
        packet.check_end()
        server.requests.append((algorithms, start, length, block_size))
        if server.refuse:
            raise asyncssh.SFTPOpUnsupported("check-file-handle not supported")
        file_obj = handler._file_handles[handle]
        algorithm = algorithms.split(",")[0]
        digest = hashlib.new(algorithm)
        offset = 0
        while True:
            chunk = handler._server.read(file_obj, offset, 64 * 1024)
            if inspect.isawaitable(chunk):
                chunk = await chunk
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
        return _Reply(String("check-file") + String(algorithm) + digest.digest())

    monkeypatch.setitem(
        asftp.SFTPServerHandler._packet_handlers,
        b"check-file-handle",
        process_check_file,
    )
    # On the server class only, never the shared base table: the client must
    # accept an EXTENDED_REPLY to this request on its own.
    monkeypatch.setattr(
        asftp.SFTPServerHandler,
        "_return_types",
        {
            **asftp.SFTPServerHandler._return_types,
            b"check-file-handle": asftp.FXP_EXTENDED_REPLY,
        },
    )
    return server


@pytest.mark.parametrize("algorithm", ["md5", "sha256"])
def test_checksum_is_the_server_side_digest(remote, check_file_server, algorithm):
    root, local = remote
    payload = os.urandom(200 * 1024)
    (local / "blob.bin").write_bytes(payload)

    digest = (root / "blob.bin").checksum(algorithm)

    assert digest == hashlib.new(algorithm, payload).hexdigest()
    # What the server received: the one algorithm, the whole file, one hash.
    assert check_file_server.requests == [(algorithm, 0, 0, 0)]
    assert (root / "blob.bin").supported_checksums() == frozenset(_checkfile.ALGORITHMS)
    # The asyncssh client registers the reply type on its own connection.
    assert b"check-file-handle" not in asftp.SFTPClientHandler._return_types


def test_checksum_refusal_costs_one_request_per_connection(remote, check_file_server):
    # A server without the extension (like OpenSSH) is asked once, not once
    # per file.
    root, local = remote
    check_file_server.refuse = True
    (local / "x.txt").write_text("x")
    (local / "y.txt").write_text("y")

    for name in ("a.txt", "x.txt", "y.txt"):
        with pytest.raises(NotImplementedError):
            (root / name).checksum()
    assert (root / "a.txt").supported_checksums() == frozenset()

    assert len(check_file_server.requests) == 1


def test_checksum_against_a_server_without_the_extension(remote):
    # The unmodified loopback server does not know the request at all.
    root, _local = remote

    with pytest.raises(NotImplementedError):
        (root / "a.txt").checksum()
    assert (root / "a.txt").supported_checksums() == frozenset()


def test_sync_between_remote_trees_compares_native_digests(remote, check_file_server):
    root, local = remote
    (local / "s").mkdir()
    (local / "d").mkdir()
    (local / "s" / "same.txt").write_text("unchanged")
    (local / "d" / "same.txt").write_text("unchanged")
    (local / "s" / "cfg.json").write_text('{"v": 2}')
    (local / "d" / "cfg.json").write_text('{"v": 1}')
    # Different mtimes: the size+mtime quick check settles neither file.
    for name in ("same.txt", "cfg.json"):
        os.utime(local / "d" / name, (1, 1))

    PathSyncer().sync(root / "s", root / "d")

    assert (local / "d" / "cfg.json").read_text() == '{"v": 2}'
    assert os.stat(local / "d" / "same.txt").st_mtime == 1  # left alone
    # Every digest asked for was the whole file's, by the PathSyncer default.
    assert set(check_file_server.requests) == {("md5", 0, 0, 0)}


def test_sync_sftp_to_sftp_without_native_checksum_still_detects_changes(remote):
    root, local = remote
    (local / "s").mkdir()
    (local / "d").mkdir()
    (local / "s" / "cfg.json").write_text('{"v": 2}')
    (local / "d" / "cfg.json").write_text('{"v": 1}')

    PathSyncer().sync(root / "s", root / "d")

    assert (local / "d" / "cfg.json").read_text() == '{"v": 2}'
    assert _names(local / "d") == ["cfg.json"]


def test_sync_leaves_identical_remote_symlink_alone(remote, flavor, monkeypatch):
    # Absolute targets: this chrooted server cannot read a relative one back
    # (see test_readlink_absolute_target_keeps_the_server).
    root, local = remote
    (local / "releases" / "42").mkdir(parents=True)
    (local / "src").mkdir()
    _symlink(str(local / "releases" / "42"), local / "src" / "current")
    syncer = PathSyncer(_size, follow_symlinks=False)
    syncer.sync(root / "src", root / "dst")
    assert _link_text(flavor, (root / "dst" / "current").readlink()) == "/releases/42"

    created = []
    monkeypatch.setattr(
        type(root), "_symlink_to", lambda self, *args: created.append(self)
    )
    events = []
    syncer._hook = lambda s, t, event, dry_run: events.append(event)
    syncer.sync(root / "src", root / "dst")

    assert created == []
    assert SyncEvent.Symlink not in events
    assert os.path.islink(local / "dst" / "current")
