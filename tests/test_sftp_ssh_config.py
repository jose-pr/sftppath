"""What reaches a connection, and a ProxyCommand's argv, from a host, a port, a
user and an ssh_config, on both backends.

Nothing here connects and nothing is spawned: `asyncssh.connect` and
`paramiko.ProxyCommand` are replaced by recorders, every config is a file under
`tmp_path` that the test passes explicitly, and HOME is an empty directory so
the developer's own `~/.ssh` is never read.
"""

from __future__ import annotations

import asyncio
import ipaddress
import pathlib
import urllib.parse

import pytest

from sftppath import SFTPPath, _sshconfig
from sftppath._sshconfig import DEFAULT_SSH_CONFIG

try:
    import asyncssh

    from sftppath._asyncssh import AsyncSSHBackend
    from sftppath._asyncssh._connect import _aconnect
except ImportError:
    asyncssh = AsyncSSHBackend = _aconnect = None

try:
    import paramiko

    from sftppath import _paramiko as paramiko_mod
    from sftppath._paramiko import SFTPBackend
except ImportError:
    paramiko = paramiko_mod = SFTPBackend = None

needs_asyncssh = pytest.mark.skipif(asyncssh is None, reason="asyncssh not installed")
needs_paramiko = pytest.mark.skipif(paramiko is None, reason="paramiko not installed")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """An empty home: the default ssh_config would be this one's."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert pathlib.Path.home() == home
    assert _sshconfig._normalize_config_paths(DEFAULT_SSH_CONFIG) == (
        str(home / ".ssh" / "config"),
    )
    return home


def _write(path, text):
    path.write_bytes(text.encode())
    return str(path)


class _Stopped(Exception):
    """Raised by the recorder that stands for `asyncssh.connect`."""


@pytest.fixture
def connects(monkeypatch):
    """The calls of `asyncssh.connect`, which never connects."""
    calls = []

    async def connect(*args, **kwargs):
        calls.append((args, kwargs))
        raise _Stopped

    monkeypatch.setattr(asyncssh, "connect", connect)
    return calls


@pytest.fixture
def proxy_commands(monkeypatch):
    """The command lines given to `paramiko.ProxyCommand`, which starts none."""
    commands = []

    class _Recorded:
        def __init__(self, command_line):
            commands.append(command_line)

        def close(self):
            pass

    monkeypatch.setattr(paramiko, "ProxyCommand", _Recorded)
    return commands


@pytest.fixture
def started(kind, request):
    """What a backend of `kind` started toward a server: the calls of
    `asyncssh.connect`, or the command lines of the `ProxyCommand`s the
    ssh_config asked paramiko for."""
    return request.getfixturevalue(
        "connects" if kind == "asyncssh" else "proxy_commands"
    )


def _backend_class(kind):
    return AsyncSSHBackend if kind == "asyncssh" else SFTPBackend


def _path(flavor, kind, host, *, user=None, ssh_config=None, backends):
    """`/x` on `host` as `user` over a backend of `kind` that reads `ssh_config`:
    an `SFTPPath`, or the `sftp:` URI of the same account, whose backend takes
    the host and the user from the URI."""
    cls = _backend_class(kind)
    if flavor == "path":
        backend = cls(host, user=user, ssh_config=ssh_config)
        backends.append(backend)
        return SFTPPath("/x", backend=backend)
    from uripath import URIPath

    from sftppath.uri import SFTPURIBackend

    backend = SFTPURIBackend(cls, ssh_config=ssh_config)
    backends.append(backend)
    userinfo = "" if user is None else urllib.parse.quote(user, safe="") + "@"
    return URIPath(
        f"sftp://{userinfo}{urllib.parse.quote(host, safe='')}/x", backend=backend
    )


# --- the port ----------------------------------------------------------------

_PORT_CONFIG = "Host box\n  HostName 127.0.0.1\n  Port 4242\n  User cfg\n"


@needs_asyncssh
def test_asyncssh_leaves_the_port_to_the_ssh_config_when_none_is_given(
    tmp_path, connects
):
    config = _write(tmp_path / "config", _PORT_CONFIG)
    backend = AsyncSSHBackend("box", ssh_config=config)

    with pytest.raises(_Stopped):
        backend.client()

    ((args, kwargs),) = connects
    assert args == ("box",)
    assert "port" not in kwargs
    assert "username" not in kwargs
    # What asyncssh then connects to, from the arguments it was given.
    options = asyncssh.SSHClientConnectionOptions(
        host=args[0], config=kwargs["config"], known_hosts=None
    )
    assert (options.host, options.port) == ("127.0.0.1", 4242)


@needs_asyncssh
def test_asyncssh_connects_to_the_port_and_user_named_over_the_ssh_config(
    tmp_path, connects
):
    config = _write(tmp_path / "config", _PORT_CONFIG)
    backend = AsyncSSHBackend("box", 2222, user="alice", ssh_config=config)

    with pytest.raises(_Stopped):
        backend.client()

    ((args, kwargs),) = connects
    assert args == ("box",)
    assert kwargs["port"] == 2222
    assert kwargs["username"] == "alice"


@needs_paramiko
def test_paramiko_takes_the_host_name_port_and_user_of_the_ssh_config(tmp_path):
    config = _write(
        tmp_path / "config",
        "Host box\n  HostName real.example\n  Port 4242\n  User cfg\n",
    )

    opts = SFTPBackend("box", ssh_config=config).opts()

    assert (opts["hostname"], opts["port"], opts["username"]) == (
        "real.example",
        4242,
        "cfg",
    )


@needs_paramiko
def test_paramiko_connects_to_the_port_and_user_named_over_the_ssh_config(tmp_path):
    config = _write(
        tmp_path / "config",
        "Host box\n  HostName real.example\n  Port 4242\n  User cfg\n",
    )

    opts = SFTPBackend("box", 2222, user="alice", ssh_config=config).opts()

    assert (opts["hostname"], opts["port"], opts["username"]) == (
        "real.example",
        2222,
        "alice",
    )


# --- hosts that are not plain host names ------------------------------------------

_ACCEPTED = [
    "example.com",
    "host",
    "host_name",
    "_dmarc.example",
    "a-b.c_d",
    "host.example.",
    "Example.COM",
    "1.2.3.4",
    "xn--bcher-kva.example",
    "bücher.example",
    "日本語.example",
    "é.example",
    "::1",
    "2001:db8::1",
    "fe80::1%eth0",
    "fe80::1%12",
    ipaddress.IPv4Address("10.0.0.1"),
    ipaddress.IPv6Address("2001:db8::2"),
    ipaddress.IPv6Address("fe80::1%eth0"),
    None,
    "",
]

_REFUSED = [
    "host --flag",
    "-oProxyCommand=calc",
    "-",
    "--host",
    'ho"st',
    "ho'st",
    "ho`st",
    "ho$st",
    "ho;st",
    "ho|st",
    "ho&st",
    "ho<st",
    "ho>st",
    "ho(st)",
    "ho{st}",
    "ho\\st",
    "ho/st",
    "ho*st",
    "ho?st",
    "ho~st",
    "ho%st",
    "ho st",
    "ho\tst",
    "ho\nst",
    "ho\r\nst",
    "ho\x00st",
    "ho\x1fst",
    "ho\x7fst",
    "ho st",
    "ho st",
    "ho‮st",
    "bücher --flag",
    "host:22",
    "[::1]",
    "::1;calc",
    "fe80::1%",
    "fe80::1%a b",
    "1:2",
]


@pytest.mark.parametrize("host", _ACCEPTED, ids=repr)
def test_a_plain_host_name_or_address_is_accepted(host):
    _sshconfig._check_host(host)


@pytest.mark.parametrize("host", _REFUSED, ids=repr)
def test_a_host_that_is_not_a_plain_name_or_address_is_refused_naming_it(host):
    with pytest.raises(ValueError) as refused:
        _sshconfig._check_host(host)
    assert repr(host) in str(refused.value)


_HOSTILE_HOSTS = [
    "host --evil-flag",
    "-oProxyCommand=calc",
    'host" "second',
    "ho\x00st",
    "ho\nst",
    "ho;calc",
]


# The check is where a lookup or a connection starts, not where a path is built:
# a path to such a host can still be named, joined and compared.


@pytest.mark.parametrize("host", _HOSTILE_HOSTS, ids=repr)
def test_pure_path_operations_on_a_path_to_a_refused_host_still_work(host):
    path = SFTPPath("/x", host=host, user="alice", ssh_config=None)

    assert path.name == "x"
    assert (path / "y").name == "y"
    assert (path / "y").parent == path
    assert path.with_name("z").name == "z"
    assert path.parent.name == ""
    assert path == SFTPPath("/x", host=host, user="alice", ssh_config=None)


@pytest.mark.parametrize("host", _HOSTILE_HOSTS, ids=repr)
def test_pure_path_operations_on_a_uri_with_a_refused_host_still_work(host):
    uripath = pytest.importorskip("uripath")
    import sftppath.uri  # noqa: F401  (registers the scheme)

    url = f"sftp://alice@{urllib.parse.quote(host, safe='')}/x"
    path = uripath.URIPath(url)

    assert path.name == "x"
    assert (path / "y").name == "y"
    assert path.with_name("z").name == "z"
    assert path.parent.name == ""
    assert str(path).startswith("sftp://alice@")
    assert path == uripath.URIPath(url)


@pytest.mark.parametrize("host", _HOSTILE_HOSTS, ids=repr)
def test_a_refused_host_fails_every_request_before_anything_starts(
    host, kind, flavor, tmp_path, backends, started
):
    config = _write(tmp_path / "config", "Host *\n  ProxyCommand prog %h %p %r\n")
    path = _path(flavor, kind, host, user="alice", ssh_config=config, backends=backends)

    for operation in (
        path.stat,
        lambda: list(path.iterdir()),
        path.read_bytes,
        path.rmdir,
    ):
        with pytest.raises(ValueError, match="plain host name"):
            operation()

    assert started == []


@needs_paramiko
@pytest.mark.parametrize("host", _HOSTILE_HOSTS, ids=repr)
def test_paramiko_refuses_a_host_before_reading_a_config_or_starting_a_proxy(
    host, tmp_path, monkeypatch, proxy_commands
):
    config = _write(tmp_path / "config", "Host *\n  ProxyCommand prog %h %p %r\n")
    looked_up = []
    real = paramiko_mod._lookup_ssh_config
    monkeypatch.setattr(
        paramiko_mod,
        "_lookup_ssh_config",
        lambda *args: looked_up.append(args) or real(*args),
    )
    backend = SFTPBackend(host, ssh_config=config)

    with pytest.raises(ValueError, match="plain host name"):
        backend.opts()
    with pytest.raises(ValueError, match="plain host name"):
        backend._known_hosts_files()
    with pytest.raises(ValueError, match="plain host name"):
        backend.transport()
    with pytest.raises(ValueError, match="plain host name"):
        backend.client()

    assert looked_up == []
    assert proxy_commands == []


@needs_asyncssh
@pytest.mark.parametrize("host", _HOSTILE_HOSTS, ids=repr)
def test_asyncssh_refuses_a_host_before_it_connects(host, tmp_path, connects):
    config = _write(tmp_path / "config", "Host *\n  ProxyCommand prog %h %p\n")
    backend = AsyncSSHBackend(host, ssh_config=config)

    with pytest.raises(ValueError, match="plain host name"):
        backend.client()
    with pytest.raises(ValueError, match="plain host name"):
        asyncio.run(_aconnect(host, None, None, None, connect_opts={"config": config}))

    assert connects == []


# --- ProxyCommand tokens (paramiko) -------------------------------------------------


def _proxy_command(
    tmp_path, proxy_commands, config_text, host, port=None, *, user=None
):
    config = _write(tmp_path / "config", config_text)
    SFTPBackend(host, port, user=user, ssh_config=config).opts()
    (command,) = proxy_commands
    return command


@needs_paramiko
def test_proxy_command_tokens_are_the_backends_host_port_and_user(
    tmp_path, proxy_commands
):
    command = _proxy_command(
        tmp_path,
        proxy_commands,
        "Host *\n  ProxyCommand prog %h %p %r %n\n",
        "example.invalid",
        2222,
        user="alice",
    )

    assert command == "prog example.invalid 2222 alice example.invalid"


_ALIAS_CONFIG = (
    "Host box\n"
    "  HostName real.example\n"
    "  Port 2200\n"
    "  User cfg\n"
    "  ProxyCommand prog %h %p %r %n\n"
)


@needs_paramiko
def test_proxy_command_tokens_are_the_ssh_configs_where_the_backend_names_none(
    tmp_path, proxy_commands
):
    command = _proxy_command(tmp_path, proxy_commands, _ALIAS_CONFIG, "box")

    assert command == "prog real.example 2200 cfg box"


@needs_paramiko
def test_proxy_command_tokens_follow_the_backend_over_the_ssh_config(
    tmp_path, proxy_commands
):
    command = _proxy_command(
        tmp_path, proxy_commands, _ALIAS_CONFIG, "box", 2222, user="alice"
    )

    assert command == "prog real.example 2222 alice box"


@needs_paramiko
def test_proxy_command_percent_percent_is_a_percent_and_values_are_not_rescanned(
    tmp_path, proxy_commands
):
    command = _proxy_command(
        tmp_path,
        proxy_commands,
        "Host *\n  ProxyCommand prog %%h %% 100%% %r %p\n",
        "host",
        user="al%pice",
    )

    assert command == "prog %h % 100% al%pice 22"


@needs_paramiko
def test_proxy_command_uses_the_local_user_when_none_is_chosen(
    tmp_path, proxy_commands, monkeypatch
):
    monkeypatch.setattr(_sshconfig._getpass, "getuser", lambda: "localuser")

    command = _proxy_command(
        tmp_path, proxy_commands, "Host *\n  ProxyCommand prog %r\n", "host"
    )

    assert command == "prog localuser"


@needs_paramiko
@pytest.mark.parametrize(
    "user",
    ["al ice", "-oFoo", 'al"ice', "alice;calc", "alice$x", "alice\nx"],
    ids=repr,
)
def test_proxy_command_refuses_a_user_that_would_split_the_command_line(
    user, tmp_path, proxy_commands
):
    config = _write(tmp_path / "config", "Host *\n  ProxyCommand prog %h %r\n")

    with pytest.raises(ValueError, match="ProxyCommand"):
        SFTPBackend("host", user=user, ssh_config=config).opts()

    assert proxy_commands == []


@needs_paramiko
def test_a_user_is_only_checked_where_the_command_holds_it(tmp_path, proxy_commands):
    command = _proxy_command(
        tmp_path,
        proxy_commands,
        "Host *\n  ProxyCommand prog %h %p\n",
        "host",
        user="al ice",
    )

    assert command == "prog host 22"


@needs_paramiko
def test_no_proxy_is_started_for_proxy_command_none(tmp_path, proxy_commands):
    config = _write(tmp_path / "config", "Host *\n  ProxyCommand none\n")

    opts = SFTPBackend("host", ssh_config=config).opts()

    assert "sock" not in opts
    assert proxy_commands == []


# --- a conditional Include ---------------------------------------------------------


def _lookup(config, host):
    return paramiko_mod._lookup_ssh_config(host, config)


def _include_config(tmp_path, home, included, config):
    (home / ".ssh" / "included.conf").write_bytes(included.encode())
    return _write(tmp_path / "config", config)


@needs_paramiko
def test_blocks_of_a_file_included_inside_a_host_block_apply_to_that_host_only(
    tmp_path, home
):
    config = _include_config(
        tmp_path,
        home,
        "User inner-default\nHost *\n  User leaked-user\n  ProxyCommand leaked %h\n",
        "Host internal\n  Include included.conf\n  Port 2200\nHost *\n  Port 22\n",
    )

    unrelated = _lookup(config, "unrelated.example")
    assert "user" not in unrelated
    assert "proxycommand" not in unrelated
    assert unrelated["port"] == "22"

    internal = _lookup(config, "internal")
    # The included file's own lines and blocks apply where the enclosing one
    # does, and the including file's following lines keep their block.
    assert internal["user"] == "inner-default"
    assert internal["proxycommand"] == "leaked %h"
    assert internal["port"] == "2200"


@needs_paramiko
@needs_asyncssh
def test_a_conditional_include_resolves_as_asyncssh_resolves_it(tmp_path, home):
    config = _include_config(
        tmp_path,
        home,
        "User inner-default\nHost *\n  User leaked-user\n",
        "Host internal\n  Include included.conf\n  Port 2200\nHost *\n  Port 22\n",
    )

    ours = _lookup(config, "internal")
    theirs = asyncssh.SSHClientConnectionOptions(
        config=[config], host="internal", known_hosts=None
    )
    assert (ours["user"], int(ours["port"])) == (theirs.username, theirs.port)

    ours = _lookup(config, "unrelated.example")
    theirs = asyncssh.SSHClientConnectionOptions(
        config=[config], host="unrelated.example", known_hosts=None
    )
    assert "user" not in ours
    assert theirs.username != "leaked-user"
    assert int(ours["port"]) == theirs.port


@needs_paramiko
def test_an_included_block_needs_both_its_own_pattern_and_the_enclosing_one(
    tmp_path, home
):
    config = _include_config(
        tmp_path,
        home,
        "Host box1\n  User one\nHost other\n  User other\n",
        "Host box*\n  Include included.conf\n  Compression yes\n",
    )

    assert _lookup(config, "box1")["user"] == "one"
    assert _lookup(config, "box1")["compression"] == "yes"
    assert "user" not in _lookup(config, "box2")
    assert _lookup(config, "box2")["compression"] == "yes"
    # `other` is in the included file but outside `box*`.
    assert "user" not in _lookup(config, "other")
    assert "compression" not in _lookup(config, "other")


@needs_paramiko
def test_a_negated_enclosing_pattern_keeps_its_exception(tmp_path, home):
    config = _include_config(
        tmp_path,
        home,
        "Host *\n  User from-include\n",
        "Host * !skip\n  Include included.conf\n",
    )

    assert _lookup(config, "anything")["user"] == "from-include"
    assert "user" not in _lookup(config, "skip")


@needs_paramiko
def test_a_conditional_include_inside_a_match_block(tmp_path, home):
    config = _include_config(
        tmp_path,
        home,
        "Port 2200\nHost b*\n  User b-user\nHost c*\n  User c-user\n",
        "Match originalhost a*,b*\n  Include included.conf\n",
    )

    assert _lookup(config, "a1")["port"] == "2200"
    assert "user" not in _lookup(config, "a1")
    assert _lookup(config, "b1")["user"] == "b-user"
    # `c*` is in the included file but outside the enclosing Match.
    assert "port" not in _lookup(config, "c1")
    assert "user" not in _lookup(config, "c1")


@needs_paramiko
def test_includes_nested_in_a_conditional_include_stay_conditional(tmp_path, home):
    (home / ".ssh" / "deep.conf").write_bytes(b"Host *\n  User deep\n")
    config = _include_config(
        tmp_path,
        home,
        "Host inner\n  Include deep.conf\n",
        "Host outer\n  Include included.conf\n",
    )

    assert "user" not in _lookup(config, "inner")
    assert "user" not in _lookup(config, "outer")


@needs_paramiko
def test_includes_nested_under_both_conditions_apply_where_both_hold(tmp_path, home):
    (home / ".ssh" / "deep.conf").write_bytes(b"User deep\nHost x*\n  User deep-x\n")
    config = _include_config(
        tmp_path,
        home,
        "Host o*\n  Include deep.conf\n",
        "Host o*\n  Include included.conf\n",
    )

    assert _lookup(config, "outer")["user"] == "deep"
    assert "user" not in _lookup(config, "inner")
    # `x*` is in the innermost file but outside `o*`.
    assert "user" not in _lookup(config, "x1")


@needs_paramiko
def test_a_top_level_include_keeps_its_blocks(tmp_path, home):
    config = _include_config(
        tmp_path,
        home,
        "Host alias\n  HostName 10.9.8.7\nHost *\n  User everyone\n",
        "Include included.conf\nHost late\n  Port 1\n",
    )

    assert _lookup(config, "alias")["hostname"] == "10.9.8.7"
    assert _lookup(config, "anything")["user"] == "everyone"
    assert _lookup(config, "late")["port"] == "1"


@needs_paramiko
def test_an_included_match_all_applies_wherever_the_enclosing_block_does(
    tmp_path, home
):
    config = _include_config(
        tmp_path,
        home,
        "Match all\n  User from-match-all\n",
        "Host here\n  Include included.conf\n",
    )

    assert _lookup(config, "here")["user"] == "from-match-all"
    assert "user" not in _lookup(config, "elsewhere")


# --- the user in an asyncssh ProxyCommand ----------------------------------------

_PROXY_WITH_USER = "Host *\n  ProxyCommand ssh -W %h:%p -l %r bastion\n"
_PROXY_WITHOUT_USER = "Host *\n  ProxyCommand ssh -W %h:%p bastion\n"
# A no-break space is one asyncssh turns into a plain space (SASLprep).
_SPLITTING_USERS = [
    "a -oProxyCommand=calc",
    "a b",
    "-oProxyCommand=x",
    "a;b",
    "a b",
]


@needs_asyncssh
@pytest.mark.parametrize("user", _SPLITTING_USERS, ids=repr)
def test_asyncssh_refuses_a_user_its_proxy_command_would_pass_on(
    user, tmp_path, connects
):
    config = _write(tmp_path / "config", _PROXY_WITH_USER)
    backend = AsyncSSHBackend("example.invalid", user=user, ssh_config=config)

    with pytest.raises(ValueError, match="refusing SFTP user"):
        backend.client()
    with pytest.raises(ValueError, match="refusing SFTP user"):
        asyncio.run(
            _aconnect(
                "example.invalid", None, user, None, connect_opts={"config": config}
            )
        )

    # Refused before asyncssh was given the user, so nothing was expanded.
    assert connects == []


@needs_asyncssh
def test_asyncssh_would_split_the_proxy_command_at_a_user_with_a_space(tmp_path):
    # What the refusal above prevents: asyncssh's own expansion of `%r`.
    config = _write(tmp_path / "config", _PROXY_WITH_USER)

    options = asyncssh.SSHClientConnectionOptions(
        config=[config],
        host="example.invalid",
        port=22,
        username="a -oProxyCommand=calc",
        known_hosts=None,
    )

    assert "-oProxyCommand=calc" in options.proxy_command


@needs_asyncssh
def test_asyncssh_refuses_the_user_of_a_path_before_it_connects(
    flavor, tmp_path, connects, backends
):
    config = _write(tmp_path / "config", _PROXY_WITH_USER)
    path = _path(
        flavor,
        "asyncssh",
        "example.invalid",
        user="a -oProxyCommand=calc",
        ssh_config=config,
        backends=backends,
    )

    with pytest.raises(ValueError, match="refusing SFTP user"):
        path.stat()

    assert connects == []


@needs_asyncssh
@pytest.mark.parametrize("user", _SPLITTING_USERS, ids=repr)
def test_asyncssh_accepts_such_a_user_when_the_config_does_not_use_it(
    user, tmp_path, connects
):
    config = _write(tmp_path / "config", _PROXY_WITHOUT_USER)
    backend = AsyncSSHBackend("example.invalid", user=user, ssh_config=config)

    with pytest.raises(_Stopped):
        backend.client()

    ((_args, kwargs),) = connects
    assert kwargs["username"] == user


@needs_asyncssh
def test_asyncssh_accepts_such_a_user_when_no_config_is_read(connects):
    backend = AsyncSSHBackend("example.invalid", user="a b", ssh_config=None)

    with pytest.raises(_Stopped):
        backend.client()

    ((_args, kwargs),) = connects
    assert kwargs["username"] == "a b"


@needs_asyncssh
@pytest.mark.parametrize("user", ["alice", "svc.account", "j_doe"])
def test_asyncssh_passes_an_ordinary_user_through_a_proxy_command(
    user, tmp_path, connects
):
    config = _write(tmp_path / "config", _PROXY_WITH_USER)
    backend = AsyncSSHBackend("example.invalid", user=user, ssh_config=config)

    with pytest.raises(_Stopped):
        backend.client()

    ((_args, kwargs),) = connects
    assert kwargs["username"] == user


# --- known_hosts ------------------------------------------------------------------


@needs_paramiko
def test_paramiko_reads_the_users_known_hosts_and_the_ssh_configs_for_the_host(
    tmp_path, home
):
    users = home / ".ssh" / "known_hosts"
    users.write_text("")
    (home / "pinned_hosts").write_text("")
    (home / "other_hosts").write_text("")
    config = _write(
        tmp_path / "config",
        "Host box\n  UserKnownHostsFile ~/pinned_hosts none\n"
        "Host other\n  UserKnownHostsFile ~/other_hosts\n",
    )

    files = SFTPBackend("box", ssh_config=config)._known_hosts_files()

    assert files == [str(users), str(home / "pinned_hosts")]


@needs_paramiko
def test_paramiko_skips_a_default_known_hosts_file_that_does_not_exist(tmp_path):
    config = _write(
        tmp_path / "config", "Host box\n  UserKnownHostsFile ~/missing_hosts\n"
    )

    assert SFTPBackend("box", ssh_config=config)._known_hosts_files() == []


@needs_paramiko
def test_paramiko_known_hosts_none_loads_no_file(home):
    (home / ".ssh" / "known_hosts").write_text("")

    backend = SFTPBackend("box", ssh_config=None, known_hosts=None)

    assert backend._known_hosts_files() == []


@needs_paramiko
@pytest.mark.parametrize("convert", [str, pathlib.Path])
def test_paramiko_loads_exactly_the_one_known_hosts_file_it_is_given(
    tmp_path, home, convert
):
    (home / ".ssh" / "known_hosts").write_text("")
    named = tmp_path / "named_hosts"

    backend = SFTPBackend("box", ssh_config=None, known_hosts=convert(named))

    # A file that is named is not skipped for being absent.
    assert backend._known_hosts_files() == [str(named)]


@needs_paramiko
@pytest.mark.parametrize("sequence", [list, tuple])
def test_paramiko_loads_exactly_the_known_hosts_files_of_a_sequence(
    tmp_path, home, sequence
):
    (home / ".ssh" / "known_hosts").write_text("")
    first, second = tmp_path / "first_hosts", tmp_path / "second_hosts"

    backend = SFTPBackend("box", ssh_config=None, known_hosts=sequence([first, second]))

    assert backend._known_hosts_files() == [str(first), str(second)]


@needs_asyncssh
def test_asyncssh_leaves_known_hosts_to_asyncssh_unless_the_options_name_it(
    tmp_path, connects
):
    config = _write(tmp_path / "config", "Host box\n  UserKnownHostsFile ~/pinned\n")

    with pytest.raises(_Stopped):
        AsyncSSHBackend("box", ssh_config=config).client()

    ((_args, kwargs),) = connects
    assert "known_hosts" not in kwargs


@needs_asyncssh
@pytest.mark.parametrize("known_hosts", [None, "hosts_file"])
def test_asyncssh_passes_a_known_hosts_option_through_as_given(known_hosts, connects):
    backend = AsyncSSHBackend(
        "box", ssh_config=None, connect_opts={"known_hosts": known_hosts}
    )

    with pytest.raises(_Stopped):
        backend.client()

    ((_args, kwargs),) = connects
    assert kwargs["known_hosts"] == known_hosts
