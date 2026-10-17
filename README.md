# sftppath

[![Version](https://img.shields.io/pypi/v/sftppath.svg)](https://pypi.org/project/sftppath/)
[![Python versions](https://img.shields.io/pypi/pyversions/sftppath.svg)](https://pypi.org/project/sftppath/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/jose-pr/sftppath/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-latest-blue.svg)](https://jose-pr.github.io/sftppath/)
[![CI](https://img.shields.io/github/actions/workflow/status/jose-pr/sftppath/test.yml)](https://github.com/jose-pr/sftppath/actions/workflows/test.yml)

**`pathlib` for SFTP servers.** `SFTPPath("/srv/data/report.txt", host="files.example.org")`
has the method names, signatures and exception types of `pathlib.Path`, over
paramiko or asyncssh, and needs no URI. It builds on
[`pathapi`](https://github.com/jose-pr/pathapi) alone; with
[`uripath`](https://github.com/jose-pr/uripath) installed it also provides the
`sftp:` scheme. It is documented at <https://jose-pr.github.io/sftppath/>.

## Features

- **A `pathlib` path on a server**: `open`, `read_text`, `iterdir`, `glob`,
  `stat`, `mkdir`, `rename`, `replace`, `chmod`, `symlink_to`, `readlink` and
  the rest, plus `walk`, `copy`, `move`, `rm` and `PathSyncer` from
  `pathapi`. Nothing connects until the first request.
- **Two backends, one behaviour**: `SFTPBackend` (paramiko) and `AsyncSSHBackend`
  (asyncssh) raise the same exceptions for the same server answers; the choice is
  a keyword, a subclass attribute or `SFTPPATH_BACKEND`.
- **Native tree operations**: on asyncssh a recursive `copy()` and `rm()` run as
  concurrent requests on one connection, bounded by `max_concurrency`.
- **From an event loop**: `AsyncSFTPPath` is a `pathapi.AsyncPath` whose stat,
  listing, walk, file reads and writes, `mkdir`, `unlink`, `rename`, recursive
  `copy()` and recursive `rm()` are asyncssh requests on your loop, not on a
  worker thread (it needs `pathapi>=0.1.2`).
- **Your own connection**: `AsyncSSHBackend.over_connection(connection, loop)`
  runs SFTP over an asyncssh connection your program already opened, on the
  event loop that owns it, without logging in again.
- **ssh_config**: `HostName`, `Port`, `User`, `IdentityFile`, `ProxyCommand` and
  `Include` apply to the host you name; host keys are verified.
- **Server-side checksums**: `checksum()` uses the `check-file-handle`
  extension where the server has it, and `PathSyncer` falls back to streaming
  where it does not.
- **No SSH library at import**: `import sftppath` loads neither library nor
  `uripath`. Errors are `OSError` subclasses, `SFTPAuthenticationError` and
  `SFTPHostKeyError`; no library exception crosses the API.
- **The `sftp:` scheme** for `uripath`: `URIPath("sftp://user@host/dir")` is an
  `SFTPURIPath` that delegates to an `SFTPPath`.

## Installation

```bash
pip install "sftppath[paramiko]"
```

Requires `pathapi`, which pip installs with it, and Python 3.9 or newer.

| Install | Adds |
| --- | --- |
| `pip install sftppath` | the path and the backend contract; no SSH library |
| `pip install "sftppath[paramiko]"` | the paramiko backend (`paramiko>=5.0,<6`) |
| `pip install "sftppath[asyncssh]"` | the asyncssh backend and the native tree operations (`asyncssh>=2.20`) |
| `pip install "sftppath[uri]"` | `uripath` and with it the `sftp:` scheme |
| `pip install "sftppath[all]"` | the three above |

## Quick start

**A path on a server**: the endpoint goes in as keywords; the user, password and
port may come from your `~/.ssh/config` instead:

<!-- example: skip: connects to a server -->
```python
from sftppath import SFTPPath

report = SFTPPath("/srv/data/report.txt", host="files.example.org", user="deploy")
print(report.read_text())
for child in report.parent.iterdir():
    print(child.name, child.stat().st_size)
```

**From an event loop**: `AsyncSFTPPath` awaits each request on the running loop;
`async with` closes its connection:

<!-- example: skip: connects to a server -->
```python
import asyncio
from sftppath import AsyncSFTPPath

async def main():
    async with AsyncSFTPPath("/srv/data", host="files.example.org", user="deploy") as root:
        await (root / "out").mkdir(exist_ok=True)
        await (root / "out" / "n.txt").write_text("1")
        async for child in root.iterdir():
            print(child.name, (await child.stat()).st_size)
        await root.copy(root.parent / "backup", recursive=True)

asyncio.run(main())
```

**Share a connection**: a backend is the connections to one server and
account, and a context manager:

<!-- example: skip: connects to a server -->
```python
from sftppath import SFTPBackend, SFTPPath

with SFTPBackend("files.example.org", user="deploy", timeout=10) as backend:
    root = SFTPPath("/srv/data", backend=backend)
    (root / "out").mkdir(exist_ok=True)
    (root / "out" / "n.txt").write_text("1")
    root.copy(SFTPPath("/srv/backup", backend=backend), recursive=True)
```

**Mirror a tree** with `PathSyncer`, onto a local directory:

<!-- example: skip: connects to a server -->
```python
from pathapi import LocalPath
from pathapi.sync import PathSyncer
from sftppath import SFTPPath

remote = SFTPPath("/var/log", host="files.example.org", user="deploy")
PathSyncer(remove_missing=True).sync(remote, LocalPath("mirror"))
```

**The scheme**, with `uripath` installed:

<!-- example: skip: connects to a server -->
```python
from uripath import URIPath

print(URIPath("sftp://deploy@files.example.org/srv/data/report.txt").read_text())
```

## API overview

| Module | Purpose |
| --- | --- |
| `sftppath` | `SFTPPath`, `AsyncSFTPPath`, the backends `SFTPBackend`, `AsyncSSHBackend`, `BaseSFTPBackend` and `CheckFileSFTPBackend`, `SFTPEndpoint`, `DEFAULT_PORT`, `DEFAULT_SSH_CONFIG`, the errors and `__version__` |
| `sftppath.exceptions` | `SFTPPathError`, `SFTPAuthenticationError` and `SFTPHostKeyError` |
| `sftppath.uri` | `SFTPURIPath` and `SFTPURIBackend`, the `sftp:` scheme of `uripath` (needs the `uri` extra) |

The full reference is on the [documentation site](https://jose-pr.github.io/sftppath/);
an agent-oriented API summary ships inside the package as `sftppath/AGENTS.md`.

## Development

```bash
pip install -e ".[dev,all]"
python -m pytest -q -rs
```

The tests start SSH servers of their own on the loopback interface with
throwaway keys; they never reach another host. A test for a library or an extra
that is not installed is skipped, so check the skip list (`-rs`). Contributor
notes (virtual environments, formatting, CI, releases) are in the repository's
[`AGENTS.md`](https://github.com/jose-pr/sftppath/blob/main/AGENTS.md). To
preview the documentation: `pip install -e ".[all,docs]"` then `mkdocs serve`.

Changes are recorded in the
[changelog](https://github.com/jose-pr/sftppath/blob/main/CHANGELOG.md).

### Releasing

This project follows [Semantic Versioning](https://semver.org/) and keeps a
[`CHANGELOG.md`](https://github.com/jose-pr/sftppath/blob/main/CHANGELOG.md). Pushing a tag matching `v*` triggers the release
workflow: test gate → build (checking the tag names the version built) → a strict
docs build as a gate → GitHub release → publish. The release workflow never deploys
the docs site itself: for a final release its last job dispatches the docs workflow
at the tag, which owns every Pages deploy.

## License

MIT — see [LICENSE](https://github.com/jose-pr/sftppath/blob/main/LICENSE).
