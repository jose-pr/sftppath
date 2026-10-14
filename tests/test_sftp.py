"""`SFTPPath` over fake backends (no server), and the options, connection cache
and check-file requests of the paramiko backend.

A fake backend is a `BaseSFTPBackend` with a `client()` that returns a double of
paramiko's `SFTPClient`; each test asserts what that client was asked.
"""

from __future__ import annotations

import copy
import errno
import io
import pickle
import stat as stat_module
import struct
import weakref

import pytest

pytest.importorskip("paramiko")

import paramiko
import paramiko.message
import paramiko.sftp as paramiko_sftp
from pathlib_next import FileStat
from pathlib_next.sync import PathSyncer, SyncEvent
from sftp_support import make_backend

import sftppath
from sftppath import (
    DEFAULT_SSH_CONFIG,
    BaseSFTPBackend,
    SFTPBackend,
    SFTPEndpoint,
    SFTPPath,
    _checkfile,
    _select,
)


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    # SFTPBackend reads ~/.ssh/config and ~/.ssh/known_hosts by default: a
    # developer's own `Host *` / `Port 2200` / ProxyCommand must not change
    # what these tests see. Path.home() reads USERPROFILE on Windows, HOME
    # elsewhere.
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def select_backend(monkeypatch):
    """`select_backend(name)` makes `SFTPPATH_BACKEND=name` the backend class a
    path builds when it is given none."""

    def select(name):
        monkeypatch.setenv("SFTPPATH_BACKEND", name)
        monkeypatch.setattr(_select, "_resolved_backend_cls", None)

    return select


def _backend_class(kind):
    """The backend class of the SSH library `kind` names, imported on demand."""
    return sftppath.SFTPBackend if kind == "paramiko" else sftppath.AsyncSSHBackend


class _FakeTransport:
    def is_active(self):
        return True

    def close(self):
        pass


class _FakeSock:
    def __init__(self, active=True):
        # paramiko's Channel: `active` is set once on open and never
        # cleared; `closed` is what a dropped connection actually sets.
        self.active = active
        self.closed = False

    def get_transport(self):
        return _FakeTransport()


class _FakeAttr:
    def __init__(self, filename, st_mode=0, st_uid=0, st_gid=0):
        self.filename = filename
        self.st_mode = st_mode
        self.st_size = 0
        self.st_mtime = 0
        self.st_uid = st_uid
        self.st_gid = st_gid


class _FakeSFTPClient:
    def __init__(self):
        self.sock = _FakeSock(True)
        self.close_calls = 0
        self.rename_calls = []
        self.chmod_calls = []
        self.chown_calls = []
        self.symlink_calls = []
        self.link_calls = []
        # Owner reported by stat(), so a partial chown() (one field left
        # "unchanged") has something to read back for the other field.
        self.stat_uid = 501
        self.stat_gid = 20

    def close(self):
        self.close_calls += 1

    def rename(self, path, target):
        self.rename_calls.append((path, target))

    def chmod(self, path, mode, **options):
        self.chmod_calls.append((path, mode, *options.items()))

    def chown(self, path, uid, gid):
        self.chown_calls.append((path, uid, gid))

    def listdir(self, path):
        return ["a", "b"]

    def listdir_attr(self, path):
        return [_FakeAttr("a"), _FakeAttr("b")]

    def stat(self, path):
        return _FakeAttr(path, st_uid=self.stat_uid, st_gid=self.stat_gid)

    def lstat(self, path):
        return self.stat(path)

    def open(self, path, mode, buffering):
        return object()

    def remove(self, path):
        pass

    def rmdir(self, path):
        pass

    def mkdir(self, path, mode):
        pass

    def symlink(self, target, path):
        self.symlink_calls.append((target, path))

    def link(self, target, path):
        self.link_calls.append((target, path))


class _FakeBackend(BaseSFTPBackend):
    def __init__(self):
        self.client_calls = 0
        self._client = _FakeSFTPClient()

    def client(self):
        self.client_calls += 1
        return self._client


def _sftp(path, backend=None):
    return SFTPPath(path, backend=_FakeBackend() if backend is None else backend)


# --- how a path gets its backend ---


class _EndpointBackend(BaseSFTPBackend):
    """Keeps the arguments `SFTPPath` built it with, and fails if anything asks
    it for a client."""

    def __init__(self, host, port=None, *, user=None, password=None, **options):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.options = options
        self.client_calls = 0

    @property
    def endpoint(self):
        return SFTPEndpoint(self.host.lower(), self.port, self.user)

    def client(self):
        self.client_calls += 1
        raise AssertionError("no client may be asked for")


class _EndpointPath(SFTPPath):
    __slots__ = ()
    _default_backend_cls = _EndpointBackend


class _ParamikoPath(SFTPPath):
    __slots__ = ()
    _default_backend_cls = SFTPBackend


def test_endpoint_keywords_build_the_backend_the_class_selects():
    path = _EndpointPath("/srv/x", host="Host", port=2222, user="u", password="pw")

    backend = path.backend
    assert type(backend) is _EndpointBackend
    assert (backend.host, backend.port, backend.user, backend.password) == (
        "Host",
        2222,
        "u",
        "pw",
    )
    assert backend.options == {"ssh_config": DEFAULT_SSH_CONFIG}
    assert path.as_posix() == "/srv/x"


def test_a_host_alone_names_no_port_user_or_password():
    backend = _EndpointPath("/", host="h").backend

    assert (backend.host, backend.port, backend.user, backend.password) == (
        "h",
        None,
        None,
        None,
    )


def test_connect_keywords_reach_the_backend_class_as_given():
    path = _EndpointPath(
        "/",
        host="h",
        ssh_config=None,
        timeout=None,
        connect_opts={"a": 1},
        known_hosts="known",
    )

    assert path.backend.options == {
        "ssh_config": None,
        "timeout": None,
        "connect_opts": {"a": 1},
        "known_hosts": "known",
    }


def test_the_environment_variable_selects_the_backend_class(kind, select_backend):
    select_backend(kind)

    path = SFTPPath("/srv/x", host="h", port=2222, user="u", password="pw")

    backend = path.backend
    assert type(backend) is _backend_class(kind)
    assert backend.endpoint == SFTPEndpoint("h", 2222, "u")
    assert backend.password == "pw"


def test_the_class_of_the_path_wins_over_the_environment_variable(select_backend):
    select_backend("paramiko")

    assert type(_EndpointPath("/", host="h").backend) is _EndpointBackend


def test_an_explicit_backend_wins_over_the_class_and_the_environment(select_backend):
    select_backend("not-a-backend")
    backend = _FakeBackend()

    assert _EndpointPath("/x", backend=backend).backend is backend


def test_an_unknown_environment_selection_fails_when_a_backend_is_built(select_backend):
    select_backend("not-a-backend")

    with pytest.raises(ValueError, match="SFTPPATH_BACKEND"):
        SFTPPath("/x", host="h")


_ENDPOINT_KEYWORDS = [
    {"port": 22},
    {"user": "u"},
    {"password": "pw"},
    {"ssh_config": None},
    {"timeout": 3},
    {"timeout": None},
    {"connect_opts": {}},
    {"known_hosts": None},
]


@pytest.mark.parametrize("keywords", [{"host": "h"}, *_ENDPOINT_KEYWORDS])
def test_backend_with_an_endpoint_keyword_is_a_type_error(keywords):
    backend = _FakeBackend()

    with pytest.raises(TypeError, match="backend="):
        SFTPPath("/x", backend=backend, **keywords)

    assert backend.client_calls == 0


@pytest.mark.parametrize("segments", [("/x",), ("x", "y"), ()])
def test_neither_a_backend_nor_a_host_is_a_type_error(segments):
    with pytest.raises(TypeError, match="host= or backend="):
        SFTPPath(*segments)


@pytest.mark.parametrize("keywords", _ENDPOINT_KEYWORDS)
def test_endpoint_keywords_need_a_host(keywords):
    with pytest.raises(TypeError, match="host="):
        SFTPPath("/x", **keywords)


def test_the_backend_of_a_segment_does_not_stand_in_for_a_host():
    base = _sftp("/srv")

    with pytest.raises(TypeError, match="host="):
        SFTPPath(base, "x", user="u")


def test_derived_paths_keep_the_very_same_backend():
    backend = _FakeBackend()
    base = _sftp("/srv/dir", backend)
    child = base / "a.txt"
    other = _sftp("sub", backend)
    derived = {
        "truediv": child,
        "joinpath": base.joinpath("x", "y"),
        "join with a path": base / other,
        "parent": child.parent,
        "parents": child.parents[1],
        "root": base.parents[-1],
        "with_name": child.with_name("b.txt"),
        "with_suffix": child.with_suffix(".md"),
        "with_segments": base.with_segments("q", "r"),
        "relative_to": child.relative_to(base),
        "iterdir": next(iter(base.iterdir())),
        "rename": child.rename("moved.txt"),
        "constructor": SFTPPath(base, "z"),
    }

    for how, path in derived.items():
        assert type(path) is SFTPPath, how
        assert path.backend is backend, how


