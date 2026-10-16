"""Reading a directory as a file raises `IsADirectoryError` on both backends,
against servers that, like OpenSSH's sftp-server, open a directory for reading.

OpenSSH on Linux opens the directory and fails the read with a bare failure
(EISDIR has no SFTPv3 status); OpenSSH for Windows also opens it, fails a sized
read the same way and reports a size of 0, so an unsized read of fstat's size
asks for nothing. The servers here reproduce both on the loopback interface.

The error is raised by a read of the open file, which belongs to the core
path, so it names the server path for both path flavors.
"""

import errno
import os

import pytest

asyncssh = pytest.importorskip("asyncssh")

from sftp_loopback import counting_server_class  # noqa: E402
from sftp_support import make_backend, make_path  # noqa: E402

#: Per server: the size an fstat of a directory handle reports, and whether a
#: read of one fails. "eof" is no OpenSSH: it answers every such read with EOF.
SERVERS = {"linux": (4096, True), "windows": (0, True), "eof": (0, False)}
OPENSSH = ["linux", "windows"]


class _DirectoryHandle:
    def __init__(self, local):
        self.local = local


def opens_directories(server):
    """An asyncssh server class that opens a directory for reading as the
    `server` of `SERVERS` does."""
    reported_size, fails = SERVERS[server]

    class Server(asyncssh.SFTPServer):
        def open(self, path, pflags, attrs):
            # asyncssh's own conversion of a mapped path to a local one.
            local = asyncssh.sftp._to_local_path(self.map_path(path))
            if os.path.isdir(local) and not pflags & asyncssh.FXF_WRITE:
                return _DirectoryHandle(local)
            return super().open(path, pflags, attrs)

        def read(self, file_obj, offset, size):
            if isinstance(file_obj, _DirectoryHandle):
                if size == 0 or not fails:
                    return b""
                raise asyncssh.SFTPFailure("Failure")
            return super().read(file_obj, offset, size)

        def fstat(self, file_obj):
            if isinstance(file_obj, _DirectoryHandle):
                attrs = asyncssh.SFTPAttrs.from_local(os.stat(file_obj.local))
                attrs.size = reported_size
                return attrs
            return super().fstat(file_obj)

        def close(self, file_obj):
            if isinstance(file_obj, _DirectoryHandle):
                return None
            return super().close(file_obj)

    return Server


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture(params=OPENSSH)
def openssh(request, sftp_root, sftp_wire, sftp_loopbacks):
    """A loopback server that opens directories as OpenSSH on Linux or on
    Windows does (or as `SERVERS` names, by indirect parametrization), serving
    `sftp_root`, which holds the directory `dir`."""
    (sftp_root / "dir").mkdir()
    server_class = counting_server_class(sftp_wire, opens_directories(request.param))
    return sftp_loopbacks(sftp_root, sftp_wire, server_class=server_class)


@pytest.fixture
def backend(kind, openssh, backends):
    backend = make_backend(kind, openssh)
    backends.append(backend)
    return backend


def assert_is_a_directory(raised):
    assert raised.type is IsADirectoryError
    assert raised.value.errno == errno.EISDIR
    assert raised.value.filename == "/dir"


def test_the_server_opens_the_directory(flavor, openssh, sftp_wire, backend):
    # The fixture is what it claims: the open succeeds, so the read decides.
    with make_path(flavor, openssh, backend, "dir").open("rb"):
        pass
    assert sftp_wire.count("open") == 1


def test_read_bytes_of_a_directory_is_a_directory_error(flavor, openssh, backend):
    with pytest.raises(IsADirectoryError) as raised:
        make_path(flavor, openssh, backend, "dir").read_bytes()
    assert_is_a_directory(raised)


def test_read_text_of_a_directory_is_a_directory_error(flavor, openssh, backend):
    with pytest.raises(IsADirectoryError) as raised:
        make_path(flavor, openssh, backend, "dir").read_text()
    assert_is_a_directory(raised)


@pytest.mark.parametrize("openssh", ["eof"], indirect=True)
def test_a_directory_whose_reads_answer_eof_is_a_directory_error(
    flavor, openssh, backend
):
    with pytest.raises(IsADirectoryError) as raised:
        make_path(flavor, openssh, backend, "dir").read_bytes()
    assert_is_a_directory(raised)


def test_a_sized_read_of_a_directory_is_a_directory_error(flavor, openssh, backend):
    with make_path(flavor, openssh, backend, "dir").open("rb") as file:
        with pytest.raises(IsADirectoryError) as raised:
            file.read(80)
    assert_is_a_directory(raised)


def test_an_empty_file_still_reads_empty(flavor, openssh, sftp_root, backend):
    (sftp_root / "empty").write_bytes(b"")
    assert make_path(flavor, openssh, backend, "empty").read_bytes() == b""
    assert make_path(flavor, openssh, backend, "empty").read_text() == ""


# --- what the check costs ----------------------------------------------------

#: The fstats an unsized read of a file asks for by itself: asyncssh reads the
#: size it then asks for, paramiko reads to EOF.
OWN_FSTATS = {"asyncssh": 1, "paramiko": 0}


def test_reading_a_file_asks_for_nothing_more(
    kind, flavor, openssh, sftp_root, sftp_wire, backend
):
    (sftp_root / "file").write_bytes(b"abc")
    path = make_path(flavor, openssh, backend, "file")
    path.stat()
    sftp_wire.reset()
    assert path.read_bytes() == b"abc"
    assert sftp_wire.count("fstat") == OWN_FSTATS[kind]
    with path.open("rb") as file:
        assert file.read(2) == b"ab"
    assert sftp_wire.count("fstat") == OWN_FSTATS[kind]


def test_an_empty_read_asks_what_the_handle_is_once(
    kind, flavor, openssh, sftp_root, sftp_wire, backend
):
    (sftp_root / "empty").write_bytes(b"")
    path = make_path(flavor, openssh, backend, "empty")
    path.stat()
    sftp_wire.reset()
    assert path.read_bytes() == b""
    assert sftp_wire.count("fstat") == OWN_FSTATS[kind] + 1


def test_a_failed_read_of_a_file_stays_a_bare_failure(
    kind, flavor, openssh, sftp_root, sftp_wire, backend
):
    (sftp_root / "file").write_bytes(b"abc")

    def fail(server, *args):
        raise asyncssh.SFTPFailure("Failure")

    sftp_wire.before["read"] = fail
    with pytest.raises(OSError) as raised:
        make_path(flavor, openssh, backend, "file").read_bytes()
    assert raised.type is OSError
    assert raised.value.errno is None
    # The handle was asked about, and said it is a file.
    assert sftp_wire.count("fstat") == OWN_FSTATS[kind] + 1
