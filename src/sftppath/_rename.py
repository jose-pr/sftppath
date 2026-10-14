"""What `SFTPPath.rename()` and `replace()` remember between calls."""

from __future__ import annotations

import contextvars as _contextvars
import weakref as _weakref

#: SFTP clients on which `posix-rename@openssh.com` proved unavailable (the
#: extension request failed and a plain rename then succeeded), so later
#: renames skip the doomed request. Weak: a reconnect starts over.
_NO_POSIX_RENAME: "_weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()


def _posix_rename_known_unsupported(client) -> bool:
    try:
        return client in _NO_POSIX_RENAME
    except TypeError:
        return False


def _mark_posix_rename_unsupported(client) -> None:
    try:
        _NO_POSIX_RENAME[client] = True
    except TypeError:
        pass


#: The `(source, target)` of the `SFTPPath.replace()` that handed its work to
#: the generic `replace()`, so that `move()` calling `replace()` again knows
#: it is the same call.
_REPLACING: "_contextvars.ContextVar[tuple | None]" = _contextvars.ContextVar(
    "sftppath_replacing", default=None
)