def test_a_path_joined_from_an_sftppath_keeps_its_backend():
    backend = _FakeBackend()
    base = _sftp("/srv", backend)

    assert SFTPPath(base, "a", "b").backend is backend
    assert SFTPPath(base, "a", "b").as_posix() == "/srv/a/b"
    assert SFTPPath("rel", _sftp("sub", backend)).backend is backend
    assert SFTPPath("rel", _sftp("sub", backend)).as_posix() == "rel/sub"


def test_an_explicit_backend_wins_over_the_backend_of_a_segment():
    base = _sftp("/srv")
    other = _FakeBackend()

    assert SFTPPath(base, "a", backend=other).backend is other


def test_constructing_and_deriving_paths_asks_no_backend_for_a_client():
    backend = _FakeBackend()
    base = SFTPPath("/srv", backend=backend)
    child = SFTPPath(base, "a.txt")

    for path in (
        child,
        base / "b",
        base.parent,
        child.with_name("c"),
        copy.copy(child),
        copy.deepcopy(child),
        child.relative_to(base),
    ):
        repr(path)
        str(path)
        hash(path)
        path == child
        path.name, path.suffix, path.segments, path.parts
        list(path.parents)
        path._wirepath
        path._node_key()
        path._same_filesystem(child)

    assert backend.client_calls == 0


def test_building_a_path_from_endpoint_keywords_asks_for_no_client():
    path = _EndpointPath("/srv/x", host="h", port=2222, user="u", password="pw")
    derived = [path / "y", path.parent, path.with_name("z"), copy.copy(path)]

    for each in (path, *derived):
        repr(each)
        each.as_uri()

    assert path.backend.client_calls == 0
    assert all(each.backend is path.backend for each in derived)


def test_building_a_path_connects_nothing(kind, select_backend, monkeypatch):
    select_backend(kind)
    backend_cls = _backend_class(kind)
    asked = []
    for name in ("client", "opts", "transport"):
        if hasattr(backend_cls, name):
            monkeypatch.setattr(
                backend_cls,
                name,
                lambda self, *args, _name=name, **kwargs: asked.append(_name),
            )

    path = SFTPPath("/srv/x", host="h", port=2222, user="u", password="pw")
    for each in (path, path / "y", path.parent, path.with_name("z"), copy.copy(path)):
        repr(each)
        each.as_uri()

    assert asked == []
    assert type(path.backend) is backend_cls


# --- the default backend and its ssh_config ---


def test_sftppath_default_backend_uses_system_ssh_config():
    backend = _ParamikoPath("/", host="h").backend

    assert isinstance(backend, SFTPBackend)
    assert backend.ssh_config is DEFAULT_SSH_CONFIG


def test_sftppath_explicit_ssh_config_disables_system_lookup():
    backend = _ParamikoPath("/", host="h", ssh_config=None).backend

    assert isinstance(backend, SFTPBackend)
    assert backend.ssh_config is None


def _ssh_config_of(backend):
    if isinstance(backend, SFTPBackend):
        return backend.ssh_config
    # asyncssh takes the ssh_config as its `config` connect option.
    return backend.connect_opts.get("config", DEFAULT_SSH_CONFIG)


def test_selected_default_backend_uses_system_ssh_config(kind, select_backend):
    select_backend(kind)

    assert _ssh_config_of(SFTPPath("/", host="h").backend) is DEFAULT_SSH_CONFIG


def test_selected_backend_with_an_explicit_ssh_config_disables_system_lookup(
    kind, select_backend, tmp_path
):
    select_backend(kind)
    config = str(tmp_path / "config")

    assert _ssh_config_of(SFTPPath("/", host="h", ssh_config=None).backend) is None
    assert _ssh_config_of(SFTPPath("/", host="h", ssh_config=config).backend) == config


# --- the same server ---


def test_paths_on_one_backend_are_one_tree():
    backend = _FakeBackend()
    first, second = _sftp("/a", backend), _sftp("/b", backend)

    assert first._same_filesystem(second) and second._same_filesystem(first)


def test_paths_on_backends_of_one_endpoint_are_one_tree():
    # The host name is compared without case; the password is not an endpoint.
    first = SFTPPath("/a", backend=SFTPBackend("Host", 22, user="u", password="one"))
    second = SFTPPath("/b", backend=SFTPBackend("host", 22, user="u", password="two"))

    assert first.backend is not second.backend
    assert first._same_filesystem(second) and second._same_filesystem(first)


@pytest.mark.parametrize(
    "other",
    [
        ("host", 2222, "u"),
        ("elsewhere", 22, "u"),
        ("host", 22, "v"),
        ("host", 22, None),
    ],
)
def test_paths_on_backends_of_different_endpoints_are_not_one_tree(other):
    host, port, user = other
    first = SFTPPath("/a", backend=SFTPBackend("host", 22, user="u"))
    second = SFTPPath("/a", backend=SFTPBackend(host, port, user=user))

    assert not first._same_filesystem(second)
    assert not second._same_filesystem(first)


def test_paths_on_backends_without_an_endpoint_are_not_one_tree():
    first, second = _sftp("/a"), _sftp("/a")

    assert first.backend.endpoint is None
    assert not first._same_filesystem(second)


def test_relative_to_needs_one_tree():
    child = SFTPPath("/a/b", backend=SFTPBackend("host", 22, user="u"))
    same = SFTPPath("/a", backend=SFTPBackend("HOST", 22, user="u"))
    elsewhere = SFTPPath("/a", backend=SFTPBackend("elsewhere", 22, user="u"))

    assert child.relative_to(same).as_posix() == "b"
    with pytest.raises(ValueError):
        child.relative_to(elsewhere)


@pytest.mark.parametrize(
    "text", ["x", "/x", "/d/../x", "d/../x", "./x", "//x", "x/", "/../x", "/d/../../x"]
)
def test_node_key_is_the_same_for_every_spelling_of_a_node(text):
    backend = _FakeBackend()
    reference = _sftp("/x", backend)._node_key()

    assert SFTPPath(text, backend=backend)._node_key() == reference
    assert reference[1] == ("x",)


def test_node_key_tells_nodes_and_the_root_apart():
    backend = _FakeBackend()
    keys = {
        text: SFTPPath(text, backend=backend)._node_key()
        for text in ("/x", "/y", "/x/y", "/", "", "/d/..")
    }

    assert [key[1] for key in keys.values()] == [
        ("x",),
        ("y",),
        ("x", "y"),
        (),
        (),
        (),
    ]


def test_node_key_namespace_is_shared_by_one_endpoint_only():
    first = SFTPPath("/x", backend=SFTPBackend("host", 22, user="u"))
    same = SFTPPath("/x", backend=SFTPBackend("HOST", 22, user="u"))
    elsewhere = SFTPPath("/x", backend=SFTPBackend("elsewhere", 22, user="u"))
    fake, another_fake = _sftp("/x"), _sftp("/x")

    keys = [path._node_key() for path in (first, same, elsewhere, fake, another_fake)]

    assert keys[0][0] is keys[1][0]
    assert keys[0][0] is not keys[2][0]
    assert keys[3][0] is fake.backend
    assert keys[3][0] is not keys[4][0]


# --- what a path shows and what it keeps ---

_SECRET = "s3cr3t-pw"


def test_repr_names_the_endpoint_and_never_the_password(kind, select_backend):
    select_backend(kind)
    path = SFTPPath(
        "/etc/x.conf", host="host", port=2222, user="user", password=_SECRET
    )

    shown = repr(path)

    assert "/etc/x.conf" in shown
    assert "host='host'" in shown and "2222" in shown and "user='user'" in shown
    assert _SECRET not in shown
    assert _SECRET not in repr(path.backend)
    assert _SECRET not in repr(path / "child")


def test_str_is_the_path_text_without_the_password():
    path = _EndpointPath("/etc/x.conf", host="host", user="user", password=_SECRET)

    assert str(path) == "/etc/x.conf"
    assert _SECRET not in str(path)


def test_as_uri_never_shows_the_password(kind, select_backend):
    select_backend(kind)
    path = SFTPPath("/etc/x.conf", host="host", user="user", password=_SECRET)

    assert path.as_uri() == "sftp://user@host/etc/x.conf"


