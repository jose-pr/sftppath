# `sftppath` — public API header

Header-file-style reference for the `sftppath` package: every public export with
its signature, arguments, contract and gotchas, so the package can be used
without reading its source. It ships inside the package and is
self-contained. Development documentation lives with the source at
<https://github.com/jose-pr/sftppath>.

Names are imported from `sftppath` (the path and the backends), from
`sftppath.exceptions` (the errors, also exported from the root) and from
`sftppath.uri` (the `sftp:` scheme of `uripath`); a module whose name starts
with an underscore is private. `import sftppath` imports neither SSH library
nor `uripath`: `SFTPBackend` and `AsyncSSHBackend` are loaded the first time
they are named, and naming one whose library is not installed raises an
`ImportError` naming its extra.

`pip install "sftppath[paramiko]"` or `"sftppath[asyncssh]"` installs an SSH
library; with neither, a path can still be built, named, joined and compared
(its endpoint keywords are not checked), and its first request raises an
`ImportError` naming both extras. `[uri]` adds `uripath` and with it the `sftp:`
scheme, `[all]` is the three together. The one required dependency is
`pathapi>=0.1.0,<0.2`. The root's `__version__` is the version of the
installed distribution.

## SFTP paths (`sftppath`)

### `SFTPPath`

```python
class SFTPPath(Path):
    backend
    def __init__(
        self, *segments, backend=None, host=None, port=None, user=None,
        password=None, ssh_config=..., timeout=..., connect_opts=None, **options,
    ) -> None: ...
    def as_uri(self) -> str: ...
    def is_local(self) -> bool: ...
    def readlink(self) -> SFTPPath: ...
    def replace(self, target) -> SFTPPath: ...
    def rename(self, target) -> SFTPPath: ...
    def hardlink_to(self, target) -> None: ...
    def chmod(self, mode, *, follow_symlinks=True) -> None: ...
    def checksum(self, algorithm="md5") -> str: ...
    def supported_checksums(self) -> FrozenSet[str]: ...
```

A `pathapi.Path` for a directory tree on an SFTP server, over paramiko or
asyncssh. It needs no URI. Nothing connects when a path is built; the first
request does.

- **Construction.** `SFTPPath("/srv/x", host="h", user="u", password="p")` builds
  the path's own backend from the endpoint keywords, of the class chosen by
  "Backend selection" below, and passes it `ssh_config=`, `timeout=`,
  `connect_opts=` (when given) and `**options`, the keywords of that class
  (`known_hosts=`, `hostkeypolicy=` for paramiko; `max_concurrency=`,
  `sftp_version=` for asyncssh): a keyword the class does not take is a
  `TypeError`. `backend=` instead shares one connection between paths built
  separately, or plugs in a `BaseSFTPBackend` of your own. Giving `backend=`
  together with any other keyword is a `TypeError`, and so is giving neither
  (unless a segment is an `SFTPPath`, whose backend is kept) and giving the
  other keywords without `host=`. Segments are `str`, `SFTPPath` or a pure
  path; an absolute segment restarts the join, empty and `.` segments
  collapse. Every path derived from one (`/`, `joinpath`, `parent`,
  `with_name`, `iterdir`, `relative_to`) keeps its backend.
- **`port`** is `None` unless the caller names one: the `Port` of the host in
  the ssh_config, else 22, is used then. A port given outranks the config.
  `ssh_config` is `~/.ssh/config` unless a path, an iterable of paths or
  `None` (no config) is given; `DEFAULT_SSH_CONFIG` names the default. A host
  that is not a plain host name or address -- an IP address (an IPv6 one may
  carry a `%zone`), or ASCII letters, digits, `.`, `_`, `-` (not first) and the
  letters and digits of other scripts -- raises `ValueError` naming it where a
  connection or an ssh_config lookup would start, on both backends; building,
  joining and comparing such a path still work.
- **Backend selection**, highest first: `backend=` → the class attribute
  `_default_backend_cls` of a subclass → the environment variable
  `SFTPPATH_BACKEND` (`auto`|`asyncssh`|`paramiko`; the first request of a path
  whose named backend is not installed raises `ImportError`) → auto (asyncssh if importable, else
  paramiko). The choice is made once per process.
