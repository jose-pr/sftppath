# The `sftp:` scheme

With [`uripath`](https://jose-pr.github.io/uripath/) installed (`pip install
"sftppath[uri]"`), a URI whose scheme is `sftp` is an `SFTPURIPath`:

<!-- example: skip: connects to a server -->
```python
from uripath import URIPath

path = URIPath("sftp://deploy@files.example.org:2222/srv/data/report.txt")
print(type(path).__name__)   # SFTPURIPath
print(path.read_text())
```

`uripath` finds the class through the entry point `sftp` of the group
`uripath.schemes` that this distribution declares, so nothing has to be
imported. Without the distribution, a path of the scheme still builds, joins and
prints, and its first request raises an `ImportError` that names
`pip install "sftppath[paramiko]"`. Importing `sftppath.uri` without `uripath`
raises an `ImportError` that names `pip install "sftppath[uri]"`; `import
sftppath` never imports `uripath`.

## One class of path behind a URI

An `SFTPURIPath` holds an [`SFTPPath`](paths.md) for the server and path the URI
names and hands every request to it, so the errors, the connection caches and
the native recursive `copy()` and `rm()` are the same. The URI layer keeps
what is about URIs: parsing and joining, `str()` without the password, pickling
and the way paths of one endpoint share a backend. An error that names the
remote path names the URI.

## Backends and `ssh_config`

Without `backend=`, a path builds an `SFTPURIBackend`; it makes the backend of the
class `SFTPPATH_BACKEND` selects for each server and account the paths name,
with the user, password and port of the URI. To set options, build one and pass
it:

<!-- example: skip: connects to a server -->
```python
from sftppath import SFTPBackend
from sftppath.uri import SFTPURIBackend
from uripath import URIPath

backend = SFTPURIBackend(SFTPBackend, timeout=10, known_hosts="/etc/ssh/ssh_known_hosts")
with backend:
    root = URIPath("sftp://deploy@files.example.org/srv/data", backend=backend)
    print([child.name for child in root.iterdir()])
```

A `BaseSFTPBackend` passed as `backend=` is used as it is for every path,
whatever host the URI names. `ssh_config=` (a path, an iterable of paths, `None`
or the default `~/.ssh/config`) is a keyword of the constructor; it follows the
paths derived from the path on the same endpoint and not those on another host.

## Moving between hosts

`rename()`, `replace()` and `hardlink_to()` from one server to another are
`NotImplementedError`, so `move()` copies and then deletes. A directory copied
to another server, or to a local path, takes the generic copy; a directory
copied within one server and one set of connections takes the native walk of the
asyncssh backend.
