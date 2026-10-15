"""`replace()` and `move(overwrite=True)` onto an existing file are one
`posix-rename@openssh.com` request that replaces it, counted at an in-process
loopback server, for an `SFTPPath` and an `SFTPURIPath` on both backends: the
target is not removed first, so a request that fails leaves both files as they
were. A server without the extension gets the remove-then-rename of
`Path.replace()`; `rename()` onto an existing file uses the extension and,
without it, is refused."""

import errno

import pytest

asyncssh = pytest.importorskip("asyncssh")

from sftp_support import make_backend, make_path  # noqa: E402


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def backend(kind, sftp_loopback, backends):
    backend = make_backend(kind, sftp_loopback)
    backends.append(backend)
    return backend


@pytest.fixture
def two_files(sftp_root):
    (sftp_root / "a.txt").write_bytes(b"from a")
    (sftp_root / "b.txt").write_bytes(b"from b")


def _paths(flavor, sftp_loopback, backend):
    return (
        make_path(flavor, sftp_loopback, backend, "a.txt"),
        make_path(flavor, sftp_loopback, backend, "b.txt"),
    )


def _without_the_extension(monkeypatch, sftp_wire):
    """A server that does not offer `posix-rename@openssh.com` (the asyncssh
    client sees it is missing) and answers the request with "unsupported" if
    it is sent anyway (paramiko sends it regardless)."""
    from asyncssh import sftp as asyncssh_sftp

    handler = asyncssh_sftp.SFTPServerHandler
    monkeypatch.setattr(
        handler,
        "_extensions",
        [e for e in handler._extensions if e[0] != b"posix-rename@openssh.com"],
    )

    def refuse(server, *args):
        raise asyncssh.SFTPOpUnsupported("unsupported")

    sftp_wire.before["posix_rename"] = refuse


def test_move_over_an_existing_file_is_one_posix_rename_and_no_remove(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend, two_files
):
    source, target = _paths(flavor, sftp_loopback, backend)
    source.exists()
    sftp_wire.reset()

    source.move(target, overwrite=True)

    assert sftp_wire.calls["posix_rename"] == 1
    assert sftp_wire.calls["remove"] == 0
    assert sftp_wire.calls["rename"] == 0
    assert not (sftp_root / "a.txt").exists()
    assert (sftp_root / "b.txt").read_bytes() == b"from a"


def test_replace_is_one_posix_rename_and_returns_the_target(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend, two_files
):
    source, target = _paths(flavor, sftp_loopback, backend)
    source.exists()
    sftp_wire.reset()

    result = source.replace(target)

    assert result == target
    assert sftp_wire.calls["posix_rename"] == 1
    assert sftp_wire.calls["remove"] == 0
    assert not (sftp_root / "a.txt").exists()
    assert (sftp_root / "b.txt").read_bytes() == b"from a"


def test_replace_takes_a_sibling_name_as_rename_does(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend, two_files
):
    source, target = _paths(flavor, sftp_loopback, backend)
    sftp_wire.reset()

    result = source.replace("b.txt")

    assert result == target
    assert result.name == "b.txt"
    assert sftp_wire.paths("posix_rename")[0].endswith(b"/a.txt")
    assert sftp_wire.calls["remove"] == 0
    assert (sftp_root / "b.txt").read_bytes() == b"from a"


def test_a_refused_posix_rename_leaves_both_files_as_they_were(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend, two_files
):
    def refuse(server, *args):
        raise asyncssh.SFTPPermissionDenied("denied")

    sftp_wire.before["posix_rename"] = refuse
    source, target = _paths(flavor, sftp_loopback, backend)

    with pytest.raises(PermissionError):
        source.move(target, overwrite=True)
    with pytest.raises(PermissionError):
        source.replace(target)

    assert sftp_wire.calls["remove"] == 0
    assert (sftp_root / "a.txt").read_bytes() == b"from a"
    assert (sftp_root / "b.txt").read_bytes() == b"from b"


def test_a_missing_source_is_not_found_and_the_target_stays(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend, two_files
):
    source = make_path(flavor, sftp_loopback, backend, "gone.txt")
    target = make_path(flavor, sftp_loopback, backend, "b.txt")

    with pytest.raises(FileNotFoundError):
        source.replace(target)

    assert sftp_wire.calls["remove"] == 0
    assert (sftp_root / "b.txt").read_bytes() == b"from b"


def test_without_the_extension_the_target_is_removed_and_the_file_renamed(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend, two_files, monkeypatch
):
    _without_the_extension(monkeypatch, sftp_wire)
    source, target = _paths(flavor, sftp_loopback, backend)
    sftp_wire.reset()

    source.move(target, overwrite=True)

    assert sftp_wire.calls["remove"] == 1
    assert not (sftp_root / "a.txt").exists()
    assert (sftp_root / "b.txt").read_bytes() == b"from a"


