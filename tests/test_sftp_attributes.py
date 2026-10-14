"""Attributes, file objects, native checksums and the exit of an SFTP session,
on both backends and both path flavors: what the server is asked for, and what
the caller gets back.

The loopback server counts the requests it receives and records the
attributes of a `setstat`, so every assertion is about what went over the
wire. Nothing here leaves the machine; the home directory is an empty one.
"""

import copy
import os
import pathlib
import pickle
import subprocess
import sys
import threading
import weakref

import pytest

asyncssh = pytest.importorskip("asyncssh")

from sftppath import DEFAULT_SSH_CONFIG  # noqa: E402
from sftppath import _checkfile, _exit  # noqa: E402

from sftp_loopback import SFTPServerAddress, counting_server_class  # noqa: E402
from sftp_support import make_backend, make_path  # noqa: E402

try:
    import paramiko
except ImportError:  # asyncssh-only install
    paramiko = None

needs_paramiko = pytest.mark.skipif(paramiko is None, reason="paramiko not installed")

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert pathlib.Path.home() == home
    return home


def built_path(flavor, kind, server, rel, monkeypatch):
    """A path whose backend is built from the endpoint alone, with the default
    host-key checking (the `known_hosts` of the home directory) and the
    default ssh_config; only the connect options that keep the SSH agent and
    keys out. Its backend is `path.backend`."""
    from sftppath import AsyncSSHBackend, SFTPBackend, SFTPPath

    if kind == "paramiko":
        backend_class = SFTPBackend
        options = {"connect_opts": {"allow_agent": False, "look_for_keys": False}}
    else:
        backend_class = AsyncSSHBackend
        options = {}
    if flavor == "path":
        monkeypatch.setattr(SFTPPath, "_default_backend_cls", backend_class)
        return SFTPPath("/" + rel, **server.options(), **options)
    uripath = pytest.importorskip("uripath")
    from sftppath.uri import SFTPURIBackend

    return uripath.URIPath(
        server.url(rel), backend=SFTPURIBackend(backend_class, **options)
    )


# --- owners: an unknown id is never sent as 0 ----------------------------------


class _SetstatRecorder:
    """A server class that records the attributes of every `setstat` and
    leaves the file alone."""

    def __init__(self, sftp_wire):
        self.seen = []
        recorder = self
        base = counting_server_class(sftp_wire)

        class _Server(base):
            def setstat(self, path, attrs):
                sftp_wire.record("setstat", (path,))
                recorder.seen.append(
                    (attrs.uid, attrs.gid, attrs.owner, attrs.group, attrs.permissions)
                )

        self.server_class = _Server


def _reports_owner(owner, group):
    """A `stat` answer that names the owner and the group, as a v4 server
    does, and carries no numeric ids."""

    def after(result, path):
        return asyncssh.SFTPAttrs(
            type=asyncssh.FILEXFER_TYPE_REGULAR,
            permissions=result.st_mode,
            size=result.st_size,
            mtime=int(result.st_mtime),
            atime=int(result.st_atime),
            owner=owner,
            group=group,
        )

    return after


@pytest.fixture
def v4(flavor, sftp_root, sftp_wire, sftp_loopbacks, backends):
    recorder = _SetstatRecorder(sftp_wire)
    server = sftp_loopbacks(
        sftp_root, sftp_wire, sftp_version=4, server_class=recorder.server_class
    )
    (sftp_root / "a.txt").write_bytes(b"hello")
    backend = make_backend("asyncssh", server, sftp_version=4)
    backends.append(backend)
    return make_path(flavor, server, backend, "a.txt"), recorder


def test_a_partial_chown_against_a_server_that_names_owners_is_refused(v4, sftp_wire):
    path, recorder = v4
    sftp_wire.after["stat"] = _reports_owner("alice", "staff")

    with pytest.raises(NotImplementedError, match="uid"):
        path.chown(None, 4242)
    with pytest.raises(NotImplementedError, match="gid"):
        path.chown(4242, None)

    assert recorder.seen == []


