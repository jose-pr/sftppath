"""Connecting and opening the SFTP session."""

from __future__ import annotations

import functools as _functools
import typing as _ty

import asyncssh as _asyncssh

from .._sshconfig import _check_host, _check_proxy_user
from . import _loop as _bridge
from ._cache import _ConnectionEntry
from ._client import _SyncSFTPClient


def _check_user_for_proxy(host: str, user: str, kwargs: "dict[str, _ty.Any]") -> None:
    """Refuse `user` when asyncssh would put it into a ProxyCommand's argv.

    asyncssh expands the ssh_config `ProxyCommand` tokens (`%r` is the user)
    and splits the result into arguments afterwards, so a user holding white
    space or a leading `-` adds arguments. A server accepts such a user, so
    only the one the configuration for this host would pass on is refused:
    the command is resolved with this user and with a safe one, and a
    difference means the user is in it.
    """
    try:
        _check_proxy_user(user)
        return
    except ValueError as refusal:
        refused = refusal
    options = {"host": host, "known_hosts": None, "client_keys": None}
    for key in ("config", "port"):
        if key in kwargs:
            options[key] = kwargs[key]
    resolve = _functools.partial(_asyncssh.SSHClientConnectionOptions, **options)
    if resolve(username=user).proxy_command != resolve(username="user").proxy_command:
        raise refused from None


async def _aconnect(
    host: str,
    port: "int | None" = None,
    user: "str | None" = None,
    password: "str | None" = None,
    connect_opts: "_ty.Mapping[str, _ty.Any] | None" = None,
    sftp_version: int = 4,
    timeout: "float | None" = _bridge._UNSET_TIMEOUT,
) -> _ConnectionEntry:
    _check_host(host)
    # No `known_hosts` default: asyncssh then verifies the server key against
    # ~/.ssh/known_hosts and the ssh_config's UserKnownHostsFile.
    kwargs: "dict[str, _ty.Any]" = {}
    if connect_opts:
        kwargs.update(connect_opts)
    if port:
        # Only a port the caller names: an explicit one outranks the ssh_config
        # `Port`, which asyncssh applies when none is passed.
        kwargs["port"] = port
    if user:
        _check_user_for_proxy(host, user, kwargs)
        kwargs["username"] = user
    if password:
        kwargs["password"] = password
    conn = await _asyncssh.connect(host, **kwargs)
    try:
        # Request the configured SFTP version (3 or 4) and let the server negotiate down
        # (OpenSSH stays at v3). Names that are not UTF-8 come back as lone surrogates
        # and go back out as the same bytes.
        aclient = await conn.start_sftp_client(
            sftp_version=sftp_version, path_errors="surrogateescape"
        )
    except BaseException:
        conn.close()
        raise
    return _ConnectionEntry(conn, _SyncSFTPClient(aclient, timeout))