@pytest.mark.parametrize(
    "keywords, text, uri",
    [
        ({"host": "h"}, "/etc/x.conf", "sftp://h/etc/x.conf"),
        ({"host": "h", "user": "u"}, "/x", "sftp://u@h/x"),
        ({"host": "h", "port": 2222, "user": "u"}, "/x", "sftp://u@h:2222/x"),
        ({"host": "::1", "port": 2222}, "/x", "sftp://[::1]:2222/x"),
        ({"host": "h", "user": "a@b c"}, "/x", "sftp://a%40b%20c@h/x"),
        ({"host": "h"}, "x/y", "sftp://h/x/y"),
        ({"host": "h"}, "/a b/c?d#e%f", "sftp://h/a%20b/c%3Fd%23e%25f"),
        ({"host": "h"}, "/", "sftp://h/"),
    ],
)
def test_as_uri_spells_the_endpoint_and_quotes_the_path(keywords, text, uri):
    path = _EndpointPath(text, password=_SECRET, **keywords)

    assert path.as_uri() == uri


def test_as_uri_needs_a_backend_with_a_host():
    with pytest.raises(NotImplementedError):
        _sftp("/a.txt").as_uri()


def test_pickle_round_trips_the_text_and_the_backend(kind, select_backend):
    select_backend(kind)
    path = SFTPPath("/srv/a b/x.txt", host="Host", port=2222, user="u", password="pw")

    restored = pickle.loads(pickle.dumps(path))

    assert type(restored) is SFTPPath
    assert restored.as_posix() == "/srv/a b/x.txt"
    assert restored.backend is not path.backend
    assert type(restored.backend) is type(path.backend)
    assert restored.backend.endpoint == SFTPEndpoint("host", 2222, "u")
    assert restored.backend.host == "Host"
    assert restored.backend.password == "pw"


def test_pickle_round_trips_the_class_of_the_path():
    path = _EndpointPath("/srv/x", host="h", port=22, user="u")

    restored = pickle.loads(pickle.dumps(path))

    assert type(restored) is _EndpointPath
    assert type(restored.backend) is _EndpointBackend
    assert restored.backend.endpoint == path.backend.endpoint


def test_paths_pickled_together_share_one_backend():
    path = _EndpointPath("/srv/x", host="h")

    first, second = pickle.loads(pickle.dumps([path, path / "y"]))

    assert second.as_posix() == "/srv/x/y"
    assert first.backend is second.backend
    assert first.backend is not path.backend


def test_a_pickled_path_carries_no_connection(kind, sftp_loopback, backends):
    backend = make_backend(kind, sftp_loopback)
    backends.append(backend)
    path = SFTPPath("/", backend=backend)
    assert path.is_dir()
    assert sftp_loopback.connections == 1

    restored = pickle.loads(pickle.dumps(path))
    backends.append(restored.backend)

    assert restored.backend is not backend
    assert restored.is_dir()
    assert sftp_loopback.connections == 2


def test_copy_and_deepcopy_share_the_backend():
    backend = _FakeBackend()
    path = _sftp("/a/b", backend)

    for duplicate in (copy.copy(path), copy.deepcopy(path)):
        assert duplicate is not path
        assert type(duplicate) is SFTPPath
        assert duplicate.as_posix() == "/a/b"
        assert duplicate.backend is backend

    first, second = copy.deepcopy([path, path / "c"])
    assert first.backend is backend and second.backend is backend


# --- SFTPBackend options ---


def test_opts_maps_host_port_user_password():
    opts = SFTPBackend("host", 2222, user="user", password="pass").opts()

    assert opts["hostname"] == "host"
    assert opts["port"] == 2222
    assert opts["username"] == "user"
    assert opts["password"] == "pass"


def test_opts_default_port_22():
    opts = SFTPBackend("host").opts()

    assert opts["port"] == 22
    assert "username" not in opts
    assert "password" not in opts


def test_opts_merges_connect_opts():
    opts = SFTPBackend("host", connect_opts={"timeout": 5}).opts()

    assert opts["timeout"] == 5


def test_opts_timeout_sets_every_bound_unless_none():
    bounded = SFTPBackend("host", timeout=7.5).opts()
    unbounded = SFTPBackend("host", timeout=None).opts()

    for key in ("timeout", "banner_timeout", "auth_timeout", "channel_timeout"):
        assert bounded[key] == 7.5
        assert key not in unbounded


def test_opts_uses_ssh_config_defaults(tmp_path, monkeypatch):
    class _RecordedProxyCommand:
        commands = []

        def __init__(self, command_line):
            type(self).commands.append(command_line)

    # The real class starts the command as a child process.
    monkeypatch.setattr("paramiko.ProxyCommand", _RecordedProxyCommand)
    # paramiko expands an IdentityFile with this machine's FQDN, which asks
    # the resolver.
    monkeypatch.setattr("socket.getfqdn", lambda *args: "localhost")
    config = tmp_path / "config"
    config.write_text(
        "Host alias-host\n"
        "  HostName real-host\n"
        "  Port 2200\n"
        "  User cfg-user\n"
        "  IdentityFile id_test\n"
        "  ProxyCommand ssh jump nc %h %p\n"
    )

    opts = SFTPBackend("alias-host", ssh_config=str(config)).opts()

    assert opts["hostname"] == "real-host"
    assert opts["port"] == 2200
    assert opts["username"] == "cfg-user"
    assert opts["key_filename"] == ["id_test"]
    # The tokens are expanded with the host name and port of this connection.
    assert _RecordedProxyCommand.commands == ["ssh jump nc real-host 2200"]
    assert isinstance(opts["sock"], _RecordedProxyCommand)


def test_endpoint_credentials_override_ssh_config(tmp_path):
    config = tmp_path / "config"
    config.write_text("Host host\n  Port 2200\n  User cfg-user\n")

    opts = SFTPBackend(
        "host", 2222, user="url-user", password="url-pass", ssh_config=str(config)
    ).opts()

    assert opts["port"] == 2222
    assert opts["username"] == "url-user"
    assert opts["password"] == "url-pass"


# --- client cache keying/invalidation ---
# `SFTPPath._sftpclient` is `self.backend.client()`: caching is each backend's
# own responsibility. `_FakeBackend` does none, so these test the paramiko
# backend's cache and invalidation against a loopback server.


@pytest.fixture
def paramiko_backend(backends):
    def make(server):
        backend = make_backend("paramiko", server)
        backends.append(backend)
        return backend

    return make


def test_sftp_backend_client_cached_across_calls(sftp_loopback, paramiko_backend):
    backend = paramiko_backend(sftp_loopback)

    client1 = backend.client()
    client2 = backend.client()

    assert client1 is client2
    assert sftp_loopback.connections == 1


def test_sftp_backend_client_recreated_when_channel_closed(
    sftp_loopback, sftp_root, paramiko_backend
):
    # A dropped paramiko connection leaves `Channel.active` truthy and sets
    # `closed`; the stale client must be replaced AND its connection closed,
    # not dropped.
    (sftp_root / "a.txt").write_bytes(b"a")
    backend = paramiko_backend(sftp_loopback)
    client1 = backend.client()
    assert client1.sock.get_transport().is_active()
    client1.sock.close()

    client2 = backend.client()

    assert client2 is not client1
    assert client2.listdir(".") == ["a.txt"]
    assert sftp_loopback.connections == 2
    assert not client1.sock.get_transport().is_active()
    assert client2.sock.get_transport().is_active()


def test_sftp_backends_of_different_servers_do_not_share_a_client(
    tmp_path, sftp_loopback, sftp_loopbacks, paramiko_backend
):
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = sftp_loopbacks(other_root)

    first = paramiko_backend(sftp_loopback).client()
    second = paramiko_backend(other).client()

    assert first is not second
    assert (sftp_loopback.connections, other.connections) == (1, 1)


def test_two_sftp_backends_of_one_server_do_not_share_a_client(
    sftp_loopback, paramiko_backend
):
    first = paramiko_backend(sftp_loopback).client()
    second = paramiko_backend(sftp_loopback).client()

    assert first is not second
    assert sftp_loopback.connections == 2