def test_a_full_chown_against_a_server_that_names_owners_is_sent(v4, sftp_wire):
    path, recorder = v4
    sftp_wire.after["stat"] = _reports_owner("alice", "staff")

    path.chown(7, 8)

    assert [(owner, group) for _u, _g, owner, group, _p in recorder.seen] == [
        ("7", "8")
    ]


def test_a_numeric_owner_name_is_the_id(v4, sftp_wire):
    path, recorder = v4
    sftp_wire.after["stat"] = _reports_owner("1000", "1001")

    stat = path.stat()
    path.chown(None, 4242)

    assert (stat.st_uid, stat.st_gid) == (1000, 1001)
    assert [(owner, group) for _u, _g, owner, group, _p in recorder.seen] == [
        ("1000", "4242")
    ]


def test_a_partial_chown_against_a_v3_server_keeps_the_known_owner(
    flavor, sftp_root, sftp_wire, sftp_loopbacks, kind, backends
):
    recorder = _SetstatRecorder(sftp_wire)
    server = sftp_loopbacks(
        sftp_root, sftp_wire, sftp_version=3, server_class=recorder.server_class
    )
    (sftp_root / "a.txt").write_bytes(b"hello")
    backend = make_backend(kind, server)
    backends.append(backend)
    path = make_path(flavor, server, backend, "a.txt")
    stat = path.stat()

    path.chown(None, 4242)

    assert [(uid, gid) for uid, gid, _o, _g, _p in recorder.seen] == [
        (stat.st_uid, 4242)
    ]


# --- file objects ----------------------------------------------------------------


@pytest.fixture
def served(flavor, kind, sftp_root, sftp_loopback, backends):
    backend = make_backend(kind, sftp_loopback)
    backends.append(backend)
    return make_path(flavor, sftp_loopback, backend), sftp_root


def test_write_bytes_returns_the_number_of_bytes(served):
    path, sftp_root = served

    assert (path / "w.bin").write_bytes(b"12345") == 5
    assert (path / "t.txt").write_text("héllo") == 5
    assert (sftp_root / "w.bin").read_bytes() == b"12345"


def test_write_of_an_exclusive_file_returns_the_count(served):
    path, sftp_root = served

    with (path / "x.bin").open("xb") as handle:
        assert handle.write(b"hi") == 2
        assert handle.write(bytearray(b"abc")) == 3
    assert (sftp_root / "x.bin").read_bytes() == b"hiabc"


def test_a_file_without_a_descriptor_says_so(served):
    path, _root = served
    with (path / "f.bin").open("wb") as handle:
        with pytest.raises(OSError):
            handle.fileno()


def test_truncate_cuts_the_remote_file(served):
    path, sftp_root = served
    (sftp_root / "t.bin").write_bytes(b"0123456789")

    with (path / "t.bin").open("r+b") as handle:
        assert handle.truncate(4) == 4
    assert (sftp_root / "t.bin").read_bytes() == b"0123"

    with (path / "t.bin").open("r+b") as handle:
        handle.seek(2)
        assert handle.truncate() == 2
    assert (sftp_root / "t.bin").read_bytes() == b"01"


def test_truncate_after_a_buffered_write_cuts_what_was_written(served):
    path, sftp_root = served

    with (path / "t.bin").open("w+b") as handle:
        handle.write(b"abcdefgh")
        handle.truncate(3)
    assert (sftp_root / "t.bin").read_bytes() == b"abc"


# --- the default sentinels survive a copy and a pickle ---------------------------


def test_the_default_ssh_config_is_still_itself_after_a_pickle_or_a_copy():
    for convert in (
        copy.copy,
        copy.deepcopy,
        lambda value: pickle.loads(pickle.dumps(value)),
    ):
        assert convert(DEFAULT_SSH_CONFIG) is DEFAULT_SSH_CONFIG


