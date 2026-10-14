"""An SFTP file is written through as the stream is used: the server opens (and
truncates) the file when `open()` returns, and a `with` block that raised leaves
what was written, as a local file does. Nothing is held back for `close()` to
commit, so there is nothing for a discard to undo.

Run on both backends and both path flavors against the loopback server.
"""

import pytest

pytest.importorskip("asyncssh")

from sftp_support import make_backend, make_path  # noqa: E402


class Abort(Exception):
    pass


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def path_at(flavor, kind, sftp_loopback, backends):
    """`path_at(name)`: the path `name` below the served root."""
    backend = make_backend(kind, sftp_loopback)
    backends.append(backend)
    return lambda name: make_path(flavor, sftp_loopback, backend, name)


def test_the_file_is_truncated_when_the_stream_is_opened(sftp_root, path_at):
    (sftp_root / "f.bin").write_bytes(b"OLD CONTENT")
    stream = path_at("f.bin").open("wb")
    try:
        assert (sftp_root / "f.bin").read_bytes() == b""
    finally:
        stream.close()


@pytest.mark.parametrize("mode, data", [("wb", b"part"), ("w", "part")])
def test_a_block_that_raised_leaves_what_was_written(sftp_root, path_at, mode, data):
    (sftp_root / "f.bin").write_bytes(b"OLD CONTENT")
    path = path_at("f.bin")
    with pytest.raises(Abort):
        with path.open(mode) as f:
            f.write(data)
            raise Abort
    assert (sftp_root / "f.bin").read_bytes() == b"part"
