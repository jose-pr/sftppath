# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

The first release of `sftppath`: the SFTP support of `pathlib-next` 0.9.14
(`pathlib_next.uri.schemes.sftp`) as a distribution of its own, with a path that
needs no URI. It needs Python 3.9 or newer and `pathlib-next>=0.10.0,<0.11`.
Neither SSH library is required to import it: the extras `paramiko`
(`paramiko>=5.0,<6`) and `asyncssh` (`asyncssh>=2.20.0`, below 3) add one, and
`uri` (`uripath>=0.1.0,<0.2`) adds the `sftp:` scheme. With neither SSH library a
path can still be built, joined and compared, and its first request raises an
`ImportError` that names both extras; naming `SFTPBackend` or `AsyncSSHBackend`
without its library raises one that names its extra. Under *Changed*, the
migration from `pathlib_next.uri.schemes.sftp` comes in this order: the table of
names, the table of extras, then every change of behaviour with what to do about
it.

### Added

- `SFTPPath`, a `pathlib_next.Path` for a directory tree on an SFTP server, built
  from keywords: `SFTPPath("/srv/x", host="h", port=2222, user="u", password="p")`.
  Nothing connects until the first request. `backend=` shares one connection
  between paths built separately; a path joined from another keeps its backend.
- `SFTPBackend` (paramiko), `AsyncSSHBackend` (asyncssh), `BaseSFTPBackend` and
  `CheckFileSFTPBackend`, each the connections to one server and account. Every
  backend is a context manager, `close()` returns when the connections are
  closed, and may be called twice. `SFTPEndpoint`, `DEFAULT_PORT` and
  `DEFAULT_SSH_CONFIG` are exported too.
- `sftppath.exceptions`: `SFTPPathError`, a `pathlib_next.PathError`, with
  `SFTPAuthenticationError` (also `PermissionError`) and `SFTPHostKeyError`
  (also `ConnectionError`).
- `sftppath.uri`: `SFTPURIPath`, the `sftp:` scheme of `uripath`, found by
  `uripath` through the entry point `sftp` of the group `uripath.schemes`, and
  `SFTPURIBackend`, which builds the backend of each server a URI names. Importing
  `sftppath.uri` without `uripath` raises an `ImportError` that names
  `pip install "sftppath[uri]"`; `import sftppath` never imports `uripath`.
- `SFTPPath.is_local()`: true for a loopback address, `localhost` and a backend
  with no host, so `PathSyncer(quick_check=True)` applies to a real server.
- `sftppath.__version__`, read from the installed metadata.

### Changed

- The names. `pathlib_next.uri.schemes.sftp` is gone with no alias.

  | Was | Now |
  | --- | --- |
  | `SftpPath` (a `UriPath`) | `sftppath.SFTPPath` for a path built from keywords; `sftppath.uri.SFTPURIPath` for `URIPath("sftp://...")` |
  | `SftpBackend` | `sftppath.SFTPBackend` |
  | `AsyncsshSftpBackend` | `sftppath.AsyncSSHBackend` |
  | `BaseSftpBackend` | `sftppath.BaseSFTPBackend` |
  | `CheckFileSftpBackend` | `sftppath.CheckFileSFTPBackend` |
  | `SftpAuthenticationError`, `SftpHostKeyError` | `sftppath.SFTPAuthenticationError`, `sftppath.SFTPHostKeyError` (in `sftppath.exceptions`) |
  | `_DEFAULT_SSH_CONFIG` | `sftppath.DEFAULT_SSH_CONFIG` |
  | `PATHLIB_NEXT_SFTP_BACKEND` | `SFTPPATH_BACKEND`; the old variable is not read |
  | the logger `pathlib_next.sftp` | the logger `sftppath` |
  | the entry-point group `pathlib_next.schemes`, entry `sftp` | the group `uripath.schemes`, entry `sftp = "sftppath.uri:SFTPURIPath"` |

- What to install where `pathlib-next` had an extra. `pathlib-next` 0.10 has no
  `sftp` or `sftp-async` extra.

  | Was | Install now |
  | --- | --- |
  | `pathlib-next[sftp]` | `sftppath[paramiko]` |
  | `pathlib-next[sftp-async]` | `sftppath[asyncssh]` |
  | either, for `UriPath("sftp://...")` | add `sftppath[uri]` (or install `uripath` beside it) |

- A backend is the connections to one server and account. Was: one backend
  served every host a path named, and `client(source)`, `opts(source)` and
  `transport(source)` took the `Source` of the URI; a backend was built with its
  options only (`SftpBackend(connect_opts, hostkeypolicy, ...)`). Now the host,
  port, user and password are arguments of the backend
  (`SFTPBackend("h", 2222, user="u", password="p", connect_opts=...)`,
  `AsyncSSHBackend("h", ...)`), and `client()`, `opts()` and `transport()` take
  nothing. A `BaseSFTPBackend` subclass overrides `client(self)`. For URI paths,
  give `backend=` an `SFTPURIBackend(SFTPBackend, connect_opts=...)` (the
  options of the backend class; the user, password and port come from the URI)
  or a `BaseSFTPBackend` for the one server. `BaseSftpBackend.default()` and the
  `default(ssh_config=)` classmethods are removed.
- A relative or empty path is read from the server root. Was: `sftp://host` (no
  path) listed the home directory of the account. Now it is `/`, and
  `SFTPPath("x", host=h)` is `/x`. Name the directory you mean
  (`/home/user`).
- `SFTPPath.readlink()` returns a path on the same backend, so dot segments of
  the target collapse as they do for `pathlib`. The `SFTPURIPath` keeps the text
  verbatim in `.path`.
- `SFTPPath.iterdir()` yields children without the attributes the listing
  carried, so `is_dir()` on each costs a request; `walk()`, `glob()` and
  `rm(recursive=True)` still use the listing. The children an `SFTPURIPath`
  yields carry them, as before.
- An error that has a file name and no second one prints only the first; it
  printed `-> None` after it.
