"""The ssh_config lookup of the paramiko backend: paramiko's parser with `Include`
support and the unexpanded `ProxyCommand` tokens, cached per file state."""

from __future__ import annotations

import pathlib as _pathlib

import paramiko as _paramiko
from pathapi import providers as _providers

from ._sshconfig import _expand_includes, _normalize_config_paths


class _SSHConfig(_paramiko.SSHConfig):
    """paramiko's parser, except that a ProxyCommand comes back with its
    ``%`` tokens unexpanded: ``SFTPBackend.opts()`` expands them with the host,
    port and user chosen for the connection, which a lookup by host name
    alone cannot know."""

    TOKENS_BY_CONFIG_KEY = {
        **_paramiko.SSHConfig.TOKENS_BY_CONFIG_KEY,
        "proxycommand": ["~"],
    }


def _file_stamps(config_paths: "tuple[str, ...]") -> tuple:
    """`(path, modification time, size)` of each config file that exists, so
    a parsed config is not reused after the file changed."""
    stamps = []
    for path in config_paths:
        try:
            info = _pathlib.Path(path).expanduser().stat()
        except OSError:
            continue
        stamps.append((path, info.st_mtime_ns, info.st_size))
    return tuple(stamps)


@_providers.LRU
def _load_ssh_config(
    config_paths: "tuple[str, ...]", stamps: tuple = ()
) -> "_SSHConfig | None":
    config = _SSHConfig()
    loaded = False
    for path in config_paths:
        ssh_path = _pathlib.Path(path).expanduser()
        if not ssh_path.is_file():
            continue
        # paramiko's parser has no Include support: expand it first.
        config.parse(_expand_includes(ssh_path))
        loaded = True
    return config if loaded else None


def _lookup_ssh_config(
    host: str,
    ssh_config: "object",
) -> "dict[str, object]":
    config_paths = _normalize_config_paths(ssh_config)
    if not config_paths:
        return {}
    config = _load_ssh_config(config_paths, _file_stamps(config_paths))
    if config is None:
        return {}
    return config.lookup(host)
