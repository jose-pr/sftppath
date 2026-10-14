"""Which paths of one SFTP endpoint share a backend, and which `ssh_config`
each one reads.

A URI path carries its `ssh_config` to every path derived from it on the same
endpoint and drops it, with the backend, when the derived path is on another
host. Two paths of one endpoint whose `ssh_config` differs do not share the
backend they derive for themselves, whichever of them does I/O first. Nothing
here connects: the default core backend is a recorder that remembers the
`ssh_config` it was built for.
"""

import pytest

pytest.importorskip("uripath")

from uripath.source import Source  # noqa: E402

from sftppath import DEFAULT_SSH_CONFIG, BaseSFTPBackend  # noqa: E402
from sftppath.uri import SFTPURIBackend, SFTPURIPath  # noqa: E402


class _Recorded(BaseSFTPBackend):
    """A core backend that connects to nothing and keeps the `ssh_config` it
    was built with."""

    built = []

    def __init__(self, host, port=None, *, user=None, password=None, ssh_config):
        self.host = host
        self.ssh_config = ssh_config
        type(self).built.append(self)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    _Recorded.built.clear()
    # `with_source()` and a destination string dispatch on the registered
    # class, so the recorder is installed on `SFTPURIPath` itself.
    monkeypatch.setattr(SFTPURIPath, "_default_backend_cls", _Recorded)
    yield
    _Recorded.built.clear()


def path(url="sftp://h/a", **kwargs):
    return SFTPURIPath(url, **kwargs)


def core(sftp_path):
    """The core backend `sftp_path` does its I/O on: the one its family builds
    for its endpoint, or the `BaseSFTPBackend` it was given."""
    backend = sftp_path.backend
    if isinstance(backend, BaseSFTPBackend):
        return backend
    return backend.backend_for(sftp_path.source)


# --- with_source and a join to another host --------------------------------------


def test_with_source_on_the_same_endpoint_keeps_the_ssh_config():
    base = path(ssh_config=None)

    moved = base.with_source(Source("sftp", None, "h", None))

    assert moved._ssh_config is None
    assert core(moved).ssh_config is None
    assert moved.backend is base.backend
    assert core(moved) is core(base)


def test_with_source_on_another_host_takes_neither_the_config_nor_the_backend():
    base = path(ssh_config="/etc/special")
    base.backend

    moved = base.with_source(Source("sftp", None, "other", None))

    assert moved._ssh_config is DEFAULT_SSH_CONFIG
    assert moved.backend is not base.backend
    assert core(moved).ssh_config is DEFAULT_SSH_CONFIG
    assert core(base).ssh_config == "/etc/special"


def test_a_join_onto_another_host_does_not_carry_the_ssh_config():
    base = path("sftp://a/x", ssh_config="CONFIG-A")

    joined = SFTPURIPath(base, "sftp://other/y")

    assert joined.source.host == "other"
    assert joined._ssh_config is DEFAULT_SSH_CONFIG
    assert core(joined).ssh_config is DEFAULT_SSH_CONFIG
    assert joined.backend is not base.backend


def test_a_join_on_the_same_endpoint_carries_the_ssh_config():
    base = path("sftp://a/x", ssh_config="CONFIG-A")

    for joined in (
        SFTPURIPath(base, "y"),
        SFTPURIPath(base, "sftp://a/y"),
        base / "y",
        base.parent,
        base.with_name("z"),
    ):
        assert joined._ssh_config == "CONFIG-A"
        assert core(joined).ssh_config == "CONFIG-A"
        assert joined.backend is base.backend
    assert len(_Recorded.built) == 1


def test_a_str_destination_on_another_host_does_not_carry_the_ssh_config():
    base = path("sftp://a/x", ssh_config="CONFIG-A")

    same = base._coerce_target("sftp://a/y")
    other = base._coerce_target("sftp://b/y")

    assert same._ssh_config == "CONFIG-A"
    assert other._ssh_config is DEFAULT_SSH_CONFIG


# --- relatives with different configurations -------------------------------------


@pytest.mark.parametrize("first", ["a", "b"])
def test_relatives_with_different_configs_do_not_share_a_derived_backend(first):
    a = path("sftp://h/a", ssh_config="CONFIG-A")
    b = SFTPURIPath(a, "b", ssh_config="CONFIG-B")

    for member in (a, b) if first == "a" else (b, a):
        core(member)

    assert core(a).ssh_config == "CONFIG-A"
    assert core(b).ssh_config == "CONFIG-B"
    assert a.backend is not b.backend
    # Each one's own derivations stay with it.
    for child in (a / "x", a.parent, a.with_name("y")):
        assert child.backend is a.backend
    for child in (b / "x", b.parent, b.with_name("y")):
        assert child.backend is b.backend
    assert len(_Recorded.built) == 2


def test_a_relative_with_its_own_config_does_not_take_a_backend_already_built():
    a = path("sftp://h/a", ssh_config="CONFIG-A")
    built_for_a = a.backend

    b = SFTPURIPath(a, "b", ssh_config="CONFIG-B")

    assert b.backend is not built_for_a
    assert core(b).ssh_config == "CONFIG-B"
    assert (a / "x").backend is built_for_a


def test_a_derivation_given_its_own_config_starts_its_own_family():
    a = path("sftp://h/a", ssh_config="CONFIG-A")
    a.backend

    b = a._from_parsed_parts(a.source, "/b", None, None, ssh_config="CONFIG-B")

    assert b.backend is not a.backend
    assert core(b).ssh_config == "CONFIG-B"
    assert (b / "x").backend is b.backend


def test_a_relative_given_the_same_config_shares_the_backend():
    a = path("sftp://h/a", ssh_config="CONFIG-A")

    same = SFTPURIPath(a, "b", ssh_config="CONFIG-A")
    default_a = path("sftp://h/a")
    default_b = SFTPURIPath(default_a, "b", ssh_config=DEFAULT_SSH_CONFIG)

    assert same.backend is a.backend
    assert default_b.backend is default_a.backend


@pytest.mark.parametrize("supplied_as", ["core", "family"])
def test_a_supplied_backend_still_wins_over_the_config_rule(supplied_as):
    if supplied_as == "core":
        supplied = _Recorded("h", ssh_config="SUPPLIED")
    else:
        supplied = SFTPURIBackend(_Recorded, ssh_config="SUPPLIED")
    a = SFTPURIPath("sftp://h/a", ssh_config="CONFIG-A", backend=supplied)

    assert a.backend is supplied
    assert (a / "x").backend is supplied
    assert SFTPURIPath(a, "b", ssh_config="CONFIG-B").backend is supplied
    assert core(a).ssh_config == "SUPPLIED"


def test_a_family_builds_the_backend_of_a_root_its_parent_and_twenty_children():
    root = path("sftp://h/dir/file.txt")

    core(root.parent)
    core(root)
    children = [root.parent / f"c{number}" for number in range(20)]
    for child in children:
        core(child)

    assert len(_Recorded.built) == 1
    assert all(child.backend is root.backend for child in children)
    assert all(core(child) is core(root) for child in children)
