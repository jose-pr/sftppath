"""The `sftp:` scheme of `uripath`: `SFTPURIPath`, a URI path that delegates to
`SFTPPath`, and `SFTPURIBackend`, the options its connections are made with.

Importing this module needs `uripath`: `pip install "sftppath[uri]"`. `uripath`
loads it by itself, through the `uripath.schemes` entry point, the first time a
path of the scheme is used.
"""

from __future__ import annotations

try:
    import uripath as _uripath  # noqa: F401
except ImportError as _error:
    raise ImportError(
        'sftppath.uri needs the uripath distribution: pip install "sftppath[uri]"',
        name="uripath",
    ) from _error

import errno as _errno
import stat as _stat
import threading as _threading
import typing as _ty
import weakref as _weakref

from pathlib_next.providers import check_follow, copy_error_predicate
from uripath import URI
from uripath.schemes import DelegatingURIPath, URIPath
from uripath.source import Source, fold_host, same_authority

from ._base import BaseSFTPBackend
from ._path import SFTPPath
from ._rename import _REPLACING
from ._select import _resolve_default_backend_cls
from ._sshconfig import DEFAULT_SSH_CONFIG, _Sentinel

__all__ = ["SFTPURIBackend", "SFTPURIPath"]

_NO_SOURCE = Source(None, None, None, None)

# "No ssh_config argument given" -- distinct from DEFAULT_SSH_CONFIG so a later
# lazy `_init()` keeps a value captured at construction.
_UNSET_SSH_CONFIG = _Sentinel(__name__, "_UNSET_SSH_CONFIG")


def _same_config(a, b) -> bool:
    """Whether two `ssh_config` values name the same configuration."""
    return a is b or (
        a is not DEFAULT_SSH_CONFIG and b is not DEFAULT_SSH_CONFIG and a == b
    )


