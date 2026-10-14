"""The `sftp:` scheme of uripath: how a URI reaches an `SFTPPath`, how paths of
one server share their connections, what takes the native walks, and the text,
copies and pickles of a path."""

from __future__ import annotations

import inspect
import pickle

import pytest

pytest.importorskip("uripath")

from pathlib_next import LocalPath, Path
from uripath import URIPath

from child import run
from sftp_loopback import ANY_PASSWORD
from sftppath import AsyncSSHBackend, BaseSFTPBackend, SFTPBackend, SFTPPath
from sftppath.uri import SFTPURIBackend, SFTPURIPath

SECRET = b"S3CR3T-dummy"


def uri_backend(kind, **options):
    """An `SFTPURIBackend` of `kind` that trusts any host key, reads no
    ssh_config and uses no agent or key of the developer's."""
    if kind == "paramiko":
        paramiko = pytest.importorskip("paramiko")
        return SFTPURIBackend(
            SFTPBackend,
            connect_opts={"allow_agent": False, "look_for_keys": False},
            hostkeypolicy=paramiko.AutoAddPolicy(),
            ssh_config=None,
            known_hosts=None,
            **options,
        )
    pytest.importorskip("asyncssh")
    return SFTPURIBackend(
        AsyncSSHBackend,
        connect_opts={"known_hosts": None},
        ssh_config=None,
        **options,
    )


class FakeClient:
    """Records the wire path of every request it is asked."""

    def __init__(self):
        self.stats = []

    def stat(self, path):
        self.stats.append(path)

        class Attrs:
            st_mode = 0o100644
            st_size = 3
            st_mtime = 0
            st_atime = 0
            st_uid = 0
            st_gid = 0

        return Attrs()


class FakeBackend(BaseSFTPBackend):
    def __init__(self):
        self.fake = FakeClient()
        self.asked = 0

    def client(self):
        self.asked += 1
        return self.fake


# --- dispatch --------------------------------------------------------------------