def test_sftp_backend_connect_and_client(backends):
    import unittest.mock

    mock_ssh = unittest.mock.MagicMock()
    mock_transport = unittest.mock.MagicMock()
    mock_sftp = unittest.mock.MagicMock()

    mock_ssh.get_transport.return_value = mock_transport
    mock_transport.open_sftp_client.return_value = mock_sftp
    # A live channel: MagicMock attributes are truthy, so `closed` would
    # otherwise read as a dropped connection.
    mock_sftp.sock.closed = False

    with unittest.mock.patch("paramiko.SSHClient", return_value=mock_ssh):
        backend = SFTPBackend(
            "host",
            2222,
            user="user",
            password="pass",
            connect_opts={"timeout": 10},
            hostkeypolicy="policy",
            ssh_config=None,
        )
        backends.append(backend)

        # Test transport()
        transport = backend.transport()
        assert transport is mock_transport
        mock_ssh.set_missing_host_key_policy.assert_called_with("policy")
        connect_kwargs = mock_ssh.connect.call_args.kwargs
        assert connect_kwargs["timeout"] == 10
        # connect_opts wins for `timeout`; the other bounds keep the default.
        assert connect_kwargs["banner_timeout"] == SFTPBackend.DEFAULT_TIMEOUT
        assert connect_kwargs["auth_timeout"] == SFTPBackend.DEFAULT_TIMEOUT
        assert connect_kwargs["hostname"] == "host"
        assert connect_kwargs["port"] == 2222
        assert connect_kwargs["username"] == "user"
        assert connect_kwargs["password"] == "pass"

        # Test client()
        client = backend.client()
        assert client is mock_sftp
        mock_transport.open_sftp_client.assert_called_once()

        # Test transport raising if None -- and the client is closed.
        mock_ssh.get_transport.return_value = None
        mock_ssh.close.reset_mock()
        with pytest.raises(paramiko.SSHException, match="no transport"):
            backend.transport()
        mock_ssh.close.assert_called_once()


# --- chmod ---


def test_chmod_follow_symlinks_true_delegates():
    backend = _FakeBackend()
    p = _sftp("/a.txt", backend)
    p.chmod(0o644)
    assert backend._client.chmod_calls == [("/a.txt", 0o644)]


def test_chmod_follow_symlinks_false_raises_notimplemented():
    backend = _FakeBackend()
    p = _sftp("/a.txt", backend)
    with pytest.raises(NotImplementedError):
        p.chmod(0o644, follow_symlinks=False)
    assert backend._client.chmod_calls == []


def test_chmod_follow_symlinks_false_reaches_a_client_that_supports_it():
    class _LchmodBackend(_FakeBackend):
        supports_lchmod = True

    backend = _LchmodBackend()
    _sftp("/a.txt", backend).chmod(0o600, follow_symlinks=False)
    assert backend._client.chmod_calls == [
        ("/a.txt", 0o600, ("follow_symlinks", False))
    ]


def test_chmod_accepts_string_octal_mode():
    backend = _FakeBackend()
    p = _sftp("/a.txt", backend)
    p.chmod("0644")
    p.chmod("644")
    # Both spellings must reach the wire as the same int the octal literal
    # means -- decimal 644 (0o1204) would be a different, valid mode.
    assert backend._client.chmod_calls == [("/a.txt", 0o644), ("/a.txt", 0o644)]


def test_chmod_rejects_non_octal_digits():
    p = _sftp("/a.txt")
    with pytest.raises(ValueError):
        p.chmod("0899")


# --- rename ---


def test_rename_uses_the_wire_path_of_the_target():
    backend = _FakeBackend()
    p = _sftp("/a.txt", backend)
    target = SFTPPath("/b.txt", backend=backend)

    moved = p.rename(target)

    # Only the path reaches the wire: no host, scheme or credentials.
    assert backend._client.rename_calls == [("/a.txt", "/b.txt")]
    assert moved.as_posix() == "/b.txt"
    assert moved.backend is backend


def test_rename_reads_a_relative_target_from_the_server_root():
    backend = _FakeBackend()
    p = _sftp("/dir/a.txt", backend)

    p.rename(SFTPPath("b.txt", backend=backend))

    assert backend._client.rename_calls == [("/dir/a.txt", "/b.txt")]


def test_rename_accepts_str_target():
    backend = _FakeBackend()
    p = _sftp("/a.txt", backend)

    moved = p.rename("b.txt")

    assert backend._client.rename_calls == [("/a.txt", "/b.txt")]
    assert moved.as_posix() == "/b.txt"
    assert moved.backend is backend


def test_rename_onto_another_server_is_not_implemented():
    backend = _FakeBackend()
    p = _sftp("/a.txt", backend)

    with pytest.raises(NotImplementedError):
        p.rename(_sftp("/b.txt"))

    assert backend._client.rename_calls == []


# --- operations on the client ---


def test_sftppath_operations():
    class _OperationsFakeSFTPClient(_FakeSFTPClient):
        def __init__(self):
            super().__init__()
            self.actions = []

        def listdir(self, path):
            self.actions.append(("listdir", path))
            return ["file1", "file2"]

        def listdir_attr(self, path):
            self.actions.append(("listdir_attr", path))
            return [_FakeAttr("file1"), _FakeAttr("file2")]

        def stat(self, path):
            self.actions.append(("stat", path))
            return FileStat(is_dir=True)

        def lstat(self, path):
            self.actions.append(("lstat", path))
            return FileStat(is_dir=False)

        def open(self, path, mode, buffering):
            self.actions.append(("open", path, mode, buffering))
            return io.BytesIO(b"data")

        def mkdir(self, path, mode):
            self.actions.append(("mkdir", path, mode))

        def remove(self, path):
            self.actions.append(("remove", path))

        def rmdir(self, path):
            self.actions.append(("rmdir", path))

    class _OperationsFakeBackend(BaseSFTPBackend):
        def __init__(self):
            self._client = _OperationsFakeSFTPClient()

        def client(self):
            return self._client

    backend = _OperationsFakeBackend()
    p = _sftp("/dir", backend)

    # listdir_attr via iterdir (scandir contract: one call for the whole
    # listing, metadata included -- no per-child stat())
    children = list(p.iterdir())
    assert [c.name for c in children] == ["file1", "file2"]
    assert backend._client.actions[-1] == ("listdir_attr", "/dir")

    # stat
    p.stat(follow_symlinks=True)
    assert backend._client.actions[-1] == ("stat", "/dir")
    p.stat(follow_symlinks=False)
    assert backend._client.actions[-1] == ("lstat", "/dir")

    # open
    p.open("r", 1024)
    assert backend._client.actions[-1] == ("open", "/dir", "r", 1024)

    # mkdir
    p.mkdir(0o755)
    assert any(a[0] == "mkdir" for a in backend._client.actions)

    # unlink
    p.unlink(missing_ok=True)
    assert backend._client.actions[-1] == ("remove", "/dir")

    # rmdir
    p.rmdir()
    assert backend._client.actions[-1] == ("rmdir", "/dir")


def test_sftppath_recursive_rm_uses_scandir_metadata_bottom_up():
    class _TreeFakeSFTPClient(_FakeSFTPClient):
        def __init__(self):
            super().__init__()
            self.actions = []
            self.tree = {
                "/root": [("sub", True), ("a.txt", False)],
                "/root/sub": [("b.txt", False)],
            }

        def lstat(self, path):
            self.actions.append(("lstat", path))
            return _FakeAttr(path.rsplit("/", 1)[-1], stat_module.S_IFDIR | 0o755)

        def listdir_attr(self, path):
            self.actions.append(("listdir_attr", path))
            return [
                _FakeAttr(
                    name,
                    (stat_module.S_IFDIR if is_dir else stat_module.S_IFREG) | 0o755,
                )
                for name, is_dir in self.tree[path]
            ]

        def remove(self, path):
            self.actions.append(("remove", path))

        def rmdir(self, path):
            self.actions.append(("rmdir", path))

        def stat(self, path):
            raise AssertionError("recursive rm should use lstat/listdir_attr metadata")

    class _TreeFakeBackend(BaseSFTPBackend):
        def __init__(self):
            self._client = _TreeFakeSFTPClient()

        def client(self):
            return self._client

    backend = _TreeFakeBackend()
    _sftp("/root", backend).rm(recursive=True)

    assert backend._client.actions == [
        ("lstat", "/root"),
        ("listdir_attr", "/root"),
        ("listdir_attr", "/root/sub"),
        ("remove", "/root/sub/b.txt"),
        ("rmdir", "/root/sub"),
        ("remove", "/root/a.txt"),
        ("rmdir", "/root"),
    ]


class _TreeBackend(_FakeBackend):
    """A backend with a walk of its own, which a recursive `rm()` hands the
    directory to."""

    supports_tree = True

    def __init__(self):
        super().__init__()
        self.tree_rm_calls = []

    def tree_rm(self, path, *, missing_ok, on_error):
        self.tree_rm_calls.append((path.as_posix(), missing_ok, on_error))


def test_recursive_rm_is_the_backends_own_walk_where_it_has_one():
    backend = _TreeBackend()

    _sftp("/root", backend).rm(recursive=True, missing_ok=True)

    assert backend.tree_rm_calls == [("/root", True, None)]


