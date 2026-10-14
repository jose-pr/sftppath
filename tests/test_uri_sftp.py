"""The `sftp:` scheme of uripath (`SFTPURIPath`) over fake backends (no server):
the text of a URI path, how the URI reaches the connection options of a
backend, and that destinations given as text or as paths reach the client as
the paths they name.

A fake backend is a `BaseSFTPBackend` with a `client()` that returns a double of
paramiko's `SFTPClient`, used as it is by every path whatever host its URI names.
"""

from __future__ import annotations

import copy
import errno
import os
import pickle
import stat as stat_module
import urllib.parse

import pytest

pytest.importorskip("uripath")

from pathapi import FileStat
from uripath import URIPath

import sftppath
from sftppath import DEFAULT_SSH_CONFIG, BaseSFTPBackend
from sftppath.uri import SFTPURIBackend, SFTPURIPath


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
def paramiko_class():
    """The paramiko `SFTPBackend` class, or a skip."""
    pytest.importorskip("paramiko")
    return sftppath.SFTPBackend


def _backend_class(kind):
    """The backend class of the SSH library `kind` names, imported on demand."""
    return sftppath.SFTPBackend if kind == "paramiko" else sftppath.AsyncSSHBackend


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
        self.rename_calls = []
        self.chmod_calls = []
        self.chown_calls = []
        self.symlink_calls = []
        self.link_calls = []
        # Owner reported by stat(), so a partial chown() (one field left
        # "unchanged") has something to read back for the other field.
        self.stat_uid = 501
        self.stat_gid = 20

    def rename(self, path, target):
        self.rename_calls.append((path, target))

    def chmod(self, path, mode, **options):
        self.chmod_calls.append((path, mode, *options.items()))

    def chown(self, path, uid, gid):
        self.chown_calls.append((path, uid, gid))

    def listdir_attr(self, path):
        return [_FakeAttr("a"), _FakeAttr("b")]

    def stat(self, path):
        return _FakeAttr(path, st_uid=self.stat_uid, st_gid=self.stat_gid)

    def lstat(self, path):
        return self.stat(path)

    def symlink(self, target, path):
        self.symlink_calls.append((target, path))

    def link(self, target, path):
        self.link_calls.append((target, path))


class _FakeBackend(BaseSFTPBackend):
    def __init__(self, client=None):
        self.client_calls = 0
        self._client = _FakeSFTPClient() if client is None else client

    def client(self):
        self.client_calls += 1
        return self._client


def _uri(url, backend=None):
    return URIPath(url, backend=_FakeBackend() if backend is None else backend)


# --- the text of a path ---


def test_a_url_with_the_scheme_is_an_sftp_uri_path():
    assert type(_uri("sftp://host/a.txt")) is SFTPURIPath


def test_fspath_returns_host_path_for_sftp():
    # sftp: is a _host_filesystem_path scheme: __fspath__ returns the
    # path on the URI's OWN host, for building a command line that runs
    # there -- not a locally-openable path.
    p = _uri("sftp://user:secret@host/etc/x.conf")
    assert os.fspath(p) == "/etc/x.conf"


def test_host_fspath_returns_path_for_sftp():
    p = _uri("sftp://user:secret@host/etc/x.conf")
    assert p.host_fspath() == "/etc/x.conf"


def test_str_drops_password_but_host_fspath_and_path_do_not():
    p = _uri("sftp://user:secret@host/etc/x.conf")
    assert "secret" not in str(p)
    assert "secret" not in repr(p)
    assert p.host_fspath() == "/etc/x.conf"
    assert p.path == "/etc/x.conf"
    # full-fidelity round trip (with credentials) is as_uri(sanitize=False)
    assert p.as_uri(sanitize=False) == "sftp://user:secret@host/etc/x.conf"