def test_the_scheme_is_found_through_the_entry_point_of_the_distribution():
    result = run("""
        from uripath import URIPath

        path = URIPath("sftp://u@h:2222/srv/x")
        print(type(path).__module__, type(path).__name__, "sftp" in URIPath.schemes())
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["sftppath.uri", "SFTPURIPath", "True"]


def test_the_registered_class_of_the_scheme_is_the_uri_path_of_this_package():
    assert URIPath.scheme_class("sftp") is SFTPURIPath
    assert type(URIPath("sftp://h/x")) is SFTPURIPath


def test_pure_operations_import_no_ssh_library_and_build_no_backend():
    result = run("""
        import sys
        from pathlib import PurePosixPath
        from uripath import URIPath

        p = URIPath("sftp://h/a/b")
        operations = [
            "p / 'y'",
            "p / PurePosixPath('y')",
            "p.parent",
            "p.with_name('z')",
            "p.with_suffix('.txt')",
            "p.joinpath('c', 'd')",
            "URIPath(p, 'y')",
            "list(p.parents)",
        ]
        for operation in operations:
            result = eval(operation)
            derived = result if isinstance(result, list) else [result]
            assert all(x._backend is None for x in derived), operation
        assert p._backend is None
        assert not {"paramiko", "asyncssh"} & {
            name for name, module in sys.modules.items() if module is not None
        }
        print("ok")
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_without_either_ssh_library_the_first_request_names_both_extras():
    result = run(
        """
        from uripath import URIPath

        path = URIPath("sftp://h/x")
        assert (path / "y").as_uri() == "sftp://h/x/y"
        try:
            path.exists()
        except ImportError as error:
            print(error)
        """,
        ["paramiko", "asyncssh"],
    )
    assert result.returncode == 0, result.stderr
    assert "sftppath[asyncssh]" in result.stdout
    assert "sftppath[paramiko]" in result.stdout


def test_a_backend_that_is_neither_kind_is_a_type_error():
    path = URIPath("sftp://h/x", backend=object())
    with pytest.raises(TypeError, match="SFTPURIBackend"):
        path.exists()


def test_a_join_with_a_relative_concrete_path_stays_on_the_server():
    import pathlib

    for relative in (pathlib.Path("etc/x"), LocalPath("etc/x")):
        joined = URIPath("sftp://root@host/srv/") / relative
        assert type(joined) is SFTPURIPath
        assert joined.as_uri() == "sftp://root@host/srv/etc/x"
        assert URIPath("sftp://root@host/srv/").joinpath(relative) == joined


# --- what a URI asks of the backend ----------------------------------------------


@pytest.mark.parametrize(
    ("url", "wire"),
    [
        ("sftp://h/a/b.txt", "/a/b.txt"),
        ("sftp://h", "/"),
        ("sftp://h/", "/"),
        ("sftp://h/a%20b/c%23d", "/a b/c#d"),
        ("sftp://u:p@h:2222/a/./b/../c", "/a/c"),
    ],
)
def test_the_wire_path_of_a_request_is_the_decoded_path_of_the_uri(url, wire):
    backend = FakeBackend()
    URIPath(url, backend=backend).stat()
    assert backend.fake.stats == [wire]
    assert backend.asked == 1


def test_a_backend_given_to_the_path_is_used_whatever_host_the_uri_names():
    first, second = FakeBackend(), FakeBackend()
    URIPath("sftp://one/a", backend=first).stat()
    URIPath("sftp://two/a", backend=second).stat()
    assert (first.fake.stats, second.fake.stats) == (["/a"], ["/a"])


# --- the connections of one server are shared ------------------------------------


def test_the_children_of_a_fresh_root_open_one_connection(
    kind, sftp_loopback, sftp_root
):
    for name in "abcdefgh":
        (sftp_root / name).write_bytes(name.encode())
    backend = uri_backend(kind)
    with backend:
        root = URIPath(sftp_loopback.url(), backend=backend)
        assert sorted(child.read_bytes() for child in root.iterdir()) == [
            name.encode() for name in "abcdefgh"
        ]
    assert sftp_loopback.connections == 1


def test_separately_built_paths_with_one_backend_share_the_connection(
    kind, sftp_loopback, sftp_root
):
    (sftp_root / "a").write_bytes(b"a")
    backend = uri_backend(kind)
    with backend:
        first = URIPath(sftp_loopback.url("a"), backend=backend)
        second = URIPath(sftp_loopback.url("a"), backend=backend)
        assert (first.read_bytes(), second.read_bytes()) == (b"a", b"a")
    assert sftp_loopback.connections == 1


def test_the_same_backend_serves_each_server_and_account_on_a_connection_of_its_own(
    kind, sftp_loopbacks, sftp_root, tmp_path
):
    other_root = tmp_path / "other"
    other_root.mkdir()
    one, two = sftp_loopbacks(sftp_root), sftp_loopbacks(other_root)
    (sftp_root / "a").write_bytes(b"one")
    (other_root / "a").write_bytes(b"two")
    backend = uri_backend(kind)
    with backend:
        contents = [
            URIPath(one.url("a"), backend=backend).read_bytes(),
            URIPath(two.url("a"), backend=backend).read_bytes(),
            URIPath(one.url("a", user="other"), backend=backend).read_bytes(),
        ]
    assert contents == [b"one", b"two", b"one"]
    assert (one.connections, two.connections) == (2, 1)


def test_the_user_and_password_of_the_uri_are_the_ones_the_server_gets(
    kind, sftp_loopbacks, sftp_root
):
    server = sftp_loopbacks(sftp_root, password=ANY_PASSWORD)
    (sftp_root / "a").write_bytes(b"a")
    backend = uri_backend(kind)
    with backend:
        path = URIPath(server.url("a", user="alice", password="pw"), backend=backend)
        assert path.read_bytes() == b"a"
    assert server.logins == [("alice", "pw")]


def test_closing_the_backend_closes_its_connections_and_a_later_use_connects_again(
    kind, sftp_loopback, sftp_root
):
    (sftp_root / "a").write_bytes(b"a")
    backend = uri_backend(kind)
    path = URIPath(sftp_loopback.url("a"), backend=backend)
    assert path.read_bytes() == b"a"
    backend.close()
    backend.close()
    assert path.read_bytes() == b"a"
    backend.close()
    assert sftp_loopback.connections == 2


def test_backend_for_gives_one_backend_per_server_and_account_with_the_options_given():
    class Recording(BaseSFTPBackend):
        def __init__(self, host, port, *, user, password, **options):
            self.arguments = (host, port, user, password, options)

    owner = SFTPURIBackend(Recording, timeout=7)
    path = URIPath("sftp://Alice:pw@Host.Example:2200/x")
    core = owner.backend_for(path.source)
    assert core.arguments == ("Host.Example", 2200, "Alice", "pw", {"timeout": 7})
    assert (
        owner.backend_for(URIPath("sftp://Alice:pw@host.example:2200/y").source) is core
    )
    assert (
        owner.backend_for(URIPath("sftp://Alice@host.example:2200/y").source)
        is not core
    )
    assert (
        owner.backend_for(URIPath("sftp://Alice:pw@host.example/y").source) is not core
    )
    assert SFTPURIBackend(Recording, timeout=7).backend_for(path.source) is not core


def test_a_backend_that_outlives_its_owner_is_never_handed_to_another_owner():
    class Recording(BaseSFTPBackend):
        def __init__(self, host, port, *, user, password, **options):
            self.options = options

    source = URIPath("sftp://u@h/x").source
    kept = []
    for number in range(200):
        owner = SFTPURIBackend(Recording, number=number)
        backend = owner.backend_for(source)
        assert backend.options == {"number": number}
        kept.append(backend)
        del owner


# --- the native walks -------------------------------------------------------------


def _spy(calls):
    class Spy(AsyncSSHBackend):
        def tree_copy(self, *args, **kwargs):
            calls.append("tree_copy")
            return super().tree_copy(*args, **kwargs)

        def tree_rm(self, *args, **kwargs):
            calls.append("tree_rm")
            return super().tree_rm(*args, **kwargs)

    return Spy


def _tree(root):
    (root / "d" / "sub").mkdir(parents=True)
    (root / "d" / "a.txt").write_bytes(b"a")
    (root / "d" / "sub" / "b.txt").write_bytes(b"b")


def test_a_copy_and_a_remove_between_two_paths_of_one_server_take_the_native_walk(
    sftp_loopback, sftp_root
):
    pytest.importorskip("asyncssh")
    _tree(sftp_root)
    calls = []
    backend = SFTPURIBackend(
        _spy(calls), connect_opts={"known_hosts": None}, ssh_config=None
    )
    with backend:
        source = URIPath(sftp_loopback.url("d"), backend=backend)
        target = URIPath(sftp_loopback.url("copy"), backend=backend)
        source.copy(target, recursive=True)
        assert (sftp_root / "copy" / "sub" / "b.txt").read_bytes() == b"b"
        assert calls == ["tree_copy"]
        target.rm(recursive=True)
        assert not (sftp_root / "copy").exists()
        assert calls == ["tree_copy", "tree_rm"]


def test_a_copy_to_a_path_the_walk_cannot_reach_is_the_generic_copy(
    sftp_loopback, sftp_root, tmp_path
):
    pytest.importorskip("asyncssh")
    _tree(sftp_root)
    calls = []
    backend = SFTPURIBackend(
        _spy(calls), connect_opts={"known_hosts": None}, ssh_config=None
    )
    with backend:
        source = URIPath(sftp_loopback.url("d"), backend=backend)
        local = tmp_path / "download"
        source.copy(LocalPath(local), recursive=True)
        assert (local / "sub" / "b.txt").read_bytes() == b"b"
    assert calls == []


def test_the_walk_is_not_used_for_a_foreign_target_or_a_second_supplied_backend(
    monkeypatch,
):
    pytest.importorskip("asyncssh")
    calls = []
    monkeypatch.setattr(Path, "copy", lambda self, target, **kw: calls.append(target))
    # A directory source: the fan-out condition must fail on the target alone.
    monkeypatch.setattr(SFTPURIPath, "is_dir", lambda self, **kw: True)
    src = URIPath("sftp://a.invalid/data", backend=AsyncSSHBackend("a.invalid"))
    local = LocalPath("dl").absolute()
    SFTPURIPath.copy(src, local, recursive=True)
    other_host = URIPath(
        "sftp://b.invalid/backup", backend=AsyncSSHBackend("b.invalid")
    )
    SFTPURIPath.copy(src, other_host, recursive=True)
    second = URIPath("sftp://a.invalid/backup", backend=AsyncSSHBackend("a.invalid"))
    SFTPURIPath.copy(src, second, recursive=True)
    shared = SFTPURIBackend(AsyncSSHBackend)
    on_a = URIPath("sftp://a.invalid/data", backend=shared)
    on_b = URIPath("sftp://b.invalid/backup", backend=shared)
    SFTPURIPath.copy(on_a, on_b, recursive=True)
    assert calls == [local, other_host, second, on_b]


def test_rename_and_hardlink_to_another_host_raise_not_implemented():
    src = URIPath("sftp://a.invalid/data/report.csv")
    with pytest.raises(NotImplementedError):
        src.rename(URIPath("sftp://b.invalid/incoming/report.csv"))
    with pytest.raises(NotImplementedError):
        src.rename(LocalPath("report.csv").absolute())
    with pytest.raises(NotImplementedError):
        src.hardlink_to(URIPath("sftp://b.invalid/incoming/report.csv"))


def test_rm_has_the_signature_of_path_rm():
    expected = inspect.signature(Path.rm).parameters
    actual = inspect.signature(SFTPURIPath.rm).parameters
    assert list(actual) == list(expected)
    for name, parameter in expected.items():
        assert actual[name].kind == parameter.kind, name
        assert actual[name].default == parameter.default, name
    core = inspect.signature(SFTPPath.rm).parameters
    assert list(core) == list(expected)


@pytest.mark.parametrize("keyword", ["follow_symlinks", "follow_binds"])
@pytest.mark.parametrize("recursive", [False, True])
def test_rm_checks_the_policies_before_anything_is_sent(recursive, keyword):
    backend = FakeBackend()
    path = URIPath("sftp://host/dir", backend=backend)
    with pytest.raises(ValueError, match=keyword):
        path.rm(recursive=recursive, **{keyword: "yes"})
    assert backend.asked == 0


# --- text, copies, pickles ---------------------------------------------------------


def test_a_path_with_userinfo_pickles_with_it_and_no_more():
    path = URIPath("sftp://user:S3CR3T-dummy@h/x")
    assert SECRET in pickle.dumps(path)  # the URI text itself carries it
    again = pickle.loads(pickle.dumps(path))
    assert again.source.parsed_userinfo() == ("user", "S3CR3T-dummy")
    assert SECRET not in pickle.dumps(URIPath("sftp://user@h/x"))


def test_a_supplied_backend_is_not_in_the_pickle_whatever_it_holds():
    paramiko = pytest.importorskip("paramiko")  # noqa: F841
    backend = SFTPURIBackend(SFTPBackend, connect_opts={"password": "S3CR3T-dummy"})
    path = URIPath("sftp://u@h/x", backend=backend)
    data = pickle.dumps(path)
    assert SECRET not in data
    again = pickle.loads(data)
    assert again == URIPath("sftp://u@h/x")
    assert again._supplied_backend() is None


def test_a_core_backend_given_to_a_path_is_not_in_its_pickle():
    paramiko = pytest.importorskip("paramiko")  # noqa: F841
    backend = SFTPBackend("h", user="u", password="S3CR3T-dummy")
    path = URIPath("sftp://u@h/x", backend=backend)
    assert SECRET not in pickle.dumps(path)
