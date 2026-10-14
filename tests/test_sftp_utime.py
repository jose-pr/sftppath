"""`SFTPPath` and `SFTPURIPath` set the access and modification time of a file
through the SFTP attributes request, on both client libraries, so `touch()`
moves an existing file's time and a sync can leave its copies with their
source's time. Every test reads the server's own file system afterwards."""

import os

import pytest

pytest.importorskip("asyncssh")

from pathlib_next import LocalPath  # noqa: E402
from pathlib_next.sync import PathSyncer, SyncEvent  # noqa: E402
from sftp_support import make_backend, make_path  # noqa: E402

from sftppath import SFTPPath  # noqa: E402

#: 2001-09-09 01:46:40 UTC.
OLD = 1_000_000_000


@pytest.fixture
def remote(kind, flavor, sftp_server, fixture_tree, backends):
    """(path at the server root, local path of that root)."""
    backend = make_backend(kind, sftp_server)
    backends.append(backend)
    return make_path(flavor, sftp_server, backend), fixture_tree


def test_touch_moves_the_time_of_an_existing_remote_file(remote):
    root, local = remote
    os.utime(local / "a.txt", (OLD, OLD))

    (root / "a.txt").touch()

    stat = os.stat(local / "a.txt")
    assert stat.st_mtime > OLD + 1_000
    assert stat.st_atime > OLD + 1_000
    assert (local / "a.txt").read_text() == "a"


def test_utime_sets_whole_seconds_and_keeps_the_time_it_is_not_given(remote):
    root, local = remote
    os.utime(local / "a.txt", (OLD + 100, OLD))

    (root / "a.txt")._utime(mtime=OLD + 7.9)
    assert os.stat(local / "a.txt").st_mtime == OLD + 7
    assert os.stat(local / "a.txt").st_atime == OLD + 100

    (root / "a.txt")._utime(atime=OLD + 300)
    assert os.stat(local / "a.txt").st_atime == OLD + 300
    assert os.stat(local / "a.txt").st_mtime == OLD + 7


def test_utime_of_a_missing_file_raises_file_not_found(remote):
    root, _local = remote

    with pytest.raises(FileNotFoundError):
        (root / "absent.txt")._utime(mtime=OLD)


class Reads:
    """The files an `SFTPPath` opened for reading, by name: an `SFTPURIPath`
    reads through the `SFTPPath` it delegates to."""

    def __init__(self, monkeypatch, remote_class):
        self.opened = []
        real = SFTPPath._open

        def spy(path, mode="r", buffering=-1):
            if mode.startswith("r"):
                self.opened.append(path.name)
            return real(path, mode, buffering)

        monkeypatch.setattr(SFTPPath, "_open", spy)
        # The loopback server counts as this machine: a quick check is for
        # pairs with a remote side.
        monkeypatch.setattr(remote_class, "is_local", lambda path: False, raising=False)


@pytest.fixture
def reads(monkeypatch, remote):
    return Reads(monkeypatch, type(remote[0]))


def _source(tmp_path, mtime=OLD + 0.75):
    source = LocalPath(tmp_path / "source")
    (source / "d").mkdir(parents=True)
    for name in ("one.txt", "d/two.txt"):
        (source / name).write_text(name)
        os.utime(source / name, (mtime, mtime))
    return source


def test_sync_onto_sftp_leaves_the_source_time_and_the_next_run_reads_nothing(
    remote, tmp_path, reads
):
    root, local = remote
    source = _source(tmp_path)

    PathSyncer().sync(source, root / "dst")

    # SFTP stores whole seconds.
    assert os.stat(local / "dst" / "one.txt").st_mtime == OLD
    assert os.stat(local / "dst" / "d" / "two.txt").st_mtime == OLD
    reads.opened.clear()
    checksums = []

    def counting(entry):
        checksums.append(entry.path.name)
        return 0

    syncer = PathSyncer(counting)
    seen = []
    syncer._hook = lambda s, t, event, dry_run: seen.append(event)
    syncer.sync(source, root / "dst")

    assert SyncEvent.Copy not in seen
    assert checksums == []
    assert reads.opened == []


def test_sync_onto_sftp_without_preserve_mtime_reads_the_files_again(
    remote, tmp_path, reads
):
    root, local = remote
    source = _source(tmp_path)
    PathSyncer(preserve_mtime=False).sync(source, root / "dst")
    assert os.stat(local / "dst" / "one.txt").st_mtime > OLD + 1_000
    reads.opened.clear()

    PathSyncer(preserve_mtime=False).sync(source, root / "dst")

    assert sorted(reads.opened) == ["one.txt", "two.txt"]


def test_sync_from_sftp_gives_the_local_copy_the_server_time(remote, tmp_path_factory):
    root, local = remote
    os.utime(local / "sub" / "c.py", (OLD + 11, OLD + 11))
    copy = tmp_path_factory.mktemp("copy")

    PathSyncer().sync(root / "sub", LocalPath(copy / "sub"))

    assert os.stat(copy / "sub" / "c.py").st_mtime == OLD + 11
