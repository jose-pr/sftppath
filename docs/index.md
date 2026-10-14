# sftppath

**`pathlib` for SFTP servers.** `SFTPPath("/srv/data/report.txt", host="files.example.org",
user="deploy")` has the method names, signatures and exception types of
`pathlib.Path`, over [paramiko](https://www.paramiko.org/) or
[asyncssh](https://asyncssh.readthedocs.io/). It needs no URI and builds on
[`pathlib-next`](https://jose-pr.github.io/pathlib-next/) alone, which holds the
`Path` base class, `glob`, `walk` and `PathSyncer`. With
[`uripath`](https://jose-pr.github.io/uripath/) installed the same package also
provides the `sftp:` scheme: `URIPath("sftp://deploy@files.example.org/srv/data")`.

Deliberate behavioral differences from `pathlib` are listed, with their reasons,
in [Divergences from pathlib](divergences.md).

## Installation

```bash
pip install "sftppath[paramiko]"      # or "sftppath[asyncssh]", or both
pip install "sftppath[uri]"           # adds uripath and with it the sftp: scheme
```

Neither SSH library is required to import the package; the first path built from
a host name raises an `ImportError` that names both extras when neither is
installed. With both installed, asyncssh is used, and it also runs recursive
`copy()` and `rm()` as concurrent requests.

| Install | Adds |
| --- | --- |
| `pip install sftppath` | the path and the backends' contract (no SSH library) |
| `pip install "sftppath[paramiko]"` | the paramiko backend, `SFTPBackend` |
| `pip install "sftppath[asyncssh]"` | the asyncssh backend, `AsyncSSHBackend` |
| `pip install "sftppath[uri]"` | `uripath` and the `sftp:` scheme |
| `pip install "sftppath[all]"` | all three |

## Tour

**A path on a server**: the host, port, user and password go in as keywords; a
path with no `backend=` builds its own and nothing connects until the first
request:

<!-- example: skip: connects to a server -->
```python
from sftppath import SFTPPath

report = SFTPPath("/srv/data/report.txt", host="files.example.org", user="deploy")
print(report.read_text())
for child in report.parent.iterdir():
    print(child.name, child.stat().st_size)
```

**One connection for many paths**: a backend is the connections to one server
and account; paths that share it share its connections:

<!-- example: skip: connects to a server -->
```python
from sftppath import SFTPBackend, SFTPPath

with SFTPBackend("files.example.org", user="deploy", timeout=10) as backend:
    root = SFTPPath("/srv/data", backend=backend)
    (root / "out").mkdir(exist_ok=True)
    (root / "out" / "n.txt").write_text("1")
    root.copy(SFTPPath("/srv/backup", backend=backend), recursive=True)
```

**The scheme**: with `uripath` installed, the URI of the server works too, and
the same classes do the work:

<!-- example: skip: connects to a server -->
```python
from uripath import URIPath

print(URIPath("sftp://deploy@files.example.org/srv/data/report.txt").read_text())
```

See [SFTP paths](guides/paths.md) and [The sftp scheme](guides/uri.md).