def test_recursive_rm_tells_the_backends_walk_whether_to_skip_errors():
    backend = _TreeBackend()
    root = _sftp("/root", backend)

    root.rm(recursive=True, ignore_error=True)
    seen = []
    root.rm(recursive=True, ignore_error=lambda error, path: seen.append(path) or False)

    skip_all, ask = (call[2] for call in backend.tree_rm_calls)
    assert skip_all(OSError(), root) is True
    assert ask(OSError(), root) is False
    assert seen == [root]


def test_rm_that_is_not_recursive_does_not_use_the_backends_walk():
    client = _LinkAndRenameClient({"/f": "file"})
    backend = _TreeBackend()
    backend._client = client

    _sftp("/f", backend).rm()

    assert backend.tree_rm_calls == []
    assert client.remove_calls == ["/f"]


# --- native checksum protocol (protocols/checksum.py::NativeChecksum) ------
# `_FakeBackend` never overrides `checksum()`, so it inherits
# `BaseSFTPBackend.checksum()`'s `notimplemented` stub -- exercising it
# proves the "server/backend doesn't support this" fallback path for free,
# with no extra fixture. A second fake backend below DOES implement it, to
# prove `SFTPPath.checksum()` delegates and returns the digest as-is.


def test_sftppath_checksum_raises_notimplemented_when_backend_lacks_support():
    # Covers the "no native hashing at all" case (a custom backend with no
    # checksum() override) and, transitively, PathSyncer's
    # fallback-to-streaming trigger (checksum.native() catches
    # exactly this).
    p = _sftp("/a.txt")
    with pytest.raises(NotImplementedError):
        p.checksum()


def test_sftppath_checksum_raises_notimplemented_for_minimal_custom_backend():
    # A bare BaseSFTPBackend subclass with only `client()` implemented --
    # the smallest custom backend -- inherits the notimplemented default.
    class _MinimalBackend(BaseSFTPBackend):
        def client(self):
            return _FakeSFTPClient()

    p = _sftp("/a.txt", _MinimalBackend())
    with pytest.raises(NotImplementedError):
        p.checksum()


class _RegularFileSFTPClient(_FakeSFTPClient):
    """A client whose `stat()` reports a regular file. The default `stat()`
    (mode 0) and `listdir_attr()` (always `["a", "b"]`) make any path look like
    a non-empty directory whose children are the same two names, which
    `PathSyncer` would walk forever."""

    def stat(self, path):
        return FileStat(st_mode=stat_module.S_IFREG | 0o644, st_size=len(b"x"))

    def lstat(self, path):
        return self.stat(path)


class _ChecksumCapableBackend(BaseSFTPBackend):
    """A backend that implements native checksums: `SFTPPath.checksum()`
    delegates to `backend.checksum()` and returns its value unchanged, and any
    failure that is not a `NotImplementedError` becomes one, so that a caller
    like `PathSyncer` sees every reason a digest cannot be produced alike."""

    def __init__(self, digests=None, error=None):
        self._client = _RegularFileSFTPClient()
        self.digests = digests or {}
        self.error = error
        self.calls = []

    def client(self):
        return self._client

    def checksum(self, path, algorithm):
        self.calls.append((path._wirepath, algorithm))
        if self.error is not None:
            raise self.error
        return self.digests[algorithm]

    def supported_checksums(self, path):
        return frozenset(self.digests)


def test_sftppath_checksum_delegates_to_backend():
    backend = _ChecksumCapableBackend(digests={"md5": "deadbeef"})
    p = _sftp("/a.txt", backend)
    assert p.checksum() == "deadbeef"
    assert p.checksum("md5") == "deadbeef"
    assert backend.calls == [("/a.txt", "md5"), ("/a.txt", "md5")]


def test_sftppath_checksum_wraps_non_notimplemented_backend_errors():
    # A backend raising something other than NotImplementedError (a
    # transport error, a KeyError for an unadvertised algorithm, ...) must
    # still surface as NotImplementedError to the caller -- the contract is
    # "raise if a genuine digest can't be produced", regardless of why.
    backend = _ChecksumCapableBackend(error=OSError("connection reset"))
    p = _sftp("/a.txt", backend)
    with pytest.raises(NotImplementedError):
        p.checksum()


def test_sftppath_checksum_preserves_explicit_notimplementederror():
    backend = _ChecksumCapableBackend(error=NotImplementedError("no md5 here"))
    p = _sftp("/a.txt", backend)
    with pytest.raises(NotImplementedError, match="no md5 here"):
        p.checksum()


def test_sftppath_supported_checksums_empty_when_backend_lacks_support():
    # _FakeBackend: no checksum()/supported_checksums() override -- inherits
    # BaseSFTPBackend's empty-frozenset default.
    p = _sftp("/a.txt")
    assert p.supported_checksums() == frozenset()


def test_sftppath_supported_checksums_reflects_backend_advertisement():
    backend = _ChecksumCapableBackend(digests={"md5": "deadbeef"})
    p = _sftp("/a.txt", backend)
    assert p.supported_checksums() == frozenset({"md5"})


# --- native checksum: the shared check-file-handle reply parser ------------


def _string(value):
    value = value.encode() if isinstance(value, str) else value
    return struct.pack(">I", len(value)) + value


def test_check_file_reply_parser_accepts_both_draft_shapes():
    digest = bytes.fromhex("deadbeef" * 4)
    bare = _string("md5") + digest
    prefixed = _string("check-file") + _string("md5") + digest

    assert _checkfile.parse_reply(bare, "md5") == "deadbeef" * 4
    assert _checkfile.parse_reply(prefixed, "md5") == "deadbeef" * 4


@pytest.mark.parametrize(
    "payload",
    [
        b"",  # no algorithm name at all
        b"\x00\x00\x00\x09md5",  # name longer than the packet
        _string("sha256") + bytes(32),  # not the algorithm requested
        _string("md5") + bytes(4),  # truncated digest
        _string("md5") + _string(bytes(16)),  # length-prefixed digest
    ],
)
def test_check_file_reply_parser_rejects_malformed_replies(payload):
    with pytest.raises(NotImplementedError):
        _checkfile.parse_reply(payload, "md5")


# --- native checksum: paramiko SFTPBackend wire-level implementation ------
# SFTPBackend speaks the filexfer draft's check-file-handle extension
# directly via paramiko's low-level _request()/CMD_EXTENDED -- these tests
# fake that primitive to prove the request is built correctly (handle,
# algorithm, int64 offset/length, block-size) and the reply is
# parsed/validated correctly. The real round trip, on both backends, is in
# tests/test_sync_sftp_parity.py (which adds the extension to the loopback
# server; OpenSSH has none).

_MD5_DEADBEEF = bytes.fromhex("deadbeef" * 4)


def _check_file_reply(algorithm="md5", digest=_MD5_DEADBEEF, prefixed=False):
    """A draft check-file reply: [string "check-file"] string algorithm,
    then the raw hash bytes as the rest of the packet."""
    msg = paramiko.message.Message()
    if prefixed:
        msg.add_string("check-file")
    msg.add_string(algorithm)
    msg.add_bytes(digest)
    msg.rewind()
    return msg


@pytest.fixture
def paramiko_over(monkeypatch, backends):
    """`paramiko_over(client)`: a paramiko `SFTPBackend` whose connection is
    `client`, a double of paramiko's `SFTPClient`. The backend's connection
    cache, liveness check and check-file bookkeeping run as they do; only what
    leaves the process is replaced: `paramiko.SSHClient.connect` and the
    transport it yields."""

    def over(client):
        if not hasattr(client, "sock"):
            client.sock = _FakeSock(True)
        if not hasattr(client, "close"):
            client.close = lambda: None

        class _Transport:
            def open_sftp_client(self):
                return client

            def close(self):
                pass

        monkeypatch.setattr(paramiko.SSHClient, "connect", lambda self, **options: None)
        monkeypatch.setattr(
            paramiko.SSHClient, "get_transport", lambda self: _Transport()
        )
        # A fresh record of what each connection has shown about the extension.
        monkeypatch.setattr(_checkfile, "_SUPPORT_CACHE", weakref.WeakKeyDictionary())
        backend = SFTPBackend(
            "host",
            hostkeypolicy=paramiko.AutoAddPolicy(),
            ssh_config=None,
            known_hosts=None,
        )
        backends.append(backend)
        return backend

    return over


class _HandleFile:
    """What `SFTPClient.open()` returns for a check-file request: a handle."""

    def __init__(self, handle=b"h"):
        self.handle = handle
        self.closed = False

    def close(self):
        self.closed = True