- **Names.** The tree has no working directory: `srv/x` and `/srv/x` name one
  file, read from the server root, and `/` is sent for the root (never an empty
  argument, which a server reads as its start directory). `..` is sent as
  written. A name the server sends that is not UTF-8 is the string of its bytes
  as lone surrogates (pathlib's spelling on POSIX), on both backends: it lists
  beside its siblings, and `stat()`, `open()`, `unlink()`, `rm()` and `copy()`
  send the same bytes back.
- **Text forms.** `str()` is the path alone. `repr()` names the endpoint
  (`SFTPPath('/srv/x', host='h', port=22, user='u')`, with `port=` and `user=`
  only when set) and never a password. `as_uri()` is
  `sftp://[user@]host[:port]/path` without a password; `NotImplementedError` on a
  backend that has no `host`.
- **`is_local()`** is true when the host is a loopback or unspecified address or
  `localhost`, or the backend has no host; a host name is not resolved.
  `PathSyncer` skips its `quick_check` when both sides are local.
- **Identity.** `==` compares the names only (as `MemPath` does); two paths are
  one tree (`copy()`, `move()` and `PathSyncer` ask) when they share a backend
  object or their backends have equal `endpoint`s. `_node_key()` follows the
  same rule and applies `..` to the text. A pickle holds the text and the
  backend, which carries no connection and a password or `connect_opts` only
  if it was given them; `copy.copy` and `copy.deepcopy` share the backend.
- **Listing and stat.** A directory is listed with one request that carries every
  entry's attributes (a link is not followed): `walk()`, `glob()` and
  `rm(recursive=True)` test each child from that listing, and an entry that is
  not a link is not stat-ed again. `iterdir()` yields children without
  attributes, so `is_dir()` on each costs one request; the children an
  `SFTPURIPath` yields carry them. A listed name that is not one path
  component (`..`, anything with `/`) is skipped. `stat()` returns the
  client's `os.stat_result`-shaped attributes.
- **Errors** are the same on both backends and none is chained to the SSH
  library's exception. A server status is the pathlib exception with `errno`
  and `filename` (the remote path; `filename2` for the second path of
  `rename()` and `symlink_to()`); an "operation unsupported" status is
  `NotImplementedError`. An error `SFTPPath` builds itself (`mkdir()` of an
  existing path, `rmdir()` or `iterdir()` of the wrong kind of entry) carries
  the path as `filename`. Reading a directory is `IsADirectoryError`
  (`EISDIR`) on every server: OpenSSH opens a directory for reading, so there
  `open()` succeeds and the first read raises it, with the remote path as
  `filename` for both path classes. A read that fails with no `errno`, or an
  unsized read that returns nothing, costs one more request (an fstat) to
  tell; a read that returns data costs none. A connection that goes away, mid-request or
  mid-transfer, is `ConnectionResetError`; a refused login is
  `SFTPAuthenticationError` (a `PermissionError`, `EACCES`); a host key that
  is unknown, changed or refused is `SFTPHostKeyError` (a `ConnectionError`,
  `ECONNABORTED`) and no credential was sent; any other failure of the
  handshake or the session is `ConnectionAbortedError`; a refused TCP
  connection is `ConnectionRefusedError`; a timeout is `TimeoutError` (a
  banner that never arrives is reported as `ConnectionAbortedError` by
  paramiko). The SSH library's own message for a `ConnectionAbortedError` (and
  for the rare request failure reported as `OSError(EIO)`) is logged at DEBUG on
  the logger `sftppath`, as `SFTP connection failed: <ExceptionClass>:
  <message>`; the exception carries only the class name. A DEBUG record can
  hold what the SSH library put in its message, so enable that logger for
  troubleshooting and keep it out of shared logs.