def test_pickle_round_trips_the_uri_and_the_ssh_config():
    p = SFTPURIPath("sftp://user:secret@host:2222/a b/x.txt", ssh_config=None)

    restored = pickle.loads(pickle.dumps(p))

    assert type(restored) is SFTPURIPath
    assert restored.as_uri(sanitize=False) == p.as_uri(sanitize=False)
    assert restored.path == "/a b/x.txt"
    assert restored.backend.options == {"ssh_config": None}


def test_pickle_of_a_default_ssh_config_stays_the_default():
    p = SFTPURIPath("sftp://user@host/x")

    restored = pickle.loads(pickle.dumps(p))

    assert restored.backend.options["ssh_config"] is DEFAULT_SSH_CONFIG


def test_pickle_leaves_out_a_connection_it_was_given():
    backend = _FakeBackend()
    p = _uri("sftp://user:secret@host/x", backend)

    restored = pickle.loads(pickle.dumps(p))

    assert restored.as_uri(sanitize=False) == "sftp://user:secret@host/x"
    assert restored.backend is not backend
    assert isinstance(restored.backend, SFTPURIBackend)


def test_copy_and_deepcopy_share_the_backend():
    backend = _FakeBackend()
    p = _uri("sftp://user:secret@host/a/b", backend)

    for duplicate in (copy.copy(p), copy.deepcopy(p)):
        assert duplicate is not p
        assert duplicate == p
        assert duplicate.backend is backend


# --- how a URI reaches the connection options ---


def test_the_uri_builds_the_endpoint_of_the_backend(paramiko_class):
    uri_backend = SFTPURIBackend(paramiko_class, ssh_config=None)
    p = URIPath("sftp://user:pass@Host:2222/x", backend=uri_backend)

    backend = uri_backend.backend_for(p.source)

    assert isinstance(backend, paramiko_class)
    assert (backend.host, backend.port, backend.user, backend.password) == (
        "Host",
        2222,
        "user",
        "pass",
    )
    opts = backend.opts()
    assert opts["hostname"] == "Host"
    assert opts["port"] == 2222
    assert opts["username"] == "user"
    assert opts["password"] == "pass"


def test_a_uri_without_port_or_credentials_names_none(paramiko_class):
    uri_backend = SFTPURIBackend(paramiko_class, ssh_config=None)
    p = URIPath("sftp://host/x", backend=uri_backend)

    backend = uri_backend.backend_for(p.source)

    assert (backend.port, backend.user, backend.password) == (None, None, None)
    opts = backend.opts()
    assert opts["port"] == 22
    assert "username" not in opts
    assert "password" not in opts


def test_uri_credentials_override_ssh_config(tmp_path, paramiko_class):
    config = tmp_path / "config"
    config.write_text("Host host\n  Port 2200\n  User cfg-user\n")
    uri_backend = SFTPURIBackend(paramiko_class, ssh_config=str(config))
    p = URIPath("sftp://url-user:url-pass@host:2222/x", backend=uri_backend)

    opts = uri_backend.backend_for(p.source).opts()

    assert opts["port"] == 2222
    assert opts["username"] == "url-user"
    assert opts["password"] == "url-pass"


def test_a_uri_without_credentials_takes_the_ssh_config(tmp_path, paramiko_class):
    config = tmp_path / "config"
    config.write_text(
        "Host alias\n  HostName real-host\n  Port 2200\n  User cfg-user\n"
    )
    uri_backend = SFTPURIBackend(paramiko_class, ssh_config=str(config))
    p = URIPath("sftp://alias/x", backend=uri_backend)

    opts = uri_backend.backend_for(p.source).opts()

    assert opts["hostname"] == "real-host"
    assert opts["port"] == 2200
    assert opts["username"] == "cfg-user"


