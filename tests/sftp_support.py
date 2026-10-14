"""Helpers the SFTP tests share: a backend of either kind for a loopback server,
and a path of either flavor (an `SFTPPath`, or an `SFTPURIPath` when `uripath`
is installed) on it.

Nothing here leaves the machine: the servers are `tests/sftp_loopback.py`'s, on
127.0.0.1, and a backend reads no ssh_config, agent or key of the developer's
unless a test gives it one.
"""

from __future__ import annotations

import pytest

KINDS = ["paramiko", "asyncssh"]
FLAVORS = ["path", "uri"]


def make_backend(
    kind,
    server,
    *,
    known_hosts=None,
    ssh_config=None,
    user="x",
    password="x",
    connect_opts=None,
    **kwargs,
):
    """A backend of `kind` ("paramiko" or "asyncssh") for `server` (an
    `SFTPServerAddress`), trusting `known_hosts` (a file, or None for any key).
    `connect_opts` is merged into the library's own connect options; the other
    keywords go to the backend's constructor."""
    if kind == "paramiko":
        paramiko = pytest.importorskip("paramiko")
        from sftppath import SFTPBackend

        return SFTPBackend(
            server.host,
            server.port,
            user=user,
            password=password,
            connect_opts={
                "allow_agent": False,
                "look_for_keys": False,
                **(connect_opts or {}),
            },
            hostkeypolicy=paramiko.AutoAddPolicy() if known_hosts is None else None,
            ssh_config=ssh_config,
            known_hosts=known_hosts,
            **kwargs,
        )
    pytest.importorskip("asyncssh")
    from sftppath import AsyncSSHBackend

    return AsyncSSHBackend(
        server.host,
        server.port,
        user=user,
        password=password,
        connect_opts={
            "known_hosts": None if known_hosts is None else str(known_hosts),
            **(connect_opts or {}),
        },
        ssh_config=ssh_config,
        **kwargs,
    )


def make_path(flavor, server, backend, rel=""):
    """The path `rel` below the root of `server`, over `backend`, of the given
    flavor: `"path"` is an `SFTPPath`, `"uri"` an `SFTPURIPath` (the test is
    skipped where `uripath` is not installed)."""
    if flavor == "path":
        from sftppath import SFTPPath

        return SFTPPath("/" + rel, backend=backend)
    uripath = pytest.importorskip("uripath")
    pytest.importorskip("sftppath.uri")  # registers the scheme

    return uripath.URIPath(server.url(rel), backend=backend)