class SFTPURIBackend(object):
    """How the connections of `sftp:` paths are made: the backend class and the
    options of its constructor (`connect_opts`, `ssh_config`, `timeout`, ...).
    `backend_for(source)` builds the backend for a URI's server and account,
    once for as long as a path or a connection holds it, so paths to one server
    share their connections.

    `backend_class` is a `BaseSFTPBackend` subclass; without one, the class
    `SFTPPATH_BACKEND` selects is used. The user, password and port of the URI
    are given to it; `options` are given as they are.

    A context manager: leaving the block calls `close()`."""

    # Weakly referenceable so `URIPath` can record which backends a path
    # derived for itself.
    __slots__ = ("backend_class", "options", "_backends", "_lock", "__weakref__")

    def __init__(
        self,
        backend_class: "type[BaseSFTPBackend] | None" = None,
        **options,
    ) -> None:
        self.backend_class = backend_class
        self.options = options
        # The backends made, by folded endpoint, while a path or a cached
        # connection holds one.
        self._backends: "_weakref.WeakValueDictionary[Source, BaseSFTPBackend]" = (
            _weakref.WeakValueDictionary()
        )
        self._lock = _threading.Lock()

    def __repr__(self) -> str:
        name = None if self.backend_class is None else self.backend_class.__name__
        return f"{type(self).__name__}({name!r}, {sorted(self.options)})"

    def backend_for(self, source: Source) -> BaseSFTPBackend:
        """The backend for the server and account `source` names."""
        key = source._replace(host=fold_host(source.host))
        with self._lock:
            found = self._backends.get(key)
            if found is None:
                cls = self.backend_class or _resolve_default_backend_cls()
                user, password = source.parsed_userinfo()
                found = self._backends[key] = cls(
                    str(source.host),
                    source.port,
                    user=user or None,
                    password=password or None,
                    **self.options,
                )
            return found

    def close(self) -> None:
        """Closes every connection the paths of this backend hold, whichever
        thread opened it, and returns when they are closed. Harmless when
        called again; a later use connects afresh."""
        with self._lock:
            held = list(self._backends.values())
        for backend in held:
            backend.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class SFTPURIPath(DelegatingURIPath):
    """`sftp:` scheme: full read/write access, done by an `SFTPPath` on the
    connections of `backend` (an `SFTPURIBackend`, or a `BaseSFTPBackend` used
    as it is for every path whatever host the URI names). See "backend
    selection" in `SFTPPath`; one of its SSH-library extras is needed.

    `ssh_config` -- default `~/.ssh/config`, `None` for none, a path or an
    iterable of paths -- is carried to every path derived from this one on the
    same endpoint and dropped, with the backend, when the derived path is on
    another host."""

    __SCHEMES = ("sftp",)
    __slots__ = ("_config",)
    _OPTIONS = URIPath._OPTIONS | {"ssh_config"}
    _host_filesystem_path = True
    _DELEGATED = (
        "_scandir",
        "_listdir",
        "_open",
        "_mkdir",
        "unlink",
        "rmdir",
        "chmod",
        "_chown",
        "_utime",
    )

    #: Class-level backend override, for a subclass to pin its own default
    #: without touching process env state. Wins over the env var, loses to
    #: an explicit `backend=` constructor kwarg.
    _default_backend_cls: "type[BaseSFTPBackend] | None" = None

    if _ty.TYPE_CHECKING:
        backend: "SFTPURIBackend | BaseSFTPBackend"

    def __new__(cls, *args, ssh_config=_UNSET_SSH_CONFIG, **kwargs):
        # A direct `SFTPURIPath(url, ssh_config=...)` parses lazily and never passes its
        # keywords to `_init()`, so capture the value here; without one, `_init()` takes
        # it from the join segment the backend is inherited from.
        inst = super().__new__(cls, *args, **kwargs)
        if isinstance(inst, SFTPURIPath):
            inst._config = ssh_config
        return inst

    @property
    def _ssh_config(self):
        """The `ssh_config` of this path. A path still waiting to be parsed
        takes it from the join it was made of when it is parsed."""
        config = self._config
        if config is _UNSET_SSH_CONFIG:
            if self._raw_uris and not self._initiated:
                self._load_parts()
                config = self._config
            if config is _UNSET_SSH_CONFIG:
                return DEFAULT_SSH_CONFIG
        return config

    @_ssh_config.setter
    def _ssh_config(self, value) -> None:
        self._config = value

    def _config_for(self, source: Source):
        """The `ssh_config` a path derived from this one at `source` has: this
        one's on the same endpoint, else the default (a path on another host
        takes neither this path's backend nor its configuration)."""
        config = self._ssh_config
        mine = self._source
        if source is mine or same_authority(source, mine):
            return config
        return DEFAULT_SSH_CONFIG

    def _joined_config(self, source: Source):
        """The `ssh_config` of the join segment this path takes its backend or
        shared slot from (`URIPath._inherit_backend()`), or the unset marker."""
        winner = None
        for segment, backend, _derived in reversed(self._backend_candidates or ()):
            if isinstance(segment, SFTPURIPath) and same_authority(
                segment.source, source
            ):
                if backend is not None:
                    return segment._ssh_config
                if winner is None:
                    winner = segment
        return _UNSET_SSH_CONFIG if winner is None else winner._ssh_config

    def _own_family(self) -> None:
        """Leave the connection family this path was derived into: it has an
        `ssh_config` of its own, so a backend built for the others (or one
        they will build) is not its backend."""
        if self._backend_derived:
            self._backend = None
            self._backend_derived = None
        self._derived_cell = None

    def _initbackend(self):
        return SFTPURIBackend(self._default_backend_cls, ssh_config=self._ssh_config)

    def _reduce_options(self):
        # The default is a sentinel that must not travel as a copy of itself.
        if self._ssh_config is DEFAULT_SSH_CONFIG:
            return {}
        return {"ssh_config": self._ssh_config}

    def _from_parsed_parts(self, source, path, query, fragment, /, **kwargs):
        given = kwargs.get("ssh_config", _UNSET_SSH_CONFIG)
        kwargs["ssh_config"] = (
            self._config_for(source) if given is _UNSET_SSH_CONFIG else given
        )
        inst = super()._from_parsed_parts(source, path, query, fragment, **kwargs)
        if (
            given is not _UNSET_SSH_CONFIG
            and "backend" not in kwargs
            and not _same_config(given, self._config_for(source))
        ):
            inst._own_family()
        return inst

    def with_source(self, source):
        inst = super().with_source(source)
        if isinstance(inst, SFTPURIPath):
            inst._ssh_config = self._config_for(source)
        return inst

    def _coerce_target(self, target):
        destination = super()._coerce_target(target)
        if isinstance(destination, SFTPURIPath) and destination is not self:
            destination._ssh_config = self._config_for(destination.source)
        return destination

    def _init(
        self,
        source,
        path,
        query,
        fragment,
        /,
        backend=None,
        ssh_config=_UNSET_SSH_CONFIG,
        **kwargs,
    ):
        if ssh_config is not _UNSET_SSH_CONFIG:
            self._config = ssh_config
        given = self._config
        joined = self._joined_config(source)
        if given is _UNSET_SSH_CONFIG:
            self._config = DEFAULT_SSH_CONFIG if joined is _UNSET_SSH_CONFIG else joined
        super()._init(source, path, query, fragment, backend=backend, **kwargs)
        if (
            backend is None
            and given is not _UNSET_SSH_CONFIG
            and joined is not _UNSET_SSH_CONFIG
            and not _same_config(given, joined)
        ):
            self._own_family()

    def _make_core(self) -> SFTPPath:
        owner = self.backend
        if isinstance(owner, BaseSFTPBackend):
            backend = owner
        elif isinstance(owner, SFTPURIBackend):
            backend = owner.backend_for(self.source)
        else:
            raise TypeError(
                "an sftp: path takes an SFTPURIBackend or a BaseSFTPBackend as "
                f"backend=, not {type(owner).__name__!r}"
            )
        return SFTPPath(self.path or "/", backend=backend)

    def _core_of(self, target: "SFTPURIPath") -> SFTPPath:
        """The path of this path's core backend that `target` names."""
        return self._core_path.with_segments(target.path or "/")

    def _from_core(self, path: SFTPPath) -> "SFTPURIPath":
        """The URI path on this endpoint for a path of the core backend."""
        return self.with_path(path._wirepath)

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None and (
            not follow_symlinks
            # The hint comes from listdir_attr(), which never resolves
            # symlinks: it is the answer to an lstat, and for an entry that
            # says what it is and is not a link that is the answer to a stat.
            or (_stat.S_IFMT(hint.st_mode) and not hint.is_symlink())
        ):
            return hint
        with self._delegated():
            return self._core_path.stat(follow_symlinks=follow_symlinks)

    def _symlink_to(self, target: URI, target_is_directory: bool = False) -> None:
        # `.path`, not as_posix() (which prefixes "host:"); verbatim, with no
        # dot-segment folding.
        with self._delegated():
            self._core_path._link_to(target.path)

    def readlink(self) -> "SFTPURIPath":
        # The raw target string, unresolved and verbatim (no dot-segment folding), as
        # pathlib's readlink(): it is a result, not a destination. A relative target has
        # no host; with this path's authority it would have no printable URI.
        with self._delegated():
            target = self._core_path._readlink_text()
        source = self.source if target.startswith("/") else _NO_SOURCE
        return self._from_parsed_parts(source, target, None, None)

    def hardlink_to(self, target: "SFTPURIPath | URI | str"):
        if isinstance(target, URI) and not self._same_location(target):
            raise NotImplementedError(
                f"hardlink_to() cannot link across hosts: {self} -> {target}"
            )
        target_path = target.path if isinstance(target, URI) else str(target)
        with self._delegated():
            self._core_path.hardlink_to(target_path)

    def supported_checksums(self) -> "_ty.FrozenSet[str]":
        with self._delegated():
            return self._core_path.supported_checksums()

    def checksum(self, algorithm: str = "md5") -> str:
        with self._delegated():
            return self._core_path.checksum(algorithm)

    def rename(self, target: "SFTPURIPath | URI | str"):
        # A plain str target is a sibling rename (relative to self's parent): a
        # literal name, not re-parsed as a URI. An existing target is replaced via
        # `posix-rename@openssh.com` where the server has it.
        target = self._rename_target(target)
        destination = self.with_path(target.path)
        moved = self._core_of(target)
        with self._delegated((str(moved), str(destination))):
            self._core_path.rename(moved)
        # pathlib returns the new path.
        return destination

    def replace(self, target: "SFTPURIPath | URI | str"):
        """Rename this entry to `target`, replacing what is there, and return
        `target` as a path. Onto a path of this server it is the `replace()` of
        `SFTPPath`; onto anything else it is `Path.replace()`, which
        `move(overwrite=True)` uses too."""
        if isinstance(target, str):
            target = self._coerce_target(target)
        if type(target) is type(self) and self._same_location(target):
            moved = self._core_of(target)
            with self._delegated((str(moved), str(target))):
                self._core_path.replace(moved)
            return target
        pending = _REPLACING.get()
        if pending is not None and pending[0] is self and pending[1] is target:
            # `Path.replace()` below reached `move()`, which asked this method
            # again: the target is not on this server, so `move()` copies.
            raise OSError(_errno.EXDEV, "Cross-device link", str(target.path))
        token = _REPLACING.set((self, target))
        try:
            return super().replace(target)
        finally:
            _REPLACING.reset(token)

    def rm(
        self,
        /,
        recursive=False,
        missing_ok=False,
        ignore_error: "bool | _ty.Callable[[Exception, _ty.Self], bool]" = False,
        *,
        follow_symlinks=False,
        follow_binds=False,
    ):
        check_follow("follow_symlinks", follow_symlinks)
        check_follow("follow_binds", follow_binds)
        # The native walker unlinks a symlink and never follows one, which is
        # the default policy; any other `follow_symlinks` takes the generic walk.
        if (
            recursive
            and follow_symlinks is False
            and self._core_path.backend.supports_tree
        ):
            on_error = ignore_error
            if callable(ignore_error):

                def on_error(error, path):
                    return ignore_error(error, self._from_core(path))

            with self._delegated():
                return self._core_path.rm(
                    recursive=True,
                    missing_ok=missing_ok,
                    ignore_error=on_error,
                    follow_symlinks=follow_symlinks,
                    follow_binds=follow_binds,
                )
        return super().rm(
            recursive=recursive,
            missing_ok=missing_ok,
            ignore_error=ignore_error,
            follow_symlinks=follow_symlinks,
            follow_binds=follow_binds,
        )

    def copy(
        self,
        target,
        *,
        overwrite=False,
        follow_symlinks=True,
        preserve_metadata=True,
        recursive=False,
        ignore_error=None,
        progress=None,
        on_error=None,
    ):
        """Copy with the backend's concurrent fan-out: a directory copied to a
        path of the same server and tree is done by the native walk of
        `SFTPPath.copy()`; every other copy is `Path.copy()`. `progress` is not
        called during the native fan-out."""
        offering = copy_error_predicate(ignore_error, on_error)
        # Passed on only when given: a backend or a `copy()` override without
        # the keyword has no parameter for it.
        extra = {}
        if offering is not None:

            def asked(error, path):
                return offering(error, self._from_core(path))

            extra = {"on_error": asked}
        if (
            not recursive
            or not self._core_path.backend.supports_tree
            # The fan-out writes every destination file over THIS path's connection: only a
            # target on the same host and tree (two supplied backends may reach different
            # servers) may use it.
            or not isinstance(target, SFTPURIPath)
            # The native walk keeps the setuid, setgid and sticky bits only between
            # two paths of one class, and both are an `SFTPPath` here.
            or type(target) is not type(self)
            or not self._same_location(target)
            or not self._same_filesystem(target)
            # A link copied as a link is the generic copy's job.
            or (not follow_symlinks and self.is_symlink())
            or not self.is_dir()
        ):
            return super().copy(
                target,
                overwrite=overwrite,
                follow_symlinks=follow_symlinks,
                preserve_metadata=preserve_metadata,
                recursive=recursive,
                ignore_error=ignore_error,
                progress=progress,
                **({} if offering is None else {"on_error": offering}),
            )
        moved = self._core_of(target)
        with self._delegated((str(moved), str(target))):
            return self._core_path._copy_tree(
                moved,
                overwrite=overwrite,
                follow_symlinks=follow_symlinks,
                preserve_metadata=preserve_metadata,
                ignore_error=ignore_error,
                **extra,
            )
