"""PathSyncer symlink sync across two different backends: a path on an SFTP
server (an `SFTPPath`, or an `SFTPURIPath`, over a fake `BaseSFTPBackend` with a
paramiko-shaped client -- no SSH library, no server) and `LocalPath` or
`MemPath`."""

import os
import stat as _stat_mod

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath
from pathlib_next.sync import PathAndStat, PathSyncer
from sftp_support import make_path

from sftppath import BaseSFTPBackend


def checksum(entry: PathAndStat):
    return entry.stat.st_size


def _relative_symlink_source(tmp_path):
    """A LocalPath symlink with a RELATIVE target, or skip if unsupported."""
    real = tmp_path / "real.txt"
    real.write_text("payload")
    link = tmp_path / "link.txt"
    try:
        os.symlink("real.txt", link)  # relative target, not resolved
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")
    return pathlib_next.LocalPath(link)


# --- cross-backend symlink sync (one side supports symlink_to(), the other
# doesn't / a different implementation) --------------------------------------
# A path over a fake backend (no real SFTP server) stands in as a different
# backend from LocalPath that also implements readlink()/symlink_to(); MemPath
# is the side that does not.


class _FakeSymlinkAttr:
    def __init__(self, filename, st_mode):
        self.filename = filename
        self.st_mode = st_mode
        self.st_size = 0
        self.st_mtime = 0


class _FakeSymlinkSFTPClient:
    """Minimal paramiko-shaped SFTPClient: one symlink at /link.txt
    pointing (raw, relative) at "real.txt", plus the real file it targets.
    Records symlink() calls so tests can assert what target was created."""

    def __init__(self):
        self.symlink_calls = []
        self._files = {"/real.txt": b"payload"}
        self._link_target = {"/link.txt": "real.txt"}

    def _mode_for(self, path):
        if path in self._link_target:
            return _stat_mod.S_IFLNK | 0o777
        if path in self._files:
            return _stat_mod.S_IFREG | 0o644
        raise FileNotFoundError(path)

    def lstat(self, path):
        return _FakeSymlinkAttr(path.rsplit("/", 1)[-1], self._mode_for(path))

    def stat(self, path):
        if path in self._link_target:
            path = "/" + self._link_target[path]
        return _FakeSymlinkAttr(path.rsplit("/", 1)[-1], self._mode_for(path))

    def readlink(self, path):
        return self._link_target[path]

    def symlink(self, target, path):
        self.symlink_calls.append((target, path))
        self._link_target[path] = target

    def listdir_attr(self, path):
        return []

    def open(self, path, mode, buffering):
        raise NotImplementedError


class _FakeSymlinkBackend(BaseSFTPBackend):
    def __init__(self):
        self._client = _FakeSymlinkSFTPClient()

    def client(self):
        return self._client


class _FakeHost:
    """Where the fake backend's paths live, for `make_path()`: the URI of a
    path of the `sftp:` flavor."""

    def url(self, path=""):
        return f"sftp://host/{path}"


def _fake_sftp(flavor, rel, backend):
    return make_path(flavor, _FakeHost(), backend, rel)


def test_sync_symlink_preserve_sftp_source_to_local_target(flavor, tmp_path):
    # SFTP (supports symlink_to) -> Local (supports symlink_to): the
    # supported/supported case across two DIFFERENT backend types.
    backend = _FakeSymlinkBackend()
    source = _fake_sftp(flavor, "link.txt", backend)
    target = pathlib_next.LocalPath(tmp_path / "link_copy.txt")

    PathSyncer(checksum, follow_symlinks=False).sync(source, target)

    assert target.is_symlink()
    assert target.readlink().as_posix() == "real.txt"


def test_sync_symlink_preserve_local_source_to_sftp_target(flavor, tmp_path):
    # Local (supports symlink_to) -> SFTP (supports symlink_to): the other
    # direction of the supported/supported cross-backend case.
    source = _relative_symlink_source(tmp_path)
    backend = _FakeSymlinkBackend()
    target = _fake_sftp(flavor, "link_copy.txt", backend)

    PathSyncer(checksum, follow_symlinks=False).sync(source, target)

    assert backend._client.symlink_calls == [("real.txt", "/link_copy.txt")]


def test_sync_symlink_preserve_cross_backend_unsupported_target_raises(
    flavor,
    tmp_path,
):
    # SFTP source (supports symlink_to) -> MemPath target (does not):
    # the documented behavior is NotImplementedError surfaced through
    # ignore_error/hook(), not a silent skip or crash.
    backend = _FakeSymlinkBackend()
    source = _fake_sftp(flavor, "link.txt", backend)
    target = MemPath("/link_copy.txt")

    with pytest.raises(NotImplementedError):
        PathSyncer(checksum, follow_symlinks=False).sync(source, target)
    assert not target.exists()