def test_paths_of_one_endpoint_share_a_backend_and_other_endpoints_do_not(
    paramiko_class,
):
    uri_backend = SFTPURIBackend(paramiko_class, ssh_config=None)
    paths = {
        name: URIPath(url, backend=uri_backend)
        for name, url in {
            "first": "sftp://u:p@host:22/a",
            "same": "sftp://u:p@HOST:22/b",
            "user": "sftp://v:p@host:22/a",
            "port": "sftp://u:p@host:2222/a",
            "host": "sftp://u:p@other/a",
        }.items()
    }

    backends = {name: uri_backend.backend_for(p.source) for name, p in paths.items()}

    assert backends["first"] is backends["same"]
    for name in ("user", "port", "host"):
        assert backends[name] is not backends["first"], name


def test_a_supplied_backend_instance_is_used_for_every_host():
    backend = _FakeBackend()

    first = _uri("sftp://one/a.txt", backend)
    second = _uri("sftp://two/a.txt", backend)

    assert first.backend is backend and second.backend is backend
    first.chmod(0o600)
    second.chmod(0o600)
    assert backend._client.chmod_calls == [("/a.txt", 0o600), ("/a.txt", 0o600)]


# --- the default backend and its ssh_config ---


@pytest.fixture
def pinned(paramiko_class):
    """An `SFTPURIPath` subclass that pins the paramiko backend class as its
    default, without touching the environment."""

    class _PinnedURIPath(SFTPURIPath):
        __slots__ = ()
        _default_backend_cls = paramiko_class

    return _PinnedURIPath


def test_sftppath_default_backend_uses_system_ssh_config(pinned, paramiko_class):
    p = pinned("sftp://host/")

    assert p.backend.backend_class is paramiko_class
    backend = p.backend.backend_for(p.source)
    assert isinstance(backend, paramiko_class)
    assert backend.ssh_config is DEFAULT_SSH_CONFIG


def test_sftppath_explicit_ssh_config_disables_system_lookup(pinned, paramiko_class):
    p = pinned("sftp://host/", ssh_config=None)

    backend = p.backend.backend_for(p.source)
    assert isinstance(backend, paramiko_class)
    assert backend.ssh_config is None


def test_the_environment_variable_selects_the_backend_class_of_a_uri_path(
    kind, monkeypatch
):
    monkeypatch.setenv("SFTPPATH_BACKEND", kind)
    monkeypatch.setattr(sftppath._select, "_resolved_backend_cls", None)
    p = URIPath("sftp://user:pw@host:2222/x")

    backend = p.backend.backend_for(p.source)

    assert type(backend) is _backend_class(kind)
    assert backend.endpoint == sftppath.SFTPEndpoint("host", 2222, "user")


# --- chmod and chown through the URI ---


def test_chmod_follow_symlinks_true_delegates():
    backend = _FakeBackend()
    _uri("sftp://host/a.txt", backend).chmod(0o644)
    assert backend._client.chmod_calls == [("/a.txt", 0o644)]


def test_chmod_follow_symlinks_false_raises_notimplemented():
    backend = _FakeBackend()
    with pytest.raises(NotImplementedError):
        _uri("sftp://host/a.txt", backend).chmod(0o644, follow_symlinks=False)
    assert backend._client.chmod_calls == []


def test_chown_uid_only_reads_current_gid():
    backend = _FakeBackend()
    backend._client.stat_uid, backend._client.stat_gid = 501, 20
    _uri("sftp://host/a.txt", backend).chown(uid=1000)
    assert backend._client.chown_calls == [("/a.txt", 1000, 20)]


def test_chown_names_not_supported_over_sftp():
    with pytest.raises(NotImplementedError):
        _uri("sftp://host/a.txt").chown("root", "wheel")


# --- rename: the wire path of the target, not the URI text ---


def test_rename_uses_target_path_not_as_posix():
    backend = _FakeBackend()
    p = _uri("sftp://host/a.txt", backend)
    target = URIPath("sftp://host/b.txt", backend=backend)

    moved = p.rename(target)

    # as_posix() of a URI is "host:/b.txt"; the SFTP wire protocol only wants
    # the raw path.
    assert backend._client.rename_calls == [("/a.txt", "/b.txt")]
    assert moved.path == "/b.txt"
    assert moved.backend is backend