def test_replace_without_the_extension_does_the_same_and_learns_it(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend, two_files, monkeypatch
):
    _without_the_extension(monkeypatch, sftp_wire)
    source, target = _paths(flavor, sftp_loopback, backend)

    assert source.replace(target) == target

    assert sftp_wire.calls["remove"] == 1
    assert (sftp_root / "b.txt").read_bytes() == b"from a"
    asked = sftp_wire.calls["posix_rename"]
    (sftp_root / "c.txt").write_bytes(b"from c")
    make_path(flavor, sftp_loopback, backend, "c.txt").replace(target)
    assert sftp_wire.calls["posix_rename"] == asked
    assert sftp_wire.calls["remove"] == 2
    assert (sftp_root / "b.txt").read_bytes() == b"from c"


def test_replace_of_a_directory_onto_one_that_holds_something_removes_nothing(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend
):
    (sftp_root / "src").mkdir()
    (sftp_root / "src" / "s.txt").write_bytes(b"s")
    (sftp_root / "dst").mkdir()
    (sftp_root / "dst" / "d.txt").write_bytes(b"d")
    source = make_path(flavor, sftp_loopback, backend, "src")
    target = make_path(flavor, sftp_loopback, backend, "dst")

    with pytest.raises(OSError) as raised:
        source.replace(target)

    assert raised.value.errno == errno.ENOTEMPTY
    assert sftp_wire.calls["remove"] == 0 and sftp_wire.calls["rmdir"] == 0
    assert (sftp_root / "src" / "s.txt").read_bytes() == b"s"
    assert (sftp_root / "dst" / "d.txt").read_bytes() == b"d"


def test_replace_of_a_file_onto_a_directory_is_refused_and_removes_nothing(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend, two_files
):
    (sftp_root / "dir").mkdir()
    source = make_path(flavor, sftp_loopback, backend, "a.txt")
    target = make_path(flavor, sftp_loopback, backend, "dir")

    with pytest.raises(IsADirectoryError):
        source.replace(target)

    assert sftp_wire.calls["remove"] == 0 and sftp_wire.calls["rmdir"] == 0
    assert (sftp_root / "a.txt").read_bytes() == b"from a"
    assert (sftp_root / "dir").is_dir()


def test_replace_onto_another_server_copies_and_deletes(
    flavor,
    tmp_path,
    sftp_loopbacks,
    sftp_loopback,
    sftp_root,
    sftp_wire,
    kind,
    backend,
    backends,
    two_files,
):
    other_root = tmp_path / "other"
    other_root.mkdir()
    (other_root / "b.txt").write_bytes(b"from the other b")
    other = sftp_loopbacks(other_root)
    other_backend = make_backend(kind, other)
    backends.append(other_backend)
    source = make_path(flavor, sftp_loopback, backend, "a.txt")
    target = make_path(flavor, other, other_backend, "b.txt")

    source.replace(target)

    assert (other_root / "b.txt").read_bytes() == b"from a"
    assert not (sftp_root / "a.txt").exists()
    assert sftp_wire.calls["posix_rename"] == 0
    # The core may stage the copy beside the target and swap it in with one rename.
    assert other.wire.calls["posix_rename"] <= 1
    assert sorted(p.name for p in other_root.iterdir()) == ["b.txt"]


def test_replace_of_a_path_onto_itself_keeps_the_file(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend, two_files
):
    source, _target = _paths(flavor, sftp_loopback, backend)

    source.replace(source)

    assert sftp_wire.calls["remove"] == 0
    assert (sftp_root / "a.txt").read_bytes() == b"from a"


def test_rename_over_an_existing_file_is_one_posix_rename_and_returns_the_target(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend, two_files
):
    source, target = _paths(flavor, sftp_loopback, backend)
    source.exists()
    sftp_wire.reset()

    result = source.rename(target)

    assert result == target
    assert sftp_wire.calls["posix_rename"] == 1
    assert sftp_wire.calls["remove"] == 0
    assert sftp_wire.calls["rename"] == 0
    assert not (sftp_root / "a.txt").exists()
    assert (sftp_root / "b.txt").read_bytes() == b"from a"


def test_rename_to_a_free_name_moves_the_file(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend, two_files
):
    source = make_path(flavor, sftp_loopback, backend, "a.txt")

    result = source.rename("fresh.txt")

    assert result == make_path(flavor, sftp_loopback, backend, "fresh.txt")
    assert not (sftp_root / "a.txt").exists()
    assert (sftp_root / "fresh.txt").read_bytes() == b"from a"
    assert (sftp_root / "b.txt").read_bytes() == b"from b"


def test_rename_over_an_existing_file_without_the_extension_is_refused(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend, two_files, monkeypatch
):
    _without_the_extension(monkeypatch, sftp_wire)
    source, target = _paths(flavor, sftp_loopback, backend)

    with pytest.raises(FileExistsError):
        source.rename(target)

    assert sftp_wire.calls["remove"] == 0
    assert (sftp_root / "a.txt").read_bytes() == b"from a"
    assert (sftp_root / "b.txt").read_bytes() == b"from b"