@needs_paramiko
def test_a_copied_or_pickled_paramiko_backend_still_reads_the_default_config():
    from sftppath import SFTPBackend, _paramiko

    backend = SFTPBackend("somehost")
    for clone in (
        copy.deepcopy(backend),
        pickle.loads(pickle.dumps(backend)),
    ):
        assert clone.ssh_config is DEFAULT_SSH_CONFIG
        assert clone.known_hosts is _paramiko._DEFAULT_KNOWN_HOSTS
        assert clone.opts()["hostname"] == "somehost"
        assert clone._known_hosts_files() == []


def test_a_copied_or_pickled_asyncssh_backend_keeps_its_ssh_config_choice():
    from sftppath import AsyncSSHBackend

    default = AsyncSSHBackend("somehost")
    without = AsyncSSHBackend("somehost", ssh_config=None)
    for convert in (
        copy.deepcopy,
        lambda backend: pickle.loads(pickle.dumps(backend)),
    ):
        assert "config" not in convert(default).connect_opts
        assert convert(without).connect_opts["config"] is None


def test_a_pickled_or_copied_path_stats_against_the_server(
    flavor, kind, sftp_root, sftp_loopback, home, monkeypatch, backends
):
    (sftp_root / "a.txt").write_bytes(b"hello")
    (home / ".ssh" / "known_hosts").write_text(sftp_loopback.known_hosts_line())
    path = built_path(flavor, kind, sftp_loopback, "a.txt", monkeypatch)
    backends.append(path.backend)
    unused = built_path(flavor, kind, sftp_loopback, "a.txt", monkeypatch)
    backends.append(unused.backend)

    for clone in (
        pickle.loads(pickle.dumps(path)),
        pickle.loads(pickle.dumps(unused)),
        copy.deepcopy(path),
    ):
        backends.append(clone.backend)
        assert clone.stat().st_size == 5
        assert clone.read_bytes() == b"hello"


# --- native checksums --------------------------------------------------------------

#: The bytes of the digest of each algorithm the check-file draft names.
_SIZES = {
    "md5": 16,
    "sha1": 20,
    "sha224": 28,
    "sha256": 32,
    "sha384": 48,
    "sha512": 64,
    "crc32": 4,
}
_DIGESTS = {name: bytes(range(size)) for name, size in _SIZES.items()}


def _reply(algorithm, digest=None):
    import struct

    def string(text):
        data = text.encode()
        return struct.pack(">I", len(data)) + data

    return string(algorithm) + (_DIGESTS[algorithm] if digest is None else digest)


class _Answers:
    """A paramiko-shaped client whose extension request is answered by
    `answer(algorithm, call_number)`: a payload, or an exception to raise."""

    class _Handle:
        handle = b"h"

        def close(self):
            pass

    def __init__(self, answer):
        self.answer = answer
        #: The algorithm of each extension request, and the whole of each.
        self.requests = []
        self.sent = []

    def open(self, path, mode, buffering=-1):
        return self._Handle()

    def _request(self, command, extension, handle, algorithm, *rest):
        import paramiko.sftp as paramiko_sftp

        self.requests.append(algorithm)
        self.sent.append((command, extension, handle, algorithm, rest))
        outcome = self.answer(algorithm, len(self.requests))
        if isinstance(outcome, BaseException):
            raise outcome
        return paramiko_sftp.CMD_EXTENDED_REPLY, paramiko_message(outcome)


def paramiko_message(payload):
    from paramiko.message import Message

    message = Message(payload)
    return message


@pytest.fixture
def checksum_path(flavor, monkeypatch):
    """`path.checksum()` over a fake paramiko client: the real backend code,
    the real reply parser, a client that answers as the test says."""
    pytest.importorskip("paramiko")
    from sftppath import SFTPBackend

    monkeypatch.setattr(_checkfile, "_SUPPORT_CACHE", weakref.WeakKeyDictionary())

    def build(answer):
        client = _Answers(answer)
        backend = SFTPBackend("host")
        monkeypatch.setattr(SFTPBackend, "client", lambda self: client)
        path = make_path(flavor, SFTPServerAddress("host", 22), backend, "a.txt")
        return path, client

    return build


