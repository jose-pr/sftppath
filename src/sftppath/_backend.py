from __future__ import annotations

import typing as _ty

from pathlib_next.providers import notimplemented as _notimplemented

if _ty.TYPE_CHECKING:
    from ._path import SFTPPath

DEFAULT_PORT = 22
"""The port a connection uses when neither the caller nor the ssh_config of the
host names one."""


class SFTPEndpoint(_ty.NamedTuple):
    """The server and account a backend connects to: what two backends must
    share for their paths to name the same files. The password is not part of
    it, and the host name is in lower case. `port` is `None` when the caller
    named none (the ssh_config `Port` of the host, else 22, applies)."""

    host: str
    port: "int | None"
    user: "str | None"


class BaseSFTPBackend(object):
    """Contract for obtaining a ready-to-use, paramiko-shaped `SFTPClient` for
    one server. Subclass this to plug in custom connection handling (tests
    mock it directly, no real server); `SFTPBackend` (paramiko) and
    `AsyncSSHBackend` (asyncssh) are the real implementations. Connection
    caching is each backend's own responsibility: `client()` is expected to
    return an already-cached-or-freshly-opened client, and `SFTPPath` does no
    per-backend branching anywhere.

    `endpoint`, when a subclass sets it, is a hashable value naming the server
    and account: two backends with equal endpoints serve the same files.

    A context manager: leaving the block calls `close()`."""

    # Weakly referenceable so a path can be told which backends it built.
    __slots__ = ("__weakref__",)

    endpoint: "_ty.Hashable | None" = None

    #: Whether `chmod(follow_symlinks=False)` is supported. paramiko has no
    #: lchmod equivalent to call; asyncssh's `chmod()` takes
    #: `follow_symlinks` natively.
    supports_lchmod = False

    #: Whether `hardlink_to()` is supported. SFTPv3 (paramiko's ceiling)
    #: has no core hard-link operation at all.
    supports_hardlink = False

    #: Whether this backend has its own concurrent recursive `copy()` and
    #: `rm()` (`tree_copy()`, `tree_rm()`), which `SFTPPath` then uses for a
    #: directory on one server; the generic walks serve every other case.
    supports_tree = False

    def _wire_open_mode(self, mode: str) -> str:
        """The mode string this backend's client `open()` needs for a
        pathlib `_open()` mode (no "b"). Default: unchanged."""
        return mode

    def tree_copy(
        self,
        path: "SFTPPath",
        target: "SFTPPath",
        *,
        overwrite: bool,
        follow_symlinks: bool,
        preserve_metadata: bool,
        ignore_error,
        on_error=None,
    ) -> None:
        """Copy the directory `path` into the existing `target` directory,
        on the server both are on. Only called when `supports_tree`.
        `on_error(error, path)`, when given, decides whether a failure of one
        child is skipped; `ignore_error` is then None."""
        raise NotImplementedError("tree_copy()")

    def tree_rm(self, path: "SFTPPath", *, missing_ok: bool, on_error) -> None:
        """Remove `path` and everything below it, never following a link.
        `on_error(error, path)` decides whether a failure is skipped. Only
        called when `supports_tree`."""
        raise NotImplementedError("tree_rm()")

    def supported_checksums(self, path: "SFTPPath") -> "_ty.FrozenSet[str]":
        """Advisory set of algorithm names `checksum()` can currently
        produce against `path`'s server connection (see
        `pathlib_next.protocols.checksum.NativeChecksum.supported_checksums`).
        Empty here (the default): no native-hashing support at all. Both real
        backends override this with a per-connection probe against `path`
        (neither library exposes the server's extension list -- see
        `_checkfile.CheckFileSFTPBackend.supported_checksums`). Takes a
        `path` argument (unlike a bare capability flag) because the only
        reliable way to know is to actually try the extension against a
        real file.
        """
        return frozenset()

    @_notimplemented
    def client(self):
        """A ready-to-use SFTP client for this backend's server, cached or
        newly opened."""

    def close(self) -> None:
        """Closes every connection this backend has cached, whichever thread
        opened it, and returns when they are closed. A no-op here; both real
        backends override it. Harmless when called again; the backend stays
        usable, and the next `client()` call reconnects."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @_notimplemented
    def checksum(self, path: "SFTPPath", algorithm: str) -> str:
        """Backend-native digest for `path`'s content, e.g. via the
        filexfer draft's `check-file-handle` SFTP extension (implemented by
        some servers, e.g. ProFTPD's mod_sftp; OpenSSH has no such
        extension). Raises
        `NotImplementedError` (the base/default here) when the backend has
        no such capability at all, and MUST also raise it -- not return a
        value -- when the server doesn't advertise `algorithm` specifically
        (see `pathlib_next.protocols.checksum.NativeChecksum.checksum` for
        why this is a hard contract, not a style choice). Both real
        backends implement it through
        `_checkfile.CheckFileSFTPBackend`, each supplying only the extended
        request itself.
        """
        ...
