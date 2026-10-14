"""What a failed SFTP request raises, and how a listing treats names that are
not UTF-8, on both backends and for both path flavors against a loopback server.

Both backends must agree on the exception a server status or a lost
connection becomes, and an `SFTPPath` and an `SFTPURIPath` on the same backend
must agree on it too, so every scenario runs once per backend and flavor. The
server counts what it receives and can fail or reshape any request; nothing
here leaves the machine, and the home directory is an empty one so no `~/.ssh`
of the developer's is read.

The file name an error carries is the wire path for an `SFTPPath` and the URI
of the path, without its password, for an `SFTPURIPath`.
"""

import errno
import logging
import pathlib
import socket
import threading

import pytest

asyncssh = pytest.importorskip("asyncssh")

from sftp_loopback import SFTPServerAddress, name_on_disk  # noqa: E402
from sftp_support import make_backend, make_path  # noqa: E402

from sftppath import SFTPAuthenticationError, SFTPHostKeyError  # noqa: E402


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert pathlib.Path.home() == home
    return home


def new_backend(kind, server, backends, **kwargs):
    """A backend of `kind` for `server`, closed when the test ends: it trusts
    any host key unless `known_hosts` is given, reads no ssh_config and uses no
    agent or key of the developer's."""
    backend = make_backend(kind, server, **kwargs)
    backends.append(backend)
    return backend


@pytest.fixture
def backend(kind, sftp_loopback, backends):
    return new_backend(kind, sftp_loopback, backends)


def unchained(error):
    """Neither the cause nor the context of `error` is shown: the library's
    exception, whose text can carry credentials, is not reachable from it."""
    return error.__cause__ is None and (
        error.__suppress_context__ or error.__context__ is None
    )


def listening_on(port):
    return SFTPServerAddress("127.0.0.1", port)


def shown_name(flavor, server, rel):
    """The file name an error about `rel` carries: the wire path for an
    `SFTPPath`, the URI of the path (no password) for an `SFTPURIPath`."""
    if flavor == "path":
        return "/" + rel
    return f"sftp://x@{server.host}:{server.port}/{rel}"


# --- a server status becomes the same exception on both backends ---------------


def test_missing_path_errors_carry_the_remote_path(flavor, sftp_loopback, backend):
    root = make_path(flavor, sftp_loopback, backend)
    nope = root / "nope"
    child = nope / "child"
    for call, rel in (
        (lambda: nope.stat(), "nope"),
        (lambda: nope.open("rb"), "nope"),
        (lambda: nope.unlink(), "nope"),
        (lambda: child.mkdir(), "nope/child"),
        (lambda: nope.rmdir(), "nope"),
    ):
        with pytest.raises(FileNotFoundError) as raised:
            call()
        assert raised.value.errno == errno.ENOENT
        assert raised.value.filename == shown_name(flavor, sftp_loopback, rel)


def test_rename_error_names_both_paths_without_a_winerror(
    flavor, sftp_loopback, backend
):
    root = make_path(flavor, sftp_loopback, backend)
    with pytest.raises(FileNotFoundError) as raised:
        (root / "nope").rename(root / "elsewhere")
    assert raised.value.errno == errno.ENOENT
    assert raised.value.filename == shown_name(flavor, sftp_loopback, "nope")
    assert raised.value.filename2 == shown_name(flavor, sftp_loopback, "elsewhere")
    assert "WinError" not in str(raised.value)


def test_unsupported_setstat_is_not_implemented_and_a_copy_still_completes(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend
):
    (sftp_root / "src.txt").write_bytes(b"payload")

    def unsupported(server_, *args):
        raise asyncssh.SFTPOpUnsupported("setstat not supported here")

    sftp_wire.before["setstat"] = unsupported
    src = make_path(flavor, sftp_loopback, backend, "src.txt")

    with pytest.raises(NotImplementedError):
        src.chmod(0o600)
    assert sftp_wire.paths("setstat")[-1].endswith(b"/src.txt")
    sftp_wire.reset()
    src.copy(make_path(flavor, sftp_loopback, backend, "copy.txt"))

    assert (sftp_root / "copy.txt").read_bytes() == b"payload"
    # The copy asked to carry the metadata over, and the refusal was tolerated.
    assert [p for p in sftp_wire.paths("setstat") if p.endswith(b"/copy.txt")]


def test_connection_lost_status_is_a_connection_reset(
    flavor, sftp_loopback, sftp_wire, backend
):
    def lost(server_, *args):
        raise asyncssh.SFTPConnectionLost("gone")

    sftp_wire.before["stat"] = lost

    with pytest.raises(ConnectionResetError) as raised:
        make_path(flavor, sftp_loopback, backend, "x").stat()
    assert raised.value.errno == errno.ECONNRESET
    assert unchained(raised.value)


def test_a_translated_status_is_not_chained_to_the_library_exception(
    flavor, sftp_loopback, backend
):
    with pytest.raises(FileNotFoundError) as raised:
        make_path(flavor, sftp_loopback, backend, "nope").stat()
    assert unchained(raised.value)


# --- the connection, the login and the host key ---------------------------------