def test_rename_accepts_str_target():
    backend = _FakeBackend()
    p = _uri("sftp://host/a.txt", backend)

    moved = p.rename("b.txt")

    assert backend._client.rename_calls == [("/a.txt", "/b.txt")]
    assert moved.path == "/b.txt"


# --- destination arguments are decoded paths, not URI syntax ---
#
# A str destination of rename(), symlink_to() or hardlink_to() is a decoded
# path, not URI text: a "?" or "#" in it is part of the name, a "%20" is those
# three characters, and a name that begins with "C:/" is not a scheme. Every
# assertion below is on the argument the BACKEND received, not on the absence
# of an exception.

# Names with a character that a URI treats specially.
_DEST_NAMES = [
    "rn?b.txt",  # query delimiter
    "rn#b.txt",  # fragment delimiter
    "rn b.txt",  # space
    "rn%20b.txt",  # LITERAL percent-escape: must NOT become "rn b.txt"
    "rn%b.txt",  # bare, un-decodable percent
    "rn:b.txt",  # colon must stay readable (a "/C:/Temp" path depends on it)
    "a+b&c=d.txt",  # other sub-delims
]

# RFC 3986 pchar + "/" -- ":" deliberately left unencoded so that a
# "/C:/Temp/..." path stays readable in the URI.
_PATH_SAFE = "/:@-._~!$&'()*+,;="


