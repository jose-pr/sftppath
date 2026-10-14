"""`SFTPAttrs` as the `st_*`-shaped object `FileStat.from_stat()` reads."""

from __future__ import annotations

import stat as _stat

import asyncssh as _asyncssh

# SFTPAttrs has no st_-prefixed fields.
# `FileStat.from_stat()` reads them with getattr(..., 0), so a raw SFTPAttrs gives
# an all-zero FileStat without an error (exists() True, is_dir()/is_file() False).

_FILEXFER_TYPE_TO_S_IF = {
    _asyncssh.FILEXFER_TYPE_REGULAR: _stat.S_IFREG,
    _asyncssh.FILEXFER_TYPE_DIRECTORY: _stat.S_IFDIR,
    _asyncssh.FILEXFER_TYPE_SYMLINK: _stat.S_IFLNK,
    _asyncssh.FILEXFER_TYPE_SOCKET: _stat.S_IFSOCK,
    _asyncssh.FILEXFER_TYPE_CHAR_DEVICE: _stat.S_IFCHR,
    _asyncssh.FILEXFER_TYPE_BLOCK_DEVICE: _stat.S_IFBLK,
    _asyncssh.FILEXFER_TYPE_FIFO: _stat.S_IFIFO,
}


def _numeric_id(number: "int | None", name: "str | None") -> "int | None":
    if number is not None:
        return number
    return int(name) if name and name.isdigit() else None


class _StatAdapter:
    """Adapts an asyncssh `SFTPAttrs` to the `st_*`-shaped interface
    `FileStat.from_stat()` expects. Verified empirically (in-process
    asyncssh server, both v3 and a v6-requested-but-v3-negotiated
    session): `.permissions` already carries the combined `S_IFMT` type
    bits + permission bits in practice (e.g. `0o100666` for a regular
    file), so `S_ISDIR()`/`S_ISREG()` on it alone already work. Per the
    SFTP spec, a true v4+ server may instead report only the bare
    permission bits in `.permissions` and put the type in `.type` --
    handled defensively below (combine only when `.permissions` lacks
    `S_IFMT` bits) since that path isn't reachable against any server
    available to test against (asyncssh's own bundled `SFTPServer` stays
    at v3 regardless of the version requested; real-world OpenSSH is v3
    -only too)."""

    __slots__ = ("_attrs", "filename")

    def __init__(
        self, attrs: "_asyncssh.SFTPAttrs", filename: "str | bytes | None" = None
    ):
        self._attrs = attrs
        self.filename = filename

    @property
    def st_mode(self) -> int:
        attrs = self._attrs
        perms = attrs.permissions or 0
        if _stat.S_IFMT(perms):
            return perms
        type_bits = _FILEXFER_TYPE_TO_S_IF.get(attrs.type, 0)
        return perms | type_bits

    @property
    def st_nlink(self) -> int:
        return self._attrs.nlink or 1

    # An SFTP v4 server names the owner and the group ("owner"/"group") and
    # leaves uid/gid out: a numeric name is the id, any other leaves it 0 here
    # and `uid_known`/`gid_known` False, so `SFTPPath._chown()` never sends it.
    @property
    def st_uid(self) -> int:
        return _numeric_id(self._attrs.uid, self._attrs.owner) or 0

    @property
    def st_gid(self) -> int:
        return _numeric_id(self._attrs.gid, self._attrs.group) or 0

    @property
    def uid_known(self) -> bool:
        return _numeric_id(self._attrs.uid, self._attrs.owner) is not None

    @property
    def gid_known(self) -> bool:
        return _numeric_id(self._attrs.gid, self._attrs.group) is not None

    @property
    def st_size(self) -> int:
        return self._attrs.size or 0

    @property
    def st_atime(self):
        return self._attrs.atime or 0

    @property
    def st_mtime(self):
        return self._attrs.mtime or 0

    @property
    def st_ctime(self):
        return self._attrs.ctime or 0