def test_a_refused_algorithm_does_not_switch_off_the_others(checksum_path):
    def answer(algorithm, _call):
        if algorithm == "md5":
            return NotImplementedError("md5 is disabled")
        return _reply(algorithm)

    path, client = checksum_path(answer)

    with pytest.raises(NotImplementedError):
        path.checksum("md5")
    assert path.checksum("sha256") == _DIGESTS["sha256"].hex()
    with pytest.raises(NotImplementedError):
        path.checksum("md5")

    # md5 was asked for once, sha256 once: the refusal is remembered per
    # algorithm and says nothing about the others.
    assert client.requests == ["md5", "sha256"]


def test_a_connection_that_produced_a_digest_is_never_reported_empty(checksum_path):
    def answer(algorithm, _call):
        if algorithm == "md5":
            return NotImplementedError("md5 is disabled")
        return _reply(algorithm)

    path, client = checksum_path(answer)

    assert path.checksum("sha256") == _DIGESTS["sha256"].hex()
    supported = path.supported_checksums()
    with pytest.raises(NotImplementedError):
        path.checksum("md5")

    assert "sha256" in supported
    assert "md5" not in path.supported_checksums()
    assert path.supported_checksums()
    assert client.requests == ["sha256", "md5"]


def test_a_failure_for_one_file_refuses_nothing(checksum_path):
    def answer(algorithm, call):
        if call == 1:
            return OSError("I/O error reading this one file")
        return _reply(algorithm)

    path, client = checksum_path(answer)

    with pytest.raises(NotImplementedError):
        path.checksum("md5")
    assert path.checksum("md5") == _DIGESTS["md5"].hex()
    assert client.requests == ["md5", "md5"]


def test_a_refusal_of_the_whole_extension_costs_one_request_per_algorithm(
    checksum_path,
):
    path, client = checksum_path(
        lambda algorithm, _call: NotImplementedError("operation unsupported")
    )

    for _ in range(3):
        with pytest.raises(NotImplementedError):
            path.checksum("md5")
    assert path.supported_checksums() == frozenset()
    assert client.requests == ["md5"]


def test_the_request_names_the_handle_one_algorithm_and_the_whole_file(checksum_path):
    import paramiko.sftp as paramiko_sftp

    path, client = checksum_path(lambda algorithm, _call: _reply(algorithm))

    path.checksum("sha256")

    ((command, extension, handle, algorithm, rest),) = client.sent
    assert command == paramiko_sftp.CMD_EXTENDED
    assert extension == "check-file-handle"
    assert handle == b"h"
    assert algorithm == "sha256"
    # start-offset and length are uint64 on the wire, which a plain int is not
    # when a message packs it; block-size is a uint32.
    start, length, block_size = rest
    assert (start, length, block_size) == (0, 0, 0)
    assert type(start) is paramiko_sftp.int64
    assert type(length) is paramiko_sftp.int64
    assert type(block_size) is int


@pytest.mark.parametrize("algorithm", ["MD5", "sha-256", "md5,sha1", "", "blake2"])
def test_an_algorithm_the_draft_does_not_name_is_not_sent(checksum_path, algorithm):
    path, client = checksum_path(lambda name, _call: _reply("md5"))

    with pytest.raises(NotImplementedError):
        path.checksum(algorithm)
    assert client.requests == []


@pytest.mark.parametrize("algorithm", sorted(_SIZES))
def test_a_reply_is_a_digest_only_of_the_size_the_algorithm_has(algorithm):
    size = _SIZES[algorithm]
    assert _checkfile.parse_reply(_reply(algorithm), algorithm) == (
        _DIGESTS[algorithm].hex()
    )
    for wrong in (b"", bytes(size - 1), bytes(size + 1), bytes(2 * size)):
        with pytest.raises(NotImplementedError):
            _checkfile.parse_reply(_reply(algorithm, wrong), algorithm)


def test_a_reply_for_an_algorithm_without_a_known_size_is_refused():
    for algorithm in ("crc64", "MD5", "sha-256"):
        with pytest.raises(NotImplementedError):
            _checkfile.parse_reply(_reply("md5", b"\x00" * 3), algorithm)