def test_paramiko_checksum_sends_correct_extended_request(paramiko_over):
    calls = []

    class _FakeParamikoClient:
        def __init__(self):
            self.opened = _HandleFile(b"handle-bytes")

        def open(self, path, mode, buffering=-1):
            calls.append(("open", path, mode))
            return self.opened

        def _request(self, cmd, *args):
            calls.append(("_request", cmd, args))
            return paramiko_sftp.CMD_EXTENDED_REPLY, _check_file_reply()

    fake_client = _FakeParamikoClient()
    backend = paramiko_over(fake_client)

    p = _sftp("/a.txt", backend)
    result = backend.checksum(p, "md5")

    assert result == "deadbeef" * 4
    assert fake_client.opened.closed is True
    assert calls[0] == ("open", "/a.txt", "r")
    _, cmd, args = calls[1]
    assert cmd == paramiko_sftp.CMD_EXTENDED
    # Not "check-file@openssh.com": OpenSSH has no such extension.
    assert args[0] == "check-file-handle"
    assert args[1] == b"handle-bytes"
    assert args[2] == "md5"
    assert int(args[3]) == 0 and int(args[4]) == 0  # start-offset, length
    assert args[5] == 0  # block-size: one hash over the whole file


def test_paramiko_checksum_closes_handle_even_when_request_raises(paramiko_over):
    class _FakeParamikoClient:
        def __init__(self):
            self.opened = _HandleFile()

        def open(self, path, mode, buffering=-1):
            return self.opened

        def _request(self, cmd, *args):
            raise OSError("SSH_FX_OP_UNSUPPORTED")

    fake_client = _FakeParamikoClient()
    backend = paramiko_over(fake_client)

    p = _sftp("/a.txt", backend)
    with pytest.raises(OSError):
        backend.checksum(p, "md5")
    assert fake_client.opened.closed is True

    # SFTPPath.checksum() (not backend.checksum() directly) is what
    # translates this into NotImplementedError for callers/PathSyncer.
    p2 = _sftp("/a.txt", backend)
    with pytest.raises(NotImplementedError):
        p2.checksum()


def test_paramiko_checksum_rejects_mismatched_reply_algorithm(paramiko_over):
    class _FakeParamikoClient:
        def open(self, path, mode, buffering=-1):
            return _HandleFile()

        def _request(self, cmd, *args):
            msg = paramiko.message.Message()
            # Server echoes back a different algorithm than requested --
            # must not be trusted.
            msg.add_string("sha256")
            msg.add_string(b"\x00")
            msg.rewind()
            return paramiko_sftp.CMD_EXTENDED_REPLY, msg

    backend = paramiko_over(_FakeParamikoClient())
    p = _sftp("/a.txt", backend)
    with pytest.raises(NotImplementedError):
        backend.checksum(p, "md5")


def test_paramiko_checksum_rejects_non_extended_reply(paramiko_over):
    class _FakeParamikoClient:
        def open(self, path, mode, buffering=-1):
            return _HandleFile()

        def _request(self, cmd, *args):
            # A CMD_STATUS-shaped success-ish reply that isn't actually
            # CMD_EXTENDED_REPLY must still be rejected.
            return 999, paramiko.message.Message()

    backend = paramiko_over(_FakeParamikoClient())
    p = _sftp("/a.txt", backend)
    with pytest.raises(NotImplementedError):
        backend.checksum(p, "md5")


# --- paramiko SFTPBackend.supported_checksums(): real per-connection probe -


def test_paramiko_supported_checksums_reflects_working_server(paramiko_over):
    class _FakeParamikoClient:
        def open(self, path, mode, buffering=-1):
            return _HandleFile()

        def _request(self, cmd, *args):
            return paramiko_sftp.CMD_EXTENDED_REPLY, _check_file_reply()

    backend = paramiko_over(_FakeParamikoClient())

    p = _sftp("/a.txt", backend)
    supported = backend.supported_checksums(p)

    assert supported == frozenset(_checkfile.ALGORITHMS)


def test_paramiko_supported_checksums_empty_when_server_lacks_extension(paramiko_over):
    class _FakeParamikoClient:
        def open(self, path, mode, buffering=-1):
            return _HandleFile()

        def _request(self, cmd, *args):
            raise OSError("SSH_FX_OP_UNSUPPORTED")

    backend = paramiko_over(_FakeParamikoClient())

    p = _sftp("/a.txt", backend)
    assert backend.supported_checksums(p) == frozenset()


def test_paramiko_supported_checksums_caches_per_connection(paramiko_over):
    class _FakeParamikoClient:
        def __init__(self):
            self.request_calls = 0

        def open(self, path, mode, buffering=-1):
            return _HandleFile()

        def _request(self, cmd, *args):
            self.request_calls += 1
            return paramiko_sftp.CMD_EXTENDED_REPLY, _check_file_reply()

    fake_client = _FakeParamikoClient()
    backend = paramiko_over(fake_client)

    p = _sftp("/a.txt", backend)
    backend.supported_checksums(p)
    backend.supported_checksums(p)
    backend.supported_checksums(p)

    # Only the FIRST call actually probed the server -- subsequent calls
    # for the same connection are served from the cache.
    assert fake_client.request_calls == 1


class _CountingParamikoClient:
    """Fake paramiko client for the check-file tests below: counts opens and
    extension requests, and answers each request with `reply()`."""

    def __init__(self, reply):
        self.reply = reply
        self.opens = 0
        self.requests = 0

    def open(self, path, mode, buffering=-1):
        self.opens += 1
        return _HandleFile()

    def _request(self, cmd, *args):
        self.requests += 1
        return self.reply()


def test_paramiko_checksum_refusal_is_cached_for_the_connection(paramiko_over):
    # OpenSSH answers SSH_FX_OP_UNSUPPORTED (paramiko raises
    # NotImplementedError for that status). Only the first file pays the
    # open + request + close; later ones send nothing.
    def unsupported():
        raise NotImplementedError("Operation unsupported")

    client = _CountingParamikoClient(unsupported)
    backend = paramiko_over(client)
    p = _sftp("/a.txt", backend)

    for _ in range(3):
        with pytest.raises(NotImplementedError):
            p.checksum()

    assert client.requests == 1
    assert client.opens == 1
    assert backend.supported_checksums(p) == frozenset()
    assert client.requests == 1


def test_paramiko_checksum_errno_failure_is_not_cached(paramiko_over):
    # A typed failure (the handle vanished) says nothing about the
    # extension: the next file tries again.
    def missing():
        raise OSError(errno.ENOENT, "No such file")

    client = _CountingParamikoClient(missing)
    backend = paramiko_over(client)
    p = _sftp("/a.txt", backend)

    for _ in range(2):
        with pytest.raises(NotImplementedError):
            p.checksum()
    assert client.requests == 2


def test_paramiko_checksum_accepts_check_file_prefixed_reply(paramiko_over):
    client = _CountingParamikoClient(
        lambda: (paramiko_sftp.CMD_EXTENDED_REPLY, _check_file_reply(prefixed=True))
    )
    backend = paramiko_over(client)
    p = _sftp("/a.txt", backend)
    assert backend.checksum(p, "md5") == "deadbeef" * 4


def test_paramiko_checksum_rejects_digest_of_wrong_length(paramiko_over):
    # A length-prefixed or truncated hash is a reply shape the parser does
    # not understand: never returned as a digest.
    client = _CountingParamikoClient(
        lambda: (
            paramiko_sftp.CMD_EXTENDED_REPLY,
            _check_file_reply(digest=bytes.fromhex("deadbeef")),
        )
    )
    backend = paramiko_over(client)
    p = _sftp("/a.txt", backend)
    with pytest.raises(NotImplementedError):
        backend.checksum(p, "md5")


# --- PathSyncer + SFTP: native path used, and fallback still works --------


def test_pathsyncer_uses_sftp_native_checksum_no_open_when_supported():
    class _RecordingChecksumBackend(_ChecksumCapableBackend):
        def __init__(self, digests):
            super().__init__(digests=digests)
            self.open_paths = []

        def client(self):
            client = super().client()
            real_open = client.open

            def _tracking_open(path, mode, buffering):
                self.open_paths.append(path)
                return real_open(path, mode, buffering)

            client.open = _tracking_open
            return client

    source_backend = _RecordingChecksumBackend({"md5": "same-hash"})
    target_backend = _RecordingChecksumBackend({"md5": "same-hash"})
    source = _sftp("/a.txt", source_backend)
    target = _sftp("/a.txt", target_backend)

    # quick_check=False: this test is specifically about native-checksum
    # preference, not the separately-tested quick_check metadata pre-check
    # (tests/test_checksum.py).
    syncer = PathSyncer(quick_check=False)
    events = []
    syncer._hook = lambda s, t, e, dry: events.append(e)
    syncer.sync(source, target)

    assert SyncEvent.Copy not in events
    assert source_backend.calls == [("/a.txt", "md5")]
    assert target_backend.calls == [("/a.txt", "md5")]
    # The whole point of the feature: neither side's file content was
    # opened/streamed just to decide the sync verdict.
    assert source_backend.open_paths == []
    assert target_backend.open_paths == []