def test_wrong_password_is_an_authentication_error(
    flavor, sftp_root, sftp_wire, sftp_loopbacks, kind, backends
):
    sftp_loopback = sftp_loopbacks(sftp_root, sftp_wire, password="right")
    backend = new_backend(kind, sftp_loopback, backends, user="alice", password="wrong")

    with pytest.raises(SFTPAuthenticationError) as raised:
        make_path(flavor, sftp_loopback, backend, "a").stat()

    error = raised.value
    assert isinstance(error, PermissionError)
    assert error.errno == errno.EACCES
    assert "wrong" not in str(error) and "right" not in str(error)
    assert f"alice@{sftp_loopback.host}:{sftp_loopback.port}" in str(error)
    assert unchained(error)
    assert sftp_loopback.logins and all(
        given == "wrong" for _user, given in sftp_loopback.logins
    )


def test_right_password_logs_in(
    flavor, sftp_root, sftp_wire, sftp_loopbacks, kind, backends
):
    sftp_loopback = sftp_loopbacks(sftp_root, sftp_wire, password="right")
    (sftp_root / "a").write_bytes(b"x")
    backend = new_backend(kind, sftp_loopback, backends, password="right")

    assert make_path(flavor, sftp_loopback, backend, "a").stat().st_size == 1
    assert ("x", "right") in sftp_loopback.logins


def test_a_changed_host_key_is_a_host_key_error_and_no_credential_is_sent(
    flavor, sftp_root, sftp_wire, sftp_loopbacks, kind, backends, tmp_path
):
    sftp_loopback = sftp_loopbacks(sftp_root, sftp_wire, password="right")
    other = asyncssh.generate_private_key("ssh-rsa")
    known = tmp_path / "known_hosts"
    known.write_text(sftp_loopback.known_hosts_line(other))
    backend = new_backend(
        kind, sftp_loopback, backends, known_hosts=known, password="right"
    )

    with pytest.raises(SFTPHostKeyError) as raised:
        make_path(flavor, sftp_loopback, backend, "a").stat()

    error = raised.value
    assert isinstance(error, ConnectionError)
    assert error.errno == errno.ECONNABORTED
    assert "right" not in str(error)
    assert f"x@{sftp_loopback.host}:{sftp_loopback.port}" in str(error)
    assert unchained(error)
    assert sftp_loopback.logins == []


def test_an_unknown_host_key_is_a_host_key_error(
    flavor, sftp_root, sftp_wire, sftp_loopbacks, kind, backends, tmp_path
):
    sftp_loopback = sftp_loopbacks(sftp_root, sftp_wire)
    known = tmp_path / "known_hosts"
    known.write_text("")
    backend = new_backend(kind, sftp_loopback, backends, known_hosts=known)

    with pytest.raises(SFTPHostKeyError):
        make_path(flavor, sftp_loopback, backend, "a").stat()


