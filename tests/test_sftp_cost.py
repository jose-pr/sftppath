"""What the SFTP backends ask the server for: requests counted at the loopback
server, never timings.

A listing already describes every entry, so a walk that decides file or
directory from it should not ask again for what it was told. `_scandir()` and
`walk()` cost one request per directory on either path flavor. The children
`iterdir()` yields differ: a URI path's child carries what the listing said
about it, while a core `SFTPPath` child is a plain path that asks the server
for its own stat.
"""

import os
import pathlib
import subprocess
import sys

import pytest

asyncssh = pytest.importorskip("asyncssh")

from sftp_support import make_backend, make_path  # noqa: E402

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")


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
def asyncssh_backend(sftp_loopback, backends):
    backend = make_backend("asyncssh", sftp_loopback)
    backends.append(backend)
    return backend


def make_tree(base, directories=8, files=16):
    for number in range(directories):
        folder = base / f"d{number}"
        folder.mkdir(parents=True)
        for index in range(files):
            (folder / f"f{index:02}.bin").write_bytes(b"x" * 64)


def make_directory(sftp_root, files=20):
    """`dir` with `files` files and one subdirectory; 21 entries."""
    (sftp_root / "dir").mkdir()
    for number in range(files):
        (sftp_root / "dir" / f"f{number:02}").write_bytes(b"x")
    (sftp_root / "dir" / "sub").mkdir()


def stats_for_listed_children(flavor, children):
    """The `stat` requests it takes to ask `children` (what `iterdir()` yielded
    for entries the listing described, none of them a link) whether each is a
    directory. A URI path's child answers from the listing; a core path's child
    asks the server once."""
    return 0 if flavor == "uri" else children


# --- a listing answers what a walk and a loop ask ----------------------------------


def test_native_rm_asks_about_no_entry_the_listing_described(
    flavor, sftp_loopback, sftp_root, sftp_wire, asyncssh_backend
):
    make_tree(sftp_root / "tree")
    tree = make_path(flavor, sftp_loopback, asyncssh_backend, "tree")
    tree.exists()
    sftp_wire.reset()

    tree.rm(recursive=True)

    assert not (sftp_root / "tree").exists()
    # 128 files and 9 directories are removed; each of the 9 directories is
    # listed once and the root is looked at once. The server's own lstat of
    # "." and ".." for each listing (18) is part of its answer to the listing.
    assert sftp_wire.calls["remove"] == 128
    assert sftp_wire.calls["rmdir"] == 9
    assert sftp_wire.calls["scandir"] == 9
    assert sftp_wire.calls["lstat"] <= 19
    assert sftp_wire.total() <= 165


def test_a_scan_describes_every_child_in_one_request(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend
):
    make_directory(sftp_root)
    directory = make_path(flavor, sftp_loopback, backend, "dir")
    directory.exists()
    sftp_wire.reset()

    kinds = [(name, stat.is_dir()) for name, stat in directory._scandir()]

    assert sum(1 for _name, is_dir in kinds if is_dir) == 1
    assert len(kinds) == 21
    assert sftp_wire.calls["scandir"] == 1
    assert sftp_wire.calls["stat"] == 0


def test_a_walk_asks_for_the_listing_of_each_directory_and_nothing_else(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend
):
    make_directory(sftp_root)
    directory = make_path(flavor, sftp_loopback, backend, "dir")
    directory.exists()
    sftp_wire.reset()

    (_top, dirnames, filenames), (_sub, sub_dirs, sub_files) = list(directory.walk())

    assert (len(dirnames), len(filenames)) == (1, 20)
    assert (sub_dirs, sub_files) == ([], [])
    assert sftp_wire.calls["scandir"] == 2
    assert sftp_wire.calls["stat"] == 0


def test_a_loop_over_a_listing_stats_no_child_that_is_not_a_link(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend
):
    make_directory(sftp_root)
    directory = make_path(flavor, sftp_loopback, backend, "dir")
    directory.exists()
    sftp_wire.reset()

    kinds = [(child.name, child.is_dir()) for child in directory.iterdir()]

    assert sum(1 for _name, is_dir in kinds if is_dir) == 1
    assert len(kinds) == 21
    assert sftp_wire.calls["scandir"] == 1
    assert sftp_wire.calls["stat"] == stats_for_listed_children(flavor, 21)