def _encoded(path):
    return urllib.parse.quote(path, safe=_PATH_SAFE)


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_rename_str_destination_is_a_literal_path(name):
    backend = _FakeBackend()
    p = _uri("sftp://host/mnt/a.txt", backend)
    p.rename(name)
    # Relative str == sibling rename, so it lands beside self.
    assert backend._client.rename_calls == [("/mnt/a.txt", f"/mnt/{name}")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_rename_absolute_str_destination_is_a_literal_path(name):
    backend = _FakeBackend()
    p = _uri("sftp://host/mnt/a.txt", backend)
    p.rename(f"/other/{name}")
    assert backend._client.rename_calls == [("/mnt/a.txt", f"/other/{name}")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_symlink_to_str_target_is_a_literal_path(name):
    backend = _FakeBackend()
    p = _uri("sftp://host/mnt/link", backend)
    p.symlink_to(f"/mnt/{name}")
    assert backend._client.symlink_calls == [(f"/mnt/{name}", "/mnt/link")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_symlink_to_relative_str_target_stays_relative_and_literal(name):
    backend = _FakeBackend()
    p = _uri("sftp://host/mnt/link", backend)
    p.symlink_to(name)
    # Unlike rename()'s destination, a symlink target is stored verbatim --
    # never anchored at self.parent (pathlib.Path.symlink_to() parity).
    assert backend._client.symlink_calls == [(name, "/mnt/link")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_hardlink_to_str_target_is_a_literal_path(name):
    class _HardlinkBackend(_FakeBackend):
        supports_hardlink = True

    backend = _HardlinkBackend()
    p = _uri("sftp://host/mnt/link", backend)
    p.hardlink_to(f"/mnt/{name}")
    assert backend._client.link_calls == [(f"/mnt/{name}", "/mnt/link")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_rename_path_object_destination_is_not_double_decoded(name):
    """A destination that is already a path OBJECT must reach the wire
    exactly once-decoded: a literal "%20" in the name is the case that catches
    a second decode."""
    backend = _FakeBackend()
    p = _uri("sftp://host/mnt/a.txt", backend)
    target = URIPath(f"sftp://host{_encoded(f'/mnt/{name}')}", backend=backend)
    assert target.path == f"/mnt/{name}"

    p.rename(target)

    assert backend._client.rename_calls == [("/mnt/a.txt", f"/mnt/{name}")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_symlink_to_path_object_target_is_not_double_decoded(name):
    backend = _FakeBackend()
    p = _uri("sftp://host/mnt/link", backend)
    target = URIPath(f"sftp://host{_encoded(f'/mnt/{name}')}", backend=backend)

    p.symlink_to(target)

    assert backend._client.symlink_calls == [(f"/mnt/{name}", "/mnt/link")]


def test_rename_str_destination_keeps_a_windows_style_drive_path():
    # A first segment that ends in ":" is a name, not a URI scheme:
    # "C:/Temp/x.txt" is not scheme "c" with the path "/Temp/x.txt".
    backend = _FakeBackend()
    p = _uri("sftp://host/mnt/a.txt", backend)
    p.rename("C:/Temp/x.txt")
    assert backend._client.rename_calls == [("/mnt/a.txt", "/mnt/C:/Temp/x.txt")]


def test_symlink_to_str_target_keeps_dot_dot_relative():
    backend = _FakeBackend()
    p = _uri("sftp://host/mnt/sub/link", backend)
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
    return _FakeBackend(client)


def test_unlink_missing_ok_removes_dangling_link_without_exists_probe():
    client = _LinkAndRenameClient({"/current": ("link", "/releases/41")})
    link = _uri("sftp://host/current", _link_backend(client))
    assert not link.exists()

    link.unlink(missing_ok=True)

    assert client.remove_calls == ["/current"]
    assert "/current" not in client.entries


def test_unlink_missing_ok_ignores_only_a_missing_entry():
    client = _LinkAndRenameClient({})
    path = _uri("sftp://host/gone", _link_backend(client))
    path.unlink(missing_ok=True)
    with pytest.raises(FileNotFoundError):
        path.unlink()


def test_symlink_to_force_repoints_a_dangling_link():
    client = _LinkAndRenameClient({"/current": ("link", "/releases/41")})
    link = _uri("sftp://host/current", _link_backend(client))

    link.symlink_to("/releases/42", force=True)

    assert client.symlink_calls == [("/releases/42", "/current")]


def test_rename_uses_posix_rename_to_replace_existing_target():
    client = _LinkAndRenameClient({"/a.txt": "file", "/b.txt": "file"})
    path = _uri("sftp://host/a.txt", _link_backend(client))

    path.rename("b.txt")

    assert client.posix_rename_calls == [("/a.txt", "/b.txt")]
    assert client.rename_calls == []


def test_rename_without_posix_rename_raises_file_exists_error_naming_the_uri():
    client = _LinkAndRenameClient(
        {"/a.txt": "file", "/b.txt": "file"}, posix_rename=False
    )
    path = _uri("sftp://user:secret@host/a.txt", _link_backend(client))

    with pytest.raises(FileExistsError) as excinfo:
        path.rename("b.txt")

    assert excinfo.value.errno == errno.EEXIST
    assert client.entries == {"/a.txt": "file", "/b.txt": "file"}
    # The error names the destination as the caller wrote it, with no secret.
    assert excinfo.value.filename == "sftp://user@host/b.txt"


def test_rename_falls_back_when_posix_rename_is_unsupported():
    # asyncssh: NotImplementedError (the extension was not advertised).
    client = _LinkAndRenameClient(
        {"/a.txt": "file"}, posix_error=NotImplementedError("unsupported")
    )
    path = _uri("sftp://host/a.txt", _link_backend(client))

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

    _uri("sftp://host/a.txt", backend).rename("c.txt")
    _uri("sftp://host/b.txt", backend).rename("d.txt")

    assert client.posix_rename_calls == [("/a.txt", "/c.txt")]
    assert client.rename_calls == [("/a.txt", "/c.txt"), ("/b.txt", "/d.txt")]


def test_rename_errno_failure_from_posix_rename_propagates():
    client = _LinkAndRenameClient(
        {"/a.txt": "file"}, posix_error=PermissionError(13, "denied")
    )
    path = _uri("sftp://host/a.txt", _link_backend(client))
    with pytest.raises(PermissionError):
        path.rename("c.txt")
    assert client.rename_calls == []


def test_readlink_relative_target_is_printable_and_comparable():
    client = _LinkAndRenameClient({"/srv/app/current": ("link", "releases/42")})
    link = _uri("sftp://host/srv/app/current", _link_backend(client))

    target = link.readlink()

    assert target.path == "releases/42"
    assert str(target) == "releases/42"
    assert "releases/42" in repr(target)
    assert target == link.readlink()
    assert target.name == "42"


def test_readlink_keeps_dot_segments_verbatim():
    client = _LinkAndRenameClient({"/srv/current": ("link", "../x/./y")})
    link = _uri("sftp://host/srv/current", _link_backend(client))
    assert link.readlink().path == "../x/./y"


def test_readlink_absolute_target_keeps_the_host():
    client = _LinkAndRenameClient({"/srv/current": ("link", "/releases/42")})
    link = _uri("sftp://host/srv/current", _link_backend(client))

    target = link.readlink()

    assert target.path == "/releases/42"
    assert target.source == link.source


# --- a listing is untrusted input -----------------------------------------


#: What a malicious or buggy server can put in a directory listing. Each
#: would escape the directory being walked if it became a child path.
_HOSTILE_LISTING_NAMES = ["..", ".", "../victim.txt", "sub/../../victim.txt", ""]


def _client_listing(names):
    class _HostileClient(_FakeSFTPClient):
        def listdir_attr(self, path):
            return [_FakeAttr(name) for name in names]

    return _FakeBackend(_HostileClient())


def test_a_hostile_listing_name_never_becomes_a_child():
    """`rm(recursive=True)` and a recursive copy walk what the LISTING says
    is there. A server-chosen `../victim.txt` would be a path outside the
    tree, and the destination-side guard cannot catch it: by then the name has
    collapsed to a harmless-looking `victim.txt`."""
    backend = _client_listing(_HOSTILE_LISTING_NAMES + ["ok.txt"])
    directory = _uri("sftp://host/src", backend)

    assert [p.name for p in directory.iterdir()] == ["ok.txt"]
    assert [name for name, _stat in directory._scandir()] == ["ok.txt"]
    # Nothing yielded can leave the directory it was listed from.
    for child in directory.iterdir():
        assert child.path.startswith("/src/")
        assert ".." not in child.path.split("/")


def test_a_listing_name_with_a_separator_is_dropped():
    """A name carrying "/" is two components, not one: joining it would
    reach a path the listing does not describe."""
    backend = _client_listing(["a/b", "ok.txt"])
    directory = _uri("sftp://host/src", backend)
    assert [p.name for p in directory.iterdir()] == ["ok.txt"]


# --- native checksum through the URI -----------------------------------------


class _ChecksumBackend(_FakeBackend):
    def __init__(self, digests=None, error=None):
        super().__init__()
        self.digests = digests or {}
        self.error = error
        self.calls = []

    def checksum(self, path, algorithm):
        self.calls.append((path._wirepath, algorithm))
        if self.error is not None:
            raise self.error
        return self.digests[algorithm]

    def supported_checksums(self, path):
        return frozenset(self.digests)


def test_checksum_delegates_to_the_backend():
    backend = _ChecksumBackend(digests={"md5": "deadbeef"})
    p = _uri("sftp://host/a.txt", backend)

    assert p.checksum() == "deadbeef"
    assert p.supported_checksums() == frozenset({"md5"})
    assert backend.calls == [("/a.txt", "md5")]


def test_checksum_raises_notimplemented_when_the_backend_cannot_produce_one():
    for error in (OSError("connection reset"), NotImplementedError("no md5 here")):
        p = _uri("sftp://host/a.txt", _ChecksumBackend(error=error))
        with pytest.raises(NotImplementedError):
            p.checksum()
    assert _uri("sftp://host/a.txt").supported_checksums() == frozenset()