def test_pathsyncer_sftp_falls_back_to_streaming_when_backend_unsupported():
    # _FakeBackend has no checksum() override -- models a real server without
    # check-file-handle support (or the asyncssh backend). PathSyncer must
    # still reach a correct verdict via the streaming fallback, not silently
    # report "in sync".
    source_backend = _FakeBackend()
    target_backend = _FakeBackend()

    class _OpenableSFTPClient(_FakeSFTPClient):
        def __init__(self, content: bytes):
            super().__init__()
            self._content = content

        def stat(self, path):
            return FileStat(is_dir=False)

        def open(self, path, mode, buffering):
            return io.BytesIO(self._content)

    source_backend._client = _OpenableSFTPClient(b"identical-content")
    target_backend._client = _OpenableSFTPClient(b"identical-content")

    source = _sftp("/a.txt", source_backend)
    target = _sftp("/a.txt", target_backend)

    syncer = PathSyncer()
    events = []
    syncer._hook = lambda s, t, e, dry: events.append(e)
    syncer.sync(source, target)

    assert SyncEvent.Copy not in events


# --- chown: sentinel normalization ----------------------------------------
#
# SFTPv3's setstat sends uid/gid as a *paired* attribute, so there is no way
# to set only one. SFTPPath._chown() therefore reads the current owner for
# whichever field the caller left as "unchanged" -- these pin that, since a
# backend that silently sent 0 (or -1) instead would chown to root.


def test_chown_sets_both_fields():
    backend = _FakeBackend()
    p = _sftp("/a.txt", backend)
    p.chown(1000, 1000)
    assert backend._client.chown_calls == [("/a.txt", 1000, 1000)]


def test_chown_uid_only_reads_current_gid():
    backend = _FakeBackend()
    backend._client.stat_uid, backend._client.stat_gid = 501, 20
    p = _sftp("/a.txt", backend)
    p.chown(uid=1000)
    # gid must come from stat(), not a 0/-1 guess.
    assert backend._client.chown_calls == [("/a.txt", 1000, 20)]


def test_chown_gid_only_reads_current_uid():
    backend = _FakeBackend()
    backend._client.stat_uid, backend._client.stat_gid = 501, 20
    p = _sftp("/a.txt", backend)
    p.chown(gid=1000)
    assert backend._client.chown_calls == [("/a.txt", 501, 1000)]


def test_chown_minus_one_is_the_unchanged_sentinel():
    backend = _FakeBackend()
    backend._client.stat_uid, backend._client.stat_gid = 501, 20
    p = _sftp("/a.txt", backend)
    # -1 is os.chown's spelling of "leave alone"; it must mean the same
    # thing here rather than reaching the wire as a literal -1.
    p.chown(-1, 1000)
    assert backend._client.chown_calls == [("/a.txt", 501, 1000)]


def test_chown_all_unchanged_is_a_no_op():
    backend = _FakeBackend()
    p = _sftp("/a.txt", backend)
    p.chown()
    p.chown(-1, -1)
    assert backend._client.chown_calls == []


def test_chown_uid_zero_is_not_treated_as_unset():
    backend = _FakeBackend()
    p = _sftp("/a.txt", backend)
    # uid 0 is root -- a falsy check instead of an `is None` check would
    # silently drop this and leave the file's owner untouched.
    p.chown(0, 0)
    assert backend._client.chown_calls == [("/a.txt", 0, 0)]


def test_chown_names_not_supported_over_sftp():
    p = _sftp("/a.txt")
    with pytest.raises(NotImplementedError):
        p.chown("root", "wheel")


def test_chown_follow_symlinks_false_raises_notimplemented():
    p = _sftp("/a.txt")
    with pytest.raises(NotImplementedError):
        p.chown(1000, 1000, follow_symlinks=False)


# --- destination arguments are literal paths, not URI syntax ---------------
#
# A str destination of rename(), symlink_to() or hardlink_to() is a path: a
# "?", "#" or "%" in it is part of the name, and a name that begins with
# "C:/" is not a scheme. Every assertion below is on the argument the BACKEND
# received, not on the absence of an exception.