def test_a_server_without_the_extension_is_asked_once_per_algorithm(
    flavor, kind, sftp_loopback, sftp_root, sftp_wire, backends
):
    (sftp_root / "a.txt").write_bytes(b"hello")
    backend = make_backend(kind, sftp_loopback)
    backends.append(backend)
    path = make_path(flavor, sftp_loopback, backend, "a.txt")
    sftp_wire.reset()

    for algorithm in ("md5", "md5", "md5", "sha256", "sha256"):
        with pytest.raises(NotImplementedError):
            path.checksum(algorithm)
    assert path.supported_checksums() == frozenset()

    # Each attempt opens the file once and closes it again; the second and
    # third md5 and the second sha256 never reach the server.
    assert sftp_wire.count("open", "open56") == 2
    assert sftp_wire.count("close") == 2


# --- unclosed files are written when the interpreter exits ----------------------

_CHILD = """
import sys

host, port, kind, flavor, mode = sys.argv[1:6]
port = int(port)
if kind == "paramiko":
    import paramiko
    from sftppath import SFTPBackend

    backend = SFTPBackend(
        host,
        port,
        user="x",
        password="x",
        connect_opts={"allow_agent": False, "look_for_keys": False},
        hostkeypolicy=paramiko.AutoAddPolicy(),
        ssh_config=None,
        known_hosts=None,
    )
else:
    from sftppath import AsyncSSHBackend

    backend = AsyncSSHBackend(
        host,
        port,
        user="x",
        password="x",
        connect_opts={"known_hosts": None},
        ssh_config=None,
    )
if flavor == "path":
    from sftppath import SFTPPath

    path = SFTPPath("/left.txt", backend=backend)
else:
    import sftppath.uri
    from uripath import URIPath

    path = URIPath(f"sftp://x:x@{host}:{port}/left.txt", backend=backend)
handle = path.open("wb" if mode == "bytes" else "w")
handle.write(b"buffered, never closed" if mode == "bytes" else "text, never closed")
"""


@pytest.mark.parametrize("mode", ["bytes", "text"])
def test_a_file_left_open_at_exit_is_written(
    flavor, kind, sftp_loopback, sftp_root, mode
):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [SRC, *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )

    finished = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD,
            sftp_loopback.host,
            str(sftp_loopback.port),
            kind,
            flavor,
            mode,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert finished.returncode == 0, finished.stderr
    assert "shutting down" not in finished.stderr
    expected = b"buffered, never closed" if mode == "bytes" else b"text, never closed"
    assert (sftp_root / "left.txt").read_bytes() == expected


def test_the_exit_hook_does_not_wait_for_a_connection_that_hangs(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class _Hung:
        def close(self):
            entered.set()
            # The cap only frees the thread should the hook wrongly wait for it.
            release.wait(30)
            finished.set()

    hung = _Hung()
    monkeypatch.setattr(_exit, "_UNCLOSED", weakref.WeakSet([hung]))
    monkeypatch.setattr(_exit, "_EXIT_GRACE", 0.3)
    try:
        _exit._close_unclosed()

        # The hook has returned while the close it started is still blocked.
        assert entered.wait(10)
        assert not finished.is_set()
    finally:
        release.set()
        finished.wait(10)


# --- one backend for the paths derived from one path ------------------------------


def test_a_fresh_root_its_parent_and_twenty_children_open_one_connection(
    flavor, kind, sftp_root, sftp_loopback, home, monkeypatch, backends
):
    for number in range(20):
        (sftp_root / f"c{number}.txt").write_bytes(b"x")
    (home / ".ssh" / "known_hosts").write_text(sftp_loopback.known_hosts_line())

    path = built_path(flavor, kind, sftp_loopback, "c0.txt", monkeypatch)
    backends.append(path.backend)

    assert path.parent.exists()
    assert path.exists()
    assert all((path.parent / f"c{n}.txt").exists() for n in range(20))

    assert sftp_loopback.connections == 1
