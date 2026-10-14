# SFTP paths

`SFTPPath` is a `pathapi.Path` for a directory tree on an SFTP server. It
supports what `pathlib.Path` does on a file system (`open`, `read_text`,
`write_bytes`, `iterdir`, `glob`, `stat`, `mkdir`, `unlink`, `rmdir`, `rename`,
`replace`, `chmod`, `symlink_to`, `readlink`, ...) and what `pathapi` adds
(`walk`, `copy`, `move`, `rm`, `PathSyncer`).

## Naming the server

The endpoint is part of the path: pass it as keywords, or share a backend.

```python
from sftppath import SFTPPath

SFTPPath("/srv/x", host="h")                                  # the account of the ssh_config
SFTPPath("/srv/x", host="h", port=2222, user="u", password="p")
SFTPPath("/srv/x", host="h", ssh_config=None, timeout=10)     # no ~/.ssh/config
```

`host` may be a name from your `~/.ssh/config`: its `HostName`, `Port`, `User`,
`IdentityFile` and `ProxyCommand` apply where the call names none. Keywords of
the backend class (`known_hosts=` and `hostkeypolicy=` for paramiko,
`max_concurrency=` and `sftp_version=` for asyncssh) go through the same call.
`backend=` replaces all of them, and giving both is a `TypeError`.

Names are read from the server root: `SFTPPath("data/x", host=h)` is
`/data/x`, not a file in the home directory of the account. A path joined from
another keeps its backend, and so do `parent`, `with_name`, `iterdir` and
`relative_to`.

## Backends

A backend is the connections to one server and account, and the object paths
share to share them:

| Backend | Library | Notes |
| --- | --- | --- |
| `SFTPBackend` | paramiko | one connection per thread; `ProxyJump` is not supported |
| `AsyncSSHBackend` | asyncssh | one connection per backend on a shared loop thread; recursive `copy()` and `rm()` run as concurrent requests (`max_concurrency`); `hardlink_to()` and `chmod(follow_symlinks=False)` |

Without `backend=`, the class is chosen by `SFTPPATH_BACKEND` (`auto`,
`asyncssh` or `paramiko`; `auto` prefers asyncssh). A backend is a context
manager; `close()` ends every connection it holds and a later use connects
again.

Host keys are verified. paramiko reads `~/.ssh/known_hosts` and the
`UserKnownHostsFile` of the ssh_config and rejects an unknown key
(`hostkeypolicy=`, `known_hosts=`); asyncssh does the same (`connect_opts=`).
Both raise `SFTPHostKeyError` and send no credential. Turning verification off
(`known_hosts=None`) sends the password to whoever answers.

## Errors

A failed request is the `OSError` pathlib raises for it, with `errno` and
`filename`; a lost connection is `ConnectionResetError`, a timeout
`TimeoutError`, a refused login `SFTPAuthenticationError` (a `PermissionError`)
and an unknown or changed host key `SFTPHostKeyError` (a `ConnectionError`).
Nothing of paramiko or asyncssh crosses the API. The library's own message is
logged at DEBUG on the logger `sftppath`.

## Checksums and links

`checksum()` asks the server for the digest through the `check-file-handle`
extension (OpenSSH does not have it); `PathSyncer` uses it and falls back to
streaming when it raises `NotImplementedError`. `symlink_to()` and `readlink()`
work on both backends.

## Cost

A directory is listed with one request that carries every entry's attributes.
`walk()`, `glob()` and `rm(recursive=True)` use them; `iterdir()` yields
children without attributes, so `is_dir()` on each costs one request. Prefer
`walk()` or `glob()` to a listing followed by a test of each child.