@pytest.fixture
def old_protocol_port():
    """The port of a loopback server that greets with SSH protocol version 1.5
    and then waits: both clients refuse it on their own, so the handshake
    fails for a reason that is neither the login nor the host key."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    accepted, done = [], threading.Event()

    def serve():
        while not done.is_set():
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            accepted.append(connection)
            try:
                connection.sendall(b"SSH-1.5-old\r\n")
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1]
    done.set()
    thread.join(10)
    listener.close()
    for connection in accepted:
        connection.close()


_LIBRARY_ERROR = {"paramiko": "IncompatiblePeer", "asyncssh": "ProtocolNotSupported"}


def test_a_failed_handshake_logs_the_library_error_at_debug(
    flavor, old_protocol_port, kind, backends, caplog
):
    server = listening_on(old_protocol_port)
    backend = new_backend(kind, server, backends)
    path = make_path(flavor, server, backend, "a")

    with caplog.at_level(logging.DEBUG, logger="sftppath"):
        with pytest.raises(ConnectionAbortedError) as raised:
            path.stat()

    name = _LIBRARY_ERROR[kind]
    error = raised.value
    assert error.errno == errno.ECONNABORTED
    assert str(error) == f"[Errno {errno.ECONNABORTED}] SFTP connection failed ({name})"
    assert unchained(error)
    records = [r for r in caplog.records if r.name == "sftppath"]
    assert [r.levelno for r in records] == [logging.DEBUG]
    message = records[0].getMessage()
    prefix = f"SFTP connection failed: {name}: "
    assert message.startswith(prefix) and len(message) > len(prefix)


def test_a_failed_handshake_logs_nothing_above_debug(
    flavor, old_protocol_port, kind, backends, caplog
):
    server = listening_on(old_protocol_port)
    backend = new_backend(kind, server, backends)
    path = make_path(flavor, server, backend, "a")

    with caplog.at_level(logging.INFO, logger="sftppath"):
        with pytest.raises(ConnectionAbortedError):
            path.stat()

    assert [r for r in caplog.records if r.name == "sftppath"] == []


def test_a_trusted_host_key_connects(
    flavor, sftp_root, sftp_wire, sftp_loopbacks, kind, backends, tmp_path
):
    sftp_loopback = sftp_loopbacks(sftp_root, sftp_wire)
    (sftp_root / "a").write_bytes(b"xy")
    known = tmp_path / "known_hosts"
    known.write_text(sftp_loopback.known_hosts_line())
    backend = new_backend(kind, sftp_loopback, backends, known_hosts=known)

    assert make_path(flavor, sftp_loopback, backend, "a").stat().st_size == 2


def test_a_connection_closed_in_the_middle_of_a_request_is_a_connection_reset(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend
):
    (sftp_root / "a").write_bytes(b"xy")
    path = make_path(flavor, sftp_loopback, backend, "a")
    assert path.stat().st_size == 2

    def drop(server_, *args):
        sftp_loopback.drop_connections()

    sftp_wire.before["stat"] = drop
    with pytest.raises(ConnectionResetError) as raised:
        path.stat()
    assert raised.value.errno == errno.ECONNRESET
    assert unchained(raised.value)

    # The next call reconnects.
    del sftp_wire.before["stat"]
    assert path.stat().st_size == 2
    assert sftp_loopback.connections == 2


def test_a_connection_closed_in_the_middle_of_a_read_is_a_connection_reset(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend
):
    (sftp_root / "big").write_bytes(b"x" * 300_000)

    def drop(server_, *args):
        sftp_loopback.drop_connections()

    sftp_wire.before["read"] = drop
    with pytest.raises(ConnectionResetError):
        make_path(flavor, sftp_loopback, backend, "big").read_bytes()


def test_a_refused_connection_stays_a_connection_refused_error(
    flavor, kind, backends, unused_tcp_port
):
    # Nothing listens on a port nobody holds.
    server = listening_on(unused_tcp_port)
    backend = new_backend(kind, server, backends)

    with pytest.raises(ConnectionRefusedError):
        make_path(flavor, server, backend, "a").stat()


# --- a transport failure does not trigger a second probe -------------------------


def test_a_lost_connection_in_mkdir_is_not_probed_again(
    flavor, sftp_loopback, sftp_wire, backend
):
    path = make_path(flavor, sftp_loopback, backend, "d")
    path.parent.stat()

    def drop(server_, *args):
        sftp_loopback.drop_connections()

    sftp_wire.before["mkdir"] = drop
    sftp_wire.reset()
    with pytest.raises(ConnectionResetError):
        path.mkdir()
    assert sftp_wire.calls["stat"] == 0


# --- names that are not UTF-8 -----------------------------------------------------

ODD = "bad\udcff\udcfeutf8"


def odd_on_disk():
    return name_on_disk(ODD.encode("utf-8", "surrogateescape")).decode()


def odd_tree(sftp_root):
    tree = sftp_root / "tree"
    tree.mkdir()
    (tree / "good.txt").write_bytes(b"good")
    (tree / odd_on_disk()).write_bytes(b"odd")
    sub = tree / "sub"
    sub.mkdir()
    (sub / "inner.txt").write_bytes(b"inner")
    return tree


@pytest.fixture
def odd_server(sftp_root, sftp_wire, sftp_loopbacks):
    odd_tree(sftp_root)
    return sftp_loopbacks(sftp_root, sftp_wire, escaped_names=True)


@pytest.fixture
def odd_backend(kind, odd_server, backends):
    return new_backend(kind, odd_server, backends)


def test_a_non_utf8_name_lists_beside_its_siblings(flavor, odd_server, odd_backend):
    tree = make_path(flavor, odd_server, odd_backend, "tree")

    assert sorted(child.name for child in tree.iterdir()) == sorted(
        ["good.txt", ODD, "sub"]
    )
    walked = {name for _dir, _dirs, files in tree.walk() for name in files}
    assert walked == {"good.txt", ODD, "inner.txt"}
    assert sorted(match.name for match in tree.glob("*")) == sorted(
        ["good.txt", ODD, "sub"]
    )


def test_a_non_utf8_name_reads_and_stats(flavor, odd_server, odd_backend):
    odd = make_path(flavor, odd_server, odd_backend, "tree") / ODD

    assert odd.read_bytes() == b"odd"
    assert odd.stat().st_size == 3
    assert odd.is_file()


def test_a_non_utf8_name_is_removed_by_unlink_and_by_rm(
    flavor, odd_server, sftp_root, odd_backend
):
    tree = make_path(flavor, odd_server, odd_backend, "tree")
    (tree / ODD).unlink()
    assert not (tree / ODD).exists()

    (sftp_root / "tree" / odd_on_disk()).write_bytes(b"odd")
    tree.rm(recursive=True)

    assert not (sftp_root / "tree").exists()


def test_a_tree_with_a_non_utf8_name_copies_whole(
    flavor, odd_server, sftp_root, odd_backend
):
    tree = make_path(flavor, odd_server, odd_backend, "tree")

    tree.copy(make_path(flavor, odd_server, odd_backend, "copy"), recursive=True)

    copied = sorted(
        path.relative_to(sftp_root / "copy").as_posix()
        for path in (sftp_root / "copy").rglob("*")
        if path.is_file()
    )
    assert copied == sorted(["good.txt", odd_on_disk(), "sub/inner.txt"])
