from __future__ import annotations

import errno as _errno
import ipaddress as _ipaddress
import os as _os
import posixpath as _posix
import stat as _stat
import typing as _ty
import urllib.parse as _urlparse

from pathapi import FileStat
from pathapi import providers as _providers
from pathapi.providers import (
    TreePath,
    check_follow,
    contains,
    copy_error_predicate,
)

from ._backend import BaseSFTPBackend, SFTPEndpoint
from ._exit import _track_unclosed
from ._namespace import _namespace_of
from ._rename import (
    _REPLACING,
    _mark_posix_rename_unsupported,
    _posix_rename_known_unsupported,
)
from ._select import _MissingBackend, _resolve_default_backend_cls
from ._sshconfig import DEFAULT_SSH_CONFIG, UNSET
from .exceptions import SFTPAuthenticationError


def _rebuild_path(cls, text, backend):
    return cls(text, backend=backend)


class SFTPPath(TreePath):
    """A path on an SFTP server, over paramiko or asyncssh (see "backend
    selection" in `_select`). A request needs the `paramiko` or the `asyncssh`
    extra; naming, joining and comparing paths needs neither.
    Also implements `pathapi.protocols.checksum.NativeChecksum`
    (`checksum()`, delegating to `self.backend.checksum()`) -- native on both
    backends via the filexfer draft's `check-file-handle` extension where the
    server implements it (OpenSSH does not), `NotImplementedError` (falls back
    to streaming) on a server without that extension.

    `SFTPPath("/srv/x", host="h", user="u")` builds the path's own backend
    from the endpoint keywords and the connect options (`ssh_config=`,
    `timeout=`, `connect_opts=`, and the keywords of the backend class that
    `SFTPPATH_BACKEND` selects); `backend=` instead shares one connection
    between paths built separately, or plugs in a `BaseSFTPBackend` of your
    own. Giving `backend=` together with any of those keywords is a
    `TypeError`, and so is giving neither (unless a segment is an `SFTPPath`,
    whose backend is kept). Nothing connects when a path is built. Every path
    derived from one (`/`, `joinpath`, `parent`, `with_name`, `iterdir`,
    `relative_to`) keeps its backend. Names are read from the server root:
    `srv/x` and `/srv/x` are one file."""

    __slots__ = ("_backend",)

    #: Class-level backend override, for a subclass to pin its own default
    #: without touching process env state. Wins over the env var, loses to
    #: an explicit `backend=` constructor kwarg.
    _default_backend_cls: "type[BaseSFTPBackend] | None" = None

    if _ty.TYPE_CHECKING:
        _backend: BaseSFTPBackend

    def __init__(
        self,
        *segments: "str | SFTPPath",
        backend: "BaseSFTPBackend | None" = None,
        host: "str | None" = None,
        port: "int | None" = None,
        user: "str | None" = None,
        password: "str | None" = None,
        ssh_config: object = DEFAULT_SSH_CONFIG,
        timeout: "float | None | object" = UNSET,
        connect_opts: "dict | None" = None,
        **options,
    ):
        text, donor = self._join(segments)
        endpoint = (
            host is not None
            or port is not None
            or user is not None
            or password is not None
            or ssh_config is not DEFAULT_SSH_CONFIG
            or timeout is not UNSET
            or connect_opts is not None
            or bool(options)
        )
        if backend is not None and endpoint:
            raise TypeError(
                "pass either backend= or the endpoint keywords "
                "(host=, port=, user=, ...), not both"
            )
        if backend is None:
            if endpoint:
                if host is None:
                    raise TypeError(
                        "host= is required with the other endpoint keywords"
                    )
                backend = self._new_backend(
                    host,
                    port,
                    user,
                    password,
                    ssh_config,
                    timeout,
                    connect_opts,
                    options,
                )
            elif donor is not None:
                backend = donor._backend
            else:
                raise TypeError("an SFTPPath needs host= or backend=")
        self._segments = self._parse(text)
        self._backend = backend

    @classmethod
    def _new_backend(
        cls, host, port, user, password, ssh_config, timeout, connect_opts, options
    ) -> BaseSFTPBackend:
        """The backend of the class `_default_backend_cls` or `SFTPPATH_BACKEND`
        selects, for these keywords."""
        try:
            backend_cls = cls._default_backend_cls or _resolve_default_backend_cls()
        except ImportError as error:
            # The library is needed when a request is made, not to name a file.
            return _MissingBackend(host, port, user, str(error))
        extra = dict(options)
        if timeout is not UNSET:
            extra["timeout"] = timeout
        if connect_opts is not None:
            extra["connect_opts"] = connect_opts
        return backend_cls(
            host, port, user=user, password=password, ssh_config=ssh_config, **extra
        )

    def _rebuild(self, text: str) -> "SFTPPath":
        return type(self)(text, backend=self._backend)

    def __reduce__(self):
        # The text and the backend; a backend pickles without its
        # connections, and with the password only if it was given one.
        return _rebuild_path, (type(self), self.as_posix(), self._backend)

    def __repr__(self) -> str:
        backend = self._backend
        shown = [repr(self.as_posix())]
        if isinstance(backend.endpoint, SFTPEndpoint):
            shown.append(f"host={backend.host!r}")
            if backend.port is not None:
                shown.append(f"port={backend.port}")
            if backend.user is not None:
                shown.append(f"user={backend.user!r}")
        else:
            shown.append(f"backend={backend!r}")
        return f"{type(self).__name__}({', '.join(shown)})"

    @property
    def backend(self) -> BaseSFTPBackend:
        """The backend (`SFTPBackend`, `AsyncSSHBackend` or a custom
        `BaseSFTPBackend`) holding the connections of this path."""
        return self._backend

    def as_uri(self) -> str:
        backend = self._backend
        host = getattr(backend, "host", None)
        if host is None:
            raise NotImplementedError("as_uri() needs a backend with a host")
        netloc = f"[{host}]" if ":" in host else host
        if backend.user is not None:
            netloc = f"{_urlparse.quote(backend.user, safe='')}@{netloc}"
        if backend.port is not None:
            netloc = f"{netloc}:{backend.port}"
        return f"sftp://{netloc}{_urlparse.quote(self._wirepath, safe='/')}"

    def is_local(self) -> bool:
        """Whether the server is this machine, as far as its name says: a
        loopback or unspecified address, or `localhost`. A host name is not
        resolved, and a backend with no host counts as local. `PathSyncer`
        skips its `quick_check` for two local sides."""
        host = getattr(self._backend, "host", None)
        if host is None:
            return True
        name = str(host).strip("[]").split("%", 1)[0].lower()
        if name == "localhost" or name.endswith(".localhost"):
            return True
        try:
            address = _ipaddress.ip_address(name)
        except ValueError:
            return False
        return address.is_loopback or address.is_unspecified

    def _same_filesystem(self, other: "SFTPPath") -> bool:
        # Equality ignores the server: two paths are one tree when they share a
        # backend or name the same host, port and account.
        mine, theirs = self._backend, other._backend
        if mine is theirs:
            return True
        endpoint = mine.endpoint
        return endpoint is not None and endpoint == theirs.endpoint

    def _node_key(self):
        # "x", "/x" and "/d/../x" are one node. This reads no server, so `..`
        # is applied to the text.
        posix = "/".join(self.segments).lstrip("/")
        names = _posix.normpath("/" + posix).removeprefix("/").split("/")
        return _namespace_of(self._backend), tuple(name for name in names if name)

    @property
    def _wirepath(self) -> str:
        """This path as a request argument: rooted, and `/` for the root. An
        empty argument would name the session's start directory instead."""
        return "/" + "/".join(name for name in self.segments if name)

    @property
    def _sftpclient(self):
        return self._backend.client()

    def iterdir(self):
        for name, _stat in self._scandir():
            yield self.with_segments(*self.segments, name)

    def _scandir(self):
        # listdir_attr() gets attrs (lstat-like -- symlinks are not
        # resolved) for every child in one round trip, instead of a plain
        # name list (listdir()) plus a separate stat()/lstat() per child.
        try:
            attrs = self._sftpclient.listdir_attr(self._wirepath)
        except OSError as error:
            translated = self._directory_error(error)
            if translated is None:
                raise
            raise translated from error
        for attr in attrs:
            name = attr.filename
            if not _providers.is_safe_child_name(name):
                # A listing is untrusted: a name that is not a single component of this
                # directory ("..", anything with "/") must never become a child, or rm(recursive)
                # deletes outside the tree. Both backends' `listdir_attr()` arrive here.
                continue
            listed = FileStat.from_stat(attr)
            # A listing that does not say what kind of entry it is leaves the
            # question to the caller's own stat.
            yield name, listed if _stat.S_IFMT(listed.st_mode) else None

    def _entry_stat(self):
        """This entry's stat, or None if even that fails."""
        try:
            return FileStat.from_stat(self._sftpclient.stat(self._wirepath))
        except OSError:
            return None

    @staticmethod
    def _unreachable(error: OSError) -> bool:
        """Whether `error` says the server could not be reached or logged
        into, not that it refused this one request: asking it about the entry
        would only try the same connection again."""
        return isinstance(
            error, (ConnectionError, TimeoutError, SFTPAuthenticationError)
        )

    def _directory_error(self, error: OSError, *, check_empty=False):
        """pathlib's exception for a failed directory operation (listing,
        `rmdir()`), or None to keep `error`. SFTPv3 has no ENOTDIR or
        ENOTEMPTY status: servers send "no such file" (OpenSSH maps ENOTDIR
        to it) or a bare failure, so the entry itself is consulted -- on this
        failure path only."""
        if self._unreachable(error):
            return None
        stat = self._entry_stat()
        if stat is None:
            return None
        if not stat.is_dir():
            return NotADirectoryError(
                _errno.ENOTDIR, _os.strerror(_errno.ENOTDIR), str(self)
            )
        if check_empty and error.errno is None:
            try:
                has_children = bool(self._sftpclient.listdir_attr(self._wirepath))
            except OSError:
                return None
            if has_children:
                return OSError(
                    _errno.ENOTEMPTY, _os.strerror(_errno.ENOTEMPTY), str(self)
                )
        return None

    def _file_error(self, error: OSError):
        """pathlib's exception for a file operation (open, unlink) that
        failed on a directory, or None to keep `error`. SFTPv3 has no EISDIR
        status either: OpenSSH-style servers send a bare failure. A status
        with an errno (permission denied, no such file) is already right."""
        if error.errno is not None or self._unreachable(error):
            return None
        stat = self._entry_stat()
        if stat is None or not stat.is_dir():
            return None
        return IsADirectoryError(_errno.EISDIR, _os.strerror(_errno.EISDIR), str(self))

    def stat(self, *, follow_symlinks=True):
        if follow_symlinks:
            return self._sftpclient.stat(self._wirepath)
        else:
            return self._sftpclient.lstat(self._wirepath)

    def _open(self, mode="r", buffering=-1):
        try:
            file = self._sftpclient.open(
                self._wirepath, self._backend._wire_open_mode(mode), buffering
            )
            if any(char in mode for char in "wax+"):
                _track_unclosed(file)
            return file
        except OSError as error:
            # SFTPv3 has no "already exists" status: an O_EXCL ("x") failure comes back as
            # a generic failure, unlike the genuinely missing file or parent, which maps to
            # FileNotFoundError. True on both backends against a v3 server.
            if "x" in mode and not self._unreachable(error) and self.exists():
                raise FileExistsError(
                    _errno.EEXIST, _os.strerror(_errno.EEXIST), str(self)
                ) from error
            translated = self._file_error(error)
            if translated is None:
                raise
            raise translated from error

    def _mkdir(self, mode):
        try:
            return self._sftpclient.mkdir(self._wirepath, mode)
        except OSError as error:
            # Same SFTPv3 status-code gap as _open() above: mkdir on an
            # existing path also comes back as a generic failure.
            if not self._unreachable(error) and self.exists():
                raise FileExistsError(
                    _errno.EEXIST, _os.strerror(_errno.EEXIST), str(self)
                ) from error
            raise

    def chmod(self, mode: int | str, *, follow_symlinks: bool = True):
        mode = _providers.parse_mode(mode)
        if follow_symlinks:
            return self._sftpclient.chmod(self._wirepath, mode)
        if not self._backend.supports_lchmod:
            raise NotImplementedError("chmod(follow_symlinks=False)")
        return self._sftpclient.chmod(self._wirepath, mode, follow_symlinks=False)

    def _chown(
        self,
        uid: int | str | None,
        gid: int | str | None,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        # SFTPv3 setstat carries uid/gid as a numeric pair, both or neither (one UIDGID
        # flag), with no name resolution: a partial change reads the current value of the
        # field left alone, so chown() canonicalizes None instead of each backend inventing one.
        if isinstance(uid, str) or isinstance(gid, str):
            raise NotImplementedError("sftp chown() requires numeric uid/gid")
        if not follow_symlinks:
            raise NotImplementedError("chown(follow_symlinks=False)")
        if uid is None or gid is None:
            current = self._sftpclient.stat(self._wirepath)
            unknown = [
                name
                for name, wanted in (("uid", uid), ("gid", gid))
                if wanted is None
                and (
                    getattr(current, f"st_{name}") is None
                    or not getattr(current, f"{name}_known", True)
                )
            ]
            if unknown:
                raise NotImplementedError(
                    f"chown() cannot leave the {' and '.join(unknown)} of {self} "
                    "unchanged: the server reports it by name or not at all, "
                    "and the request would have to name one"
                )
            uid = current.st_uid if uid is None else uid
            gid = current.st_gid if gid is None else gid
        return self._sftpclient.chown(self._wirepath, uid, gid)

    def _utime(
        self, *, atime: "float | None" = None, mtime: "float | None" = None
    ) -> None:
        # SFTPv3 setstat sets the access and modification time as a pair, in
        # whole seconds: the one left alone is read from the server first.
        if atime is None or mtime is None:
            current = self._sftpclient.stat(self._wirepath)
            if atime is None:
                # A server that reports no atime reads 0 here, which would
                # put the file's access time at the epoch.
                atime = current.st_atime or (
                    current.st_mtime if mtime is None else mtime
                )
            if mtime is None:
                mtime = current.st_mtime
        self._sftpclient.utime(self._wirepath, (atime, mtime))

    def supported_checksums(self) -> "_ty.FrozenSet[str]":
        """`protocols.checksum.NativeChecksum` implementation: delegates to
        `self.backend.supported_checksums(self)` -- a real per-connection
        probe on both backends (see
        `_checkfile.CheckFileSFTPBackend.supported_checksums`), so this
        is empty when the connected server doesn't implement
        `check-file-handle` (OpenSSH never does).
        """
        return self._backend.supported_checksums(self)

    def checksum(self, algorithm: str = "md5") -> str:
        """`protocols.checksum.NativeChecksum` implementation: delegates to
        `self.backend.checksum()` (the `check-file-handle` SFTP extension
        on both backends -- see `BaseSFTPBackend.checksum`). Any failure
        that isn't already
        `NotImplementedError` (a server that doesn't advertise the
        extension, an unsupported algorithm, a transport-level error) is
        also translated to `NotImplementedError`: this method's whole
        contract is "raise if a genuine digest can't be produced", and a
        caller (e.g. `PathSyncer`) must be able to fall back to streaming
        on ANY such failure, not just the backend's own explicit signal.
        """
        try:
            return self._backend.checksum(self, algorithm)
        except NotImplementedError:
            raise
        except Exception as error:
            raise NotImplementedError(
                f"native checksum unavailable: {error}"
            ) from error

    def unlink(self, missing_ok=False):
        # No exists() pre-check: it follows symlinks, so a dangling link
        # read as missing and was never removed (breaking
        # symlink_to(force=True)). pathlib ignores only ENOENT from the call.
        try:
            return self._sftpclient.remove(self._wirepath)
        except FileNotFoundError:
            if not missing_ok:
                raise
        except OSError as error:
            translated = self._file_error(error)
            if translated is None:
                raise
            raise translated from error

    def rmdir(self):
        try:
            return self._sftpclient.rmdir(self._wirepath)
        except OSError as error:
            translated = self._directory_error(error, check_empty=True)
            if translated is None:
                raise
            raise translated from error

    def rename(self, target: "SFTPPath | str"):
        # A plain str target is a literal sibling name. An existing target is
        # replaced via `posix-rename@openssh.com`; plain v3 RENAME refuses
        # (FileExistsError).
        if isinstance(target, str):
            target = self.parent / target
        elif not isinstance(target, SFTPPath) or not self._same_filesystem(target):
            raise NotImplementedError("rename() onto another server")
        destination = self._rebuild(target.as_posix())
        self._rename_on_wire(destination)
        # pathlib returns the new path.
        return destination

    def replace(self, target: "SFTPPath | str"):
        """Rename this entry to `target`, replacing what is there, and return
        `target` as a path.

        Onto a path of this server the request is `posix-rename@openssh.com`
        where the server has it: one request, atomic, and if it fails both
        entries are as they were. A server that lacks the extension, or that
        refuses it without saying why, gets what `Path.replace()` does: the
        file at `target` is removed and then this entry is renamed. Onto
        anything else this is `Path.replace()`, which `move(overwrite=True)`
        uses too."""
        if isinstance(target, str):
            target = self.parent / target
        pending = _REPLACING.get()
        if pending is not None and pending[0] is self and pending[1] is target:
            # `Path.replace()` below reached `move()`, which found a file at
            # `target` and asked this method to swap it in.
            if not self._same_filesystem(target):
                raise OSError(_errno.EXDEV, "Cross-device link", target._wirepath)
            target.unlink(missing_ok=True)
            return self.rename(target)
        if type(target) is type(self) and self._same_filesystem(target):
            client = self._sftpclient
            posix_rename = getattr(client, "posix_rename", None)
            if posix_rename is not None and not _posix_rename_known_unsupported(client):
                try:
                    posix_rename(self._wirepath, target._wirepath)
                except NotImplementedError:
                    _mark_posix_rename_unsupported(client)
                except OSError as error:
                    # A refusal that names its cause is the answer, unless a directory stands at
                    # `target`: `Path.replace()` checks those before it removes anything.
                    if error.errno is not None and not self._is_directory_at(target):
                        raise
                else:
                    return target
        token = _REPLACING.set((self, target))
        try:
            return super().replace(target)
        finally:
            _REPLACING.reset(token)

    @staticmethod
    def _is_directory_at(path: "SFTPPath") -> bool:
        try:
            found = FileStat.from_path(path, follow_symlink=False)
        except Exception:
            return False
        return found is not None and found.is_dir() and not found.is_symlink()

    def _rename_on_wire(self, target: "SFTPPath") -> None:
        client = self._sftpclient
        posix_rename = getattr(client, "posix_rename", None)
        if posix_rename is not None and not _posix_rename_known_unsupported(client):
            try:
                return posix_rename(self._wirepath, target._wirepath)
            except NotImplementedError:
                # asyncssh: the server did not advertise the extension.
                _mark_posix_rename_unsupported(client)
            except OSError as error:
                if error.errno is not None:
                    raise
                # paramiko reports "unsupported" and "failed" alike (a
                # status without errno): a plain rename tells them apart.
                try:
                    result = client.rename(self._wirepath, target._wirepath)
                except OSError as rename_error:
                    self._raise_if_exists(target, rename_error)
                    raise
                _mark_posix_rename_unsupported(client)
                return result
        try:
            return client.rename(self._wirepath, target._wirepath)
        except OSError as error:
            self._raise_if_exists(target, error)
            raise

    def _raise_if_exists(self, target: "SFTPPath", error: OSError) -> None:
        if isinstance(error, (FileNotFoundError, PermissionError)) or self._unreachable(
            error
        ):
            return
        try:
            exists = FileStat.from_path(target, follow_symlink=False) is not None
        except Exception:
            return
        if exists:
            raise FileExistsError(
                _errno.EEXIST, "File exists", target._wirepath
            ) from error

    def _symlink_to(
        self, target: "SFTPPath", target_is_directory: bool = False
    ) -> None:
        # The primitive only: `Path.symlink_to()` owns the str normalization and `force=`.
        # The Windows target_is_directory hint is accepted and ignored, as for any
        # non-local path.
        self._link_to(target.as_posix())

    def _link_to(self, target: str) -> None:
        """Create a symlink at this path whose content is `target`, verbatim."""
        self._sftpclient.symlink(target, self._wirepath)

    def readlink(self) -> "SFTPPath":
        # The target is a result, not a destination: a path on this backend.
        return self._rebuild(self._readlink_text())

    def _readlink_text(self) -> str:
        """The target of the link at this path, as the server stores it."""
        return self._sftpclient.readlink(self._wirepath)

    def hardlink_to(self, target: "SFTPPath | str"):
        if not self._backend.supports_hardlink:
            raise NotImplementedError("hardlink_to() requires the asyncssh backend")
        if isinstance(target, SFTPPath):
            if not self._same_filesystem(target):
                raise NotImplementedError(
                    f"hardlink_to() cannot link across servers: {self} -> {target}"
                )
            target_path = target._wirepath
        else:
            target_path = str(target)
        self._sftpclient.link(target_path, self._wirepath)

    def rm(
        self,
        /,
        recursive=False,
        missing_ok=False,
        ignore_error: bool | _ty.Callable[[Exception, _ty.Self], bool] = False,
        *,
        follow_symlinks=False,
        follow_binds=False,
        max_workers=None,
    ):
        """Remove as `Path.rm()` does. `max_workers` is `Path.rm()`'s; the native
        asyncssh walk (`follow_symlinks=False`) has its own concurrency, bounded
        by `backend.max_concurrency`, and ignores it."""
        check_follow("follow_symlinks", follow_symlinks)
        check_follow("follow_binds", follow_binds)
        # The native walker unlinks a symlink and never follows one, which is
        # the default policy; any other `follow_symlinks` takes the generic
        # walk. A server has no bindings, so `follow_binds` decides nothing.
        if (
            not recursive
            or follow_symlinks is not False
            or not self._backend.supports_tree
        ):
            return super().rm(
                recursive=recursive,
                missing_ok=missing_ok,
                ignore_error=ignore_error,
                follow_symlinks=follow_symlinks,
                follow_binds=follow_binds,
                # Only when given: pathapi 0.1.1 has no such keyword.
                **({} if max_workers is None else {"max_workers": max_workers}),
            )

        on_error = None
        if ignore_error:
            on_error = (
                ignore_error
                if callable(ignore_error)
                else lambda _err, _path: bool(ignore_error)
            )

        return self._backend.tree_rm(self, missing_ok=missing_ok, on_error=on_error)

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
        max_workers=None,
    ):
        """Copy with concurrent fan-out on the asyncssh backend.

        When using the asyncssh backend with `recursive=True` on a
        directory, child copies are fanned out as concurrent native requests,
        bounded by `backend.max_concurrency` (requests in flight, and files
        open at once); the whole copy has no wall-clock timeout. `progress`
        is honored on the generic single-file path below, but not called
        during the concurrent native fan-out itself. `max_workers` is
        `Path.copy()`'s and applies to the generic copy only; the native
        fan-out ignores it.
        `on_error(error, path)` is `Path.copy()`'s predicate; the native
        fan-out calls it from a worker thread, once per failing child.
        """
        offering = copy_error_predicate(ignore_error, on_error)
        # Passed on only when given: a backend or a `copy()` override without
        # the keyword has no parameter for it.
        extra = {} if offering is None else {"on_error": offering}
        if (
            not recursive
            or not self._backend.supports_tree
            # The fan-out writes every destination file over THIS path's connection: only a
            # target on the same server and tree (two supplied backends may reach different
            # servers) may use it.
            or not isinstance(target, SFTPPath)
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
                **extra,
                **({} if max_workers is None else {"max_workers": max_workers}),
            )

        return self._copy_tree(
            target,
            overwrite=overwrite,
            follow_symlinks=follow_symlinks,
            preserve_metadata=preserve_metadata,
            ignore_error=ignore_error,
            **extra,
        )

    def _copy_tree(
        self,
        target: "SFTPPath",
        *,
        overwrite,
        follow_symlinks,
        preserve_metadata,
        ignore_error,
        **extra,
    ):
        """Copy the directory at this path into `target`, on the same server,
        with the backend's own walk. `extra` is `on_error=` when it was given."""
        if contains(self, target):
            # As the generic copy(): checked before anything is created, or
            # the new directory is listed and copied into itself.
            raise OSError(
                _errno.EINVAL, "Cannot copy a directory into itself", str(target)
            )

        if target.exists():
            if not target.is_dir():
                raise FileExistsError(
                    _errno.EEXIST, _os.strerror(_errno.EEXIST), str(target)
                )
            if not overwrite:
                raise FileExistsError(
                    _errno.EEXIST, _os.strerror(_errno.EEXIST), str(target)
                )
        else:
            target.mkdir()

        # A whole-tree operation: no wall-clock bound (single requests are).
        return self._backend.tree_copy(
            self,
            target,
            overwrite=overwrite,
            follow_symlinks=follow_symlinks,
            preserve_metadata=preserve_metadata,
            ignore_error=ignore_error,
            **extra,
        )