def test_a_listed_link_is_still_followed_by_a_stat_of_its_own(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend
):
    (sftp_root / "dir" / "real").mkdir(parents=True)
    try:
        os.symlink(sftp_root / "dir" / "real", sftp_root / "dir" / "link", True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot create a symbolic link")
    directory = make_path(flavor, sftp_loopback, backend, "dir")
    directory.exists()
    sftp_wire.reset()

    # The listing describes the link as a link, whatever it points at.
    listed = dict(directory._scandir())
    assert listed["real"].is_dir()
    assert listed["link"].is_symlink() and not listed["link"].is_dir()
    assert sftp_wire.calls["stat"] == 0

    by_name = {child.name: child for child in directory.iterdir()}
    sftp_wire.reset()
    assert by_name["real"].is_dir()
    assert sftp_wire.calls["stat"] == stats_for_listed_children(flavor, 1)
    sftp_wire.reset()
    assert by_name["link"].is_dir()
    assert sftp_wire.calls["stat"] == 1
    assert by_name["link"].is_symlink()


def test_a_listed_entry_that_does_not_say_what_it_is_is_asked_about(
    flavor, sftp_loopback, sftp_root, sftp_wire, backend
):
    (sftp_root / "dir").mkdir()
    sftp_wire.listing = lambda path: [
        asyncssh.SFTPName(b"typeless", attrs=asyncssh.SFTPAttrs(size=3))
    ]
    directory = make_path(flavor, sftp_loopback, backend, "dir")
    (child,) = [c for c in directory.iterdir() if c.name == "typeless"]
    sftp_wire.reset()

    assert not child.is_dir()

    assert sftp_wire.calls["stat"] == 1


def test_a_hint_is_used_once(flavor, sftp_loopback, sftp_root, sftp_wire, backend):
    (sftp_root / "dir").mkdir()
    (sftp_root / "dir" / "a").write_bytes(b"x")
    directory = make_path(flavor, sftp_loopback, backend, "dir")
    (child,) = list(directory.iterdir())
    sftp_wire.reset()

    assert child.is_file()
    first = stats_for_listed_children(flavor, 1)
    assert sftp_wire.count("stat", "lstat") == first
    # What was learned from the listing is not trusted a second time.
    assert child.is_file()
    assert sftp_wire.count("stat", "lstat") == first + 1


# --- a backend that has no native tree operation never loads asyncssh -----------

_CHILD = """
import sys
import paramiko
from sftppath import SFTPBackend, SFTPPath

host, port, flavor = sys.argv[1:4]
backend = SFTPBackend(
    host,
    int(port),
    user="x",
    password="x",
    connect_opts={"allow_agent": False, "look_for_keys": False},
    hostkeypolicy=paramiko.AutoAddPolicy(),
    ssh_config=None,
    known_hosts=None,
)
assert not backend.supports_tree
if flavor == "path":
    root = SFTPPath("/", backend=backend)
else:
    import sftppath.uri
    from uripath import URIPath

    root = URIPath(f"sftp://x:x@{host}:{port}/", backend=backend)
try:
    (root / "one.txt").copy(root / "two.txt")
    (root / "tree" / "sub").mkdir(parents=True)
    (root / "tree" / "sub" / "f.txt").write_bytes(b"x")
    (root / "tree").copy(root / "tree2", recursive=True)
    (root / "tree").rm(recursive=True)
finally:
    backend.close()
print("asyncssh loaded:", "asyncssh" in sys.modules)
"""


def test_copy_and_rm_on_the_paramiko_backend_do_not_import_asyncssh(
    flavor, sftp_loopback, sftp_root
):
    pytest.importorskip("paramiko")
    (sftp_root / "one.txt").write_bytes(b"1")
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
            flavor,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert finished.returncode == 0, finished.stderr
    assert "asyncssh loaded: False" in finished.stdout
    assert (sftp_root / "two.txt").read_bytes() == b"1"
    assert (sftp_root / "tree2" / "sub" / "f.txt").read_bytes() == b"x"
    assert not (sftp_root / "tree").exists()