- **Writes** go through as the stream is used and `open("w")` truncates the
  file at once: a `with` block that raised leaves what it had written, as a
  local file does (the stream has no `discard()`). `write()` returns the number
  of bytes written and `truncate()` works on both backends. A file opened for
  writing is flushed and closed when the interpreter exits if the program left
  it open (a text wrapper's pending text included), waiting at most five
  seconds.
- **Links and attributes.** `readlink()` returns the target as a path on the
  same backend (dot segments collapse, as `pathlib` does) and `symlink_to()`
  stores its target verbatim, on both backends; `hardlink_to(target)` and
  `chmod(follow_symlinks=False)` on asyncssh only (paramiko →
  `NotImplementedError`); `chown()` takes numeric ids only. A partial `chown()`
  (`chown(None, gid)`) needs the current id of the field it leaves alone: a
  server that names owners instead of numbering them (SFTP version 4) makes it
  `NotImplementedError` naming the field, and nothing is sent (`st_uid` and
  `st_gid` are 0 for an owner name that is not a number). `_utime()` sets the
  times with one attributes request on both backends, in whole seconds; SFTPv3
  carries the access and modification time as a pair, so the one left alone is
  read first (one more request).
- **`rename(target)`** is onto a path of the same server (a `str` target is a
  sibling name; a path of another server is `NotImplementedError`, so `move()`
  copies). An existing target is replaced via `posix-rename@openssh.com` where
  supported, else `FileExistsError`. `replace(target)` onto a path of the same
  server is that one request when the server has the extension: atomic, the
  target is not removed first, and a refusal that names its cause
  (`PermissionError`, `FileNotFoundError`, ...) leaves both entries as they
  were. A server without the extension, one that refuses it with a bare
  failure, and a directory at `target` (judged by `Path.replace()`, which
  checks before it removes: `OSError(ENOTEMPTY)`, `IsADirectoryError`) get the
  generic `replace()`: the file at `target` is removed, then this entry is
  renamed. `move(overwrite=True)` onto an existing file of the same server
  uses it, so it is one `posix-rename` and no removal.
- **`checksum(algorithm)` and `supported_checksums()`** (`NativeChecksum`): both
  backends send the `check-file-handle` extension (OpenSSH lacks it); an
  "operation unsupported" answer is remembered per connection and algorithm
  (one request per algorithm, not per file) and a failure for one file refuses
  nothing; an algorithm other than `md5`, `sha1`, `sha224`, `sha256`,
  `sha384`, `sha512` or `crc32`, or a digest of the wrong size, is
  `NotImplementedError` (no request for the first); every failure is
  `NotImplementedError`. `supported_checksums()` is empty until a digest has
  been produced.
- **Recursive `copy()` and `rm()`.** When the backend has its own walks
  (`supports_tree`: asyncssh), a directory copied to a path of the same server
  and tree, and `rm(recursive=True)` with the default `follow_symlinks=False`,
  run them as concurrent native requests bounded by `max_concurrency`;
  otherwise (and for every backend that does not set it) the generic walks of
  `pathapi` run, and no SSH library is imported by `copy()` or `rm()`.
  `progress` is not called during the native fan-out, and the native `rm()`
  looks only at the root before it lists. `rm()` and `copy()` take
  `max_workers=None` (`pathapi`'s keyword, passed on to the generic walks only
  when given; the native walks ignore it and a path never declares
  `_threadsafe`).

### `SFTPBackend`, `AsyncSSHBackend`, `BaseSFTPBackend`

```python
DEFAULT_PORT = 22

class SFTPEndpoint(NamedTuple):
    host: str
    port: int | None
    user: str | None

class BaseSFTPBackend:
    endpoint
    supports_lchmod = False
    supports_hardlink = False
    supports_tree = False
    def client(self): ...
    def close(self) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc_info) -> None: ...
    def tree_copy(
        self, path, target, *, overwrite, follow_symlinks, preserve_metadata,
        ignore_error, on_error=None,
    ) -> None: ...
    def tree_rm(self, path, *, missing_ok, on_error) -> None: ...
    def checksum(self, path, algorithm) -> str: ...
    def supported_checksums(self, path) -> FrozenSet[str]: ...

class CheckFileSFTPBackend(BaseSFTPBackend): ...

class SFTPBackend(CheckFileSFTPBackend):
    host
    port
    user
    password
    connect_opts
    hostkeypolicy
    ssh_config
    known_hosts
    timeout
    DEFAULT_TIMEOUT = 30.0
    def __init__(
        self, host, port=None, *, user=None, password=None, connect_opts=None,
        hostkeypolicy=None, ssh_config=..., known_hosts=..., timeout=30.0,
    ) -> None: ...
    def opts(self): ...
    def transport(self) -> Transport: ...

class AsyncSSHBackend(CheckFileSFTPBackend):
    host
    port
    user
    password
    connect_opts
    max_concurrency
    sftp_version
    timeout
    DEFAULT_MAX_CONCURRENCY = 16
    def __init__(
        self, host, port=None, *, user=None, password=None, connect_opts=None,
        max_concurrency=None, sftp_version=4, ssh_config=..., timeout=60.0,
    ) -> None: ...
```

A backend is the connections to one SFTP server and account; its `endpoint`
(an `SFTPEndpoint`, with the host name in lower case and no password) is what
two backends must share for their paths to name the same files. Nothing
connects when one is built (`ValueError` for an empty `host`). `close()`
closes every connection the backend holds, whichever thread opened it, and
returns when they are closed; it is harmless twice and before any use, and a
later use connects afresh. Leaving a `with` block calls it. `host` may be a
name from the user's ssh_config.

- `BaseSFTPBackend.client()` is the override point: a paramiko-shaped
  `SFTPClient` (`stat`, `lstat`, `listdir_attr`, `open`, `mkdir`, `chmod`,
  `chown`, `utime`, `remove`, `rmdir`, `rename`, `posix_rename`, `symlink`,
  `readlink`, `link`), cached or newly opened, with no argument.
  `supports_lchmod`, `supports_hardlink`, `checksum()` and
  `supported_checksums()` are the other switches. `supports_tree` (False;
  asyncssh True) says the backend has its own concurrent recursive `copy()` and
  `rm()` (`tree_copy(path, target, *, overwrite, follow_symlinks,
  preserve_metadata, ignore_error, on_error=None)`, `tree_rm(path, *,
  missing_ok, on_error)`); `on_error(error, path)` decides whether the failure
  of one entry is skipped. `CheckFileSFTPBackend` adds the `check-file-handle`
  `checksum()` and `supported_checksums()` to a backend that supplies the
  extended request.
- **`SFTPBackend`** (paramiko; `pip install "sftppath[paramiko]"`) connects with
  `paramiko.SSHClient` using `connect_opts` merged with `opts()`, the keyword
  arguments `SSHClient.connect()` is called with for the host (a `ProxyCommand`
  starts its process there); `transport()` connects and returns the
  `paramiko.Transport`. Host keys are verified: `known_hosts` default is
  `~/.ssh/known_hosts` plus the ssh_config `UserKnownHostsFile` (`None` loads
  none; a path or list loads exactly those), `hostkeypolicy` default
  `paramiko.RejectPolicy()`; a changed key always fails with `SFTPHostKeyError`.
  Opt-out: `SFTPBackend(host, hostkeypolicy=paramiko.AutoAddPolicy(),
  known_hosts=None)` accepts any server key, and a network
  man-in-the-middle then receives the password. `timeout` fills paramiko's
  connect/banner/auth/channel timeouts (`connect_opts` values win; `None`
  leaves them unset); requests on an open connection are unbounded.
  ssh_config: `HostName`, `Port`, `User`, `IdentityFile`, `ProxyCommand`,
  `Include`; `ProxyJump` → `NotImplementedError` unless `connect_opts["sock"]` is
  given. A `ProxyCommand` expands `%h`, `%n`, `%p`, `%r` and `%%` with the host,
  port and user this connection uses (a user with white space, a quote, a shell
  metacharacter or a leading `-` is refused when `%r` is used, on asyncssh too:
  a `ValueError` before asyncssh is given the user, when the config's
  `ProxyCommand` would pass it on); an `Include` inside a `Host` or `Match`
  block applies the included file's blocks only where the enclosing one does.
  Connections are cached per (backend, thread) and replaced when dropped.
- **`AsyncSSHBackend`** (`pip install "sftppath[asyncssh]"`) connects with
  asyncssh, which verifies host keys against `known_hosts` and the ssh_config;
  the opt-out is `connect_opts={"known_hosts": None}`. `ssh_config` is passed to
  asyncssh as its `config`. The port is the one given, else the ssh_config
  `Port` of the host. `timeout` bounds single requests (a timed-out request is
  cancelled and raises `TimeoutError`); recursive `copy()`/`rm()`, streamed
  reads/writes and a native `checksum()` are unbounded (use asyncssh's
  `connect_timeout`/`keepalive_interval`). One connection per backend, served
  by one shared background event loop thread; not fork-safe (rebuilt after
  `fork()`). A sync `Path` call made on that loop thread (inside a callback
  running there) raises `RuntimeError`. `max_concurrency` (`None` → the class
  attribute `DEFAULT_MAX_CONCURRENCY`, 16) bounds requests in flight and files
  open during recursive `copy()` (target on the same server and the same tree:
  two backends with different endpoints copy through the generic walk) and
  `rm()`. Such a call that raises, times out or is interrupted
  (`KeyboardInterrupt`) has stopped when the exception reaches the caller: no
  further request is sent, and a destination file whose copy did not complete
  is removed. Both walks leave behind what the generic `copy()`/`rm()` leave: a
  source is opened before the destination it replaces is touched, `copy()`
  applies permission bits as `Path.copy()` does, `rm()`'s `missing_ok` covers
  the path it was called on only, and `ignore_error` is offered each error
  once.

## The `sftp:` scheme (`sftppath.uri`, `uri` extra)

```python
class SFTPURIPath(DelegatingURIPath):
    def __init__(self, *args, ssh_config=..., **kwargs): ...
    def readlink(self) -> SFTPURIPath: ...
    def replace(self, target) -> SFTPURIPath: ...
    def hardlink_to(self, target): ...
    def checksum(self, algorithm="md5") -> str: ...
    def supported_checksums(self) -> FrozenSet[str]: ...

class SFTPURIBackend:
    backend_class
    options
    def __init__(self, backend_class=None, **options) -> None: ...
    def backend_for(self, source) -> BaseSFTPBackend: ...
    def close(self) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc_info) -> None: ...
```

Importing `sftppath.uri` needs `uripath` (`pip install "sftppath[uri]"`); without
it the import raises an `ImportError` naming that extra. `uripath` finds the
class itself, through the entry point `sftp` of the group `uripath.schemes`,
the first time a path of the scheme is used, so `URIPath("sftp://u@h/srv/x")`
needs no import of this package. A path of the scheme is an `SFTPPath` behind
a URI: the URI layer (parsing, joining, how paths of one endpoint share a
backend, `str()` without the password, pickling) is `uripath`'s, and every
request is made by the `SFTPPath` that the URI names, so the errors, the
caches and the native walks are the ones above. An error that names the
remote path names the URI. A URI without a path (`sftp://h`) is the root.
`os.fspath()` and `host_fspath()` return `.path`, the path on the server.

- `ssh_config` is a keyword of the constructor with the meaning above. It is
  carried to every path derived from the path on the same endpoint (`/`,
  `parent`, `with_name()`, `with_source()`, a join, a destination string), and
  dropped, with the backend, when the derived path is on another host. Paths of
  one endpoint share the backend they build for themselves unless their
  `ssh_config` differs: one built with another `ssh_config` than the path it
  derives from has a backend of its own.
- `backend=` is an `SFTPURIBackend`, or a `BaseSFTPBackend` used as it is for
  every path whatever host the URI names. Without one, a path builds an
  `SFTPURIBackend` of the class that "Backend selection" chooses; the user, the
  password and the port of the URI go to the backend of each server it names.
  A supplied backend is not part of a pickle or a copy of the path (`copy.copy`
  shares it).
- `SFTPURIBackend(backend_class=None, **options)` is how the connections of
  the `sftp:` paths are made: `backend_class` is a `BaseSFTPBackend` subclass
  (`SFTPBackend`, `AsyncSSHBackend`, or your own; `None` → the selection
  above) and `options` its constructor keywords (`connect_opts`, `ssh_config`,
  `timeout`, `known_hosts`, ...). `backend_for(source)` is the backend for the
  server and account of a `uripath.source.Source`, the same object for as long
  as a path or a connection holds it, so paths to one server share a
  connection per backend. `close()` closes every connection those backends
  hold and is harmless twice; leaving a `with` block calls it.
- Two paths are on different filesystems when both carry a supplied backend
  and those differ; `rename()`, `replace()` and `hardlink_to()` onto another
  host are `NotImplementedError` (so `move()` copies), and a directory copied
  to another host, to another supplied backend or to a local path takes the
  generic copy.

## Exceptions

### `sftppath.exceptions`

```python
class SFTPPathError(PathError): ...
class SFTPAuthenticationError(SFTPPathError, PermissionError): ...
class SFTPHostKeyError(SFTPPathError, ConnectionError): ...
```

`SFTPPathError` is the base of what this package raises on its own, itself a
`pathapi.PathError`; the root exports the three. Every failure of the
server is the `OSError` type pathlib raises for it, and no exception of paramiko
or asyncssh crosses the API. A bad argument is a plain `ValueError` or
`TypeError`.

- `SFTPAuthenticationError` — the server refused the credentials offered.
- `SFTPHostKeyError` — the host key was unknown, changed or not accepted; no
  credential was sent.

## Environment variables

### `SFTPPATH_BACKEND`

`auto` (the default), `asyncssh` or `paramiko`: the backend class a path builds
when it is given endpoint keywords and no `backend=`. Any other value, an empty
one and a differently cased one included, is a `ValueError` naming the
variable; for a named backend whose library is not installed, the first
request of a path raises an `ImportError` naming its extra. It is read the first time a backend is needed and kept for
the life of the process.

## Gotchas

- A relative path is read from the server root: `SFTPPath("data/x", host=h)` is
  `/data/x`, not a file in the home directory of the account.
- `SFTPPath.readlink()` returns a path, so its dot segments collapse; the
  `SFTPURIPath` keeps the text of the target verbatim in `.path`.
- Both libraries connect lazily and cache the connection; a program that opens
  many backends should `close()` them (or use `with`).
- `AsyncSSHBackend` runs on one shared loop thread: do not call a path method
  from a callback that runs on it.
- `known_hosts=None` / `AutoAddPolicy` turn host-key verification off; the
  password is then sent to whoever answers.