# Names with a character that a URI treats specially.
_DEST_NAMES = [
    "rn?b.txt",  # query delimiter
    "rn#b.txt",  # fragment delimiter
    "rn b.txt",  # space
    "rn%20b.txt",  # LITERAL percent-escape: must NOT become "rn b.txt"
    "rn%b.txt",  # bare, un-decodable percent
    "rn:b.txt",  # colon
    "a+b&c=d.txt",  # other sub-delims
]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_rename_str_destination_is_a_literal_path(name):
    backend = _FakeBackend()
    p = _sftp("/mnt/a.txt", backend)
    p.rename(name)
    # Relative str == sibling rename, so it lands beside self.
    assert backend._client.rename_calls == [("/mnt/a.txt", f"/mnt/{name}")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_rename_absolute_str_destination_is_a_literal_path(name):
    backend = _FakeBackend()
    p = _sftp("/mnt/a.txt", backend)
    p.rename(f"/other/{name}")
    assert backend._client.rename_calls == [("/mnt/a.txt", f"/other/{name}")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_symlink_to_str_target_is_a_literal_path(name):
    backend = _FakeBackend()
    p = _sftp("/mnt/link", backend)
    p.symlink_to(f"/mnt/{name}")
    assert backend._client.symlink_calls == [(f"/mnt/{name}", "/mnt/link")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_symlink_to_relative_str_target_stays_relative_and_literal(name):
    backend = _FakeBackend()
    p = _sftp("/mnt/link", backend)
    p.symlink_to(name)
    # Unlike rename()'s destination, a symlink target is stored verbatim --
    # never anchored at self.parent (pathlib.Path.symlink_to() parity).
    assert backend._client.symlink_calls == [(name, "/mnt/link")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_hardlink_to_str_target_is_a_literal_path(name):
    class _HardlinkBackend(_FakeBackend):
        supports_hardlink = True

    backend = _HardlinkBackend()
    p = _sftp("/mnt/link", backend)
    p.hardlink_to(f"/mnt/{name}")
    assert backend._client.link_calls == [(f"/mnt/{name}", "/mnt/link")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_rename_path_object_destination_reaches_the_wire_as_named(name):
    backend = _FakeBackend()
    p = _sftp("/mnt/a.txt", backend)
    target = SFTPPath(f"/mnt/{name}", backend=backend)

    p.rename(target)

    assert backend._client.rename_calls == [("/mnt/a.txt", f"/mnt/{name}")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_symlink_to_path_object_target_reaches_the_wire_as_named(name):
    backend = _FakeBackend()
    p = _sftp("/mnt/link", backend)

    p.symlink_to(SFTPPath(f"/mnt/{name}", backend=backend))

    assert backend._client.symlink_calls == [(f"/mnt/{name}", "/mnt/link")]


def test_hardlink_to_needs_a_backend_that_supports_it():
    backend = _FakeBackend()
    with pytest.raises(NotImplementedError):
        _sftp("/mnt/link", backend).hardlink_to("/mnt/a.txt")
    assert backend._client.link_calls == []


def test_hardlink_to_a_path_of_another_server_is_not_implemented():
    class _HardlinkBackend(_FakeBackend):
        supports_hardlink = True

    backend = _HardlinkBackend()
    with pytest.raises(NotImplementedError):
        _sftp("/mnt/link", backend).hardlink_to(_sftp("/mnt/a.txt", _HardlinkBackend()))
    assert backend._client.link_calls == []


def test_rename_str_destination_keeps_a_windows_style_drive_path():
    # A first segment that ends in ":" is a name, not a scheme.
    backend = _FakeBackend()
    p = _sftp("/mnt/a.txt", backend)
    p.rename("C:/Temp/x.txt")
    assert backend._client.rename_calls == [("/mnt/a.txt", "/mnt/C:/Temp/x.txt")]


def test_symlink_to_str_target_keeps_dot_dot_relative():
    backend = _FakeBackend()
    p = _sftp("/mnt/sub/link", backend)
    p.symlink_to("../real.txt")
    assert backend._client.symlink_calls == [("../real.txt", "/mnt/sub/link")]


# --- rename / unlink / readlink ---------------------------------------------


class _LinkAndRenameClient(_FakeSFTPClient):
    """Fake client with a tiny namespace: `entries` maps a path to "file" or
    ("link", target); stat() follows links, lstat() does not."""

    def __init__(self, entries, *, posix_rename=True, posix_error=None):
        super().__init__()
        self.entries = dict(entries)
        self.posix_rename_calls = []
        self.remove_calls = []
        self._posix_error = posix_error
        if not posix_rename:
            self.posix_rename = None

    def _attr(self, path):
        entry = self.entries.get(path)
        if entry is None:
            raise FileNotFoundError(2, "No such file", path)
        if isinstance(entry, tuple):
            return FileStat(st_mode=stat_module.S_IFLNK | 0o777)
        return FileStat(st_mode=stat_module.S_IFREG | 0o644)

    def lstat(self, path):
        return self._attr(path)

    def stat(self, path):
        entry = self.entries.get(path)
        if isinstance(entry, tuple):
            return self._attr(entry[1])
        return self._attr(path)

    def remove(self, path):
        self.remove_calls.append(path)
        if path not in self.entries:
            raise FileNotFoundError(2, "No such file", path)
        del self.entries[path]

    def readlink(self, path):
        return self.entries[path][1]

    def posix_rename(self, path, target):
        self.posix_rename_calls.append((path, target))
        if self._posix_error is not None:
            raise self._posix_error
        self.entries[target] = self.entries.pop(path)

    def rename(self, path, target):
        self.rename_calls.append((path, target))
        if target in self.entries:
            raise OSError("Failure")
        self.entries[target] = self.entries.pop(path)


def _link_backend(client):
    backend = _FakeBackend()
    backend._client = client
    return backend


def test_unlink_missing_ok_removes_dangling_link_without_exists_probe():
    client = _LinkAndRenameClient({"/current": ("link", "/releases/41")})
    link = _sftp("/current", _link_backend(client))
    assert not link.exists()

    link.unlink(missing_ok=True)

    assert client.remove_calls == ["/current"]
    assert "/current" not in client.entries


def test_unlink_missing_ok_ignores_only_a_missing_entry():
    client = _LinkAndRenameClient({})
    path = _sftp("/gone", _link_backend(client))
    path.unlink(missing_ok=True)
    with pytest.raises(FileNotFoundError):
        path.unlink()


def test_symlink_to_force_repoints_a_dangling_link():
    client = _LinkAndRenameClient({"/current": ("link", "/releases/41")})
    link = _sftp("/current", _link_backend(client))

    link.symlink_to("/releases/42", force=True)

    assert client.symlink_calls == [("/releases/42", "/current")]


def test_rename_uses_posix_rename_to_replace_existing_target():
    client = _LinkAndRenameClient({"/a.txt": "file", "/b.txt": "file"})
    path = _sftp("/a.txt", _link_backend(client))

    path.rename("b.txt")

    assert client.posix_rename_calls == [("/a.txt", "/b.txt")]
    assert client.rename_calls == []


def test_rename_without_posix_rename_raises_file_exists_error():
    client = _LinkAndRenameClient(
        {"/a.txt": "file", "/b.txt": "file"}, posix_rename=False
    )
    path = _sftp("/a.txt", _link_backend(client))

    with pytest.raises(FileExistsError) as excinfo:
        path.rename("b.txt")

    assert excinfo.value.errno == errno.EEXIST
    assert client.entries == {"/a.txt": "file", "/b.txt": "file"}


def test_rename_falls_back_when_posix_rename_is_unsupported():
    # asyncssh: NotImplementedError (the extension was not advertised).
    client = _LinkAndRenameClient(
        {"/a.txt": "file"}, posix_error=NotImplementedError("unsupported")
    )
    path = _sftp("/a.txt", _link_backend(client))

    path.rename("c.txt")

    assert client.rename_calls == [("/a.txt", "/c.txt")]
    assert "/c.txt" in client.entries


def test_rename_paramiko_generic_failure_is_learned_as_unsupported():
    # paramiko renders SSH_FX_OP_UNSUPPORTED as OSError without errno: once a
    # plain rename then succeeds, later renames skip the extension request.
    client = _LinkAndRenameClient(
        {"/a.txt": "file", "/b.txt": "file"},
        posix_error=OSError("Operation unsupported"),
    )
    backend = _link_backend(client)

    _sftp("/a.txt", backend).rename("c.txt")
    _sftp("/b.txt", backend).rename("d.txt")

    assert client.posix_rename_calls == [("/a.txt", "/c.txt")]
    assert client.rename_calls == [("/a.txt", "/c.txt"), ("/b.txt", "/d.txt")]


def test_rename_errno_failure_from_posix_rename_propagates():
    client = _LinkAndRenameClient(
        {"/a.txt": "file"}, posix_error=PermissionError(13, "denied")
    )
    path = _sftp("/a.txt", _link_backend(client))
    with pytest.raises(PermissionError):
        path.rename("c.txt")
    assert client.rename_calls == []


def test_readlink_relative_target_is_printable_and_comparable():
    client = _LinkAndRenameClient({"/srv/app/current": ("link", "releases/42")})
    backend = _link_backend(client)
    link = _sftp("/srv/app/current", backend)

    target = link.readlink()

    assert target.as_posix() == "releases/42"
    assert str(target) == "releases/42"
    assert "releases/42" in repr(target)
    assert target == link.readlink()
    assert target.name == "42"
    assert target.backend is backend


def test_readlink_collapses_dot_segments_and_keeps_the_stored_text_verbatim():
    client = _LinkAndRenameClient({"/srv/current": ("link", "../x/./y")})
    link = _sftp("/srv/current", _link_backend(client))

    assert link.readlink().as_posix() == "../x/y"
    assert link._readlink_text() == "../x/./y"


def test_readlink_absolute_target_stays_on_the_backend():
    client = _LinkAndRenameClient({"/srv/current": ("link", "/releases/42")})
    backend = _link_backend(client)
    link = _sftp("/srv/current", backend)

    target = link.readlink()

    assert target.as_posix() == "/releases/42"
    assert target.backend is backend


# --- a listing is untrusted input -----------------------------------------


#: What a malicious or buggy server can put in a directory listing. Each
#: would escape the directory being walked if it became a child path.
_HOSTILE_LISTING_NAMES = ["..", ".", "../victim.txt", "sub/../../victim.txt", ""]


def _client_listing(names):
    class _HostileClient(_FakeSFTPClient):
        def listdir(self, path):
            return list(names)

        def listdir_attr(self, path):
            return [_FakeAttr(name) for name in names]

    class _HostileBackend(_FakeBackend):
        def __init__(self):
            super().__init__()
            self._client = _HostileClient()

    return _HostileBackend()


def test_a_hostile_listing_name_never_becomes_a_child():
    """`rm(recursive=True)` and a recursive copy walk what the LISTING says
    is there. A server-chosen `../victim.txt` would be a path outside the
    tree, and the destination-side guard cannot catch it: by then the name has
    collapsed to a harmless-looking `victim.txt`."""
    backend = _client_listing(_HOSTILE_LISTING_NAMES + ["ok.txt"])
    directory = SFTPPath("/src", backend=backend)

    assert [p.name for p in directory.iterdir()] == ["ok.txt"]
    assert [name for name, _stat in directory._scandir()] == ["ok.txt"]
    # Nothing yielded can leave the directory it was listed from.
    for child in directory.iterdir():
        assert child.as_posix().startswith("/src/")
        assert ".." not in child.as_posix().split("/")


def test_a_listing_name_with_a_separator_is_dropped():
    """A name carrying "/" is two components, not one: joining it would
    reach a path the listing does not describe."""
    backend = _client_listing(["a/b", "ok.txt"])
    directory = SFTPPath("/src", backend=backend)
    assert [p.name for p in directory.iterdir()] == ["ok.txt"]


def test_check_file_reply_naming_another_algorithm_is_refused_at_the_requested_size():
    # The digest has the length of the requested algorithm, so only the
    # echoed name tells the server hashed something else.
    for payload in (
        _string("md5") + bytes(32),
        _string("check-file") + _string("sha1") + bytes(32),
    ):
        with pytest.raises(NotImplementedError, match="requested 'sha256'"):
            _checkfile.parse_reply(payload, "sha256")
    assert _checkfile.parse_reply(_string("sha256") + bytes(32), "sha256") == "00" * 32


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", True),
        ("localhost", True),
        ("LocalHost", True),
        ("files.localhost", True),
        ("10.0.0.5", False),
        ("2001:db8::1", False),
        ("files.example.org", False),
    ],
)
def test_is_local_is_true_for_a_loopback_address_and_localhost(host, local):
    assert SFTPPath("/x", host=host).is_local() is local


def test_a_backend_without_a_host_is_local():
    assert SFTPPath("/x", backend=_FakeBackend()).is_local() is True
