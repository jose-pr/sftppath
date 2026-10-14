# Divergences from `pathlib`

`sftppath` targets `pathlib.Path` parity: same method names, signatures,
semantics and exception types wherever a `pathlib.Path` equivalent exists.
Extensions (extra optional kwargs, new methods like `rm()` and `copy()`) are
allowed. Any *behavioral* divergence from `pathlib` on a method that exists in
both must be listed here -- no silent divergence.

The divergences below apply to `SFTPPath` and `SFTPURIPath`. The ones of the
generic `Path` operations that every path inherits (`touch()` on a non-local
backend, `rm()` and its policies, `chown()`, `Path.replace()`, a `with` block
that raised while writing) are on the documentation site of `pathapi`,
<https://jose-pr.github.io/pathapi/divergences/>.

| Method | `pathlib` | `SFTPPath` | Why |
| --- | --- | --- | --- |
| A relative path, and the empty path | relative to the current working directory | names are read from the server root: `SFTPPath("x", host=h)` is `/x`, the empty path is `/` | A tree served by a backend has no working directory, and two spellings of one name must be one file (`==`, `copy()` and `move()` ask). |
| `rename(target)` onto an existing file | POSIX `rename(2)`: replaces | Replaces through `posix-rename@openssh.com` where the server supports it; otherwise raises `FileExistsError`. | Plain SFTPv3 rename refuses an existing target with an uninformative failure. |
| `rename(str)` | a relative `str` is relative to the current working directory | a `str` is a sibling name (relative to `self.parent`); an absolute one is a path on the same server | There is no working directory; a sibling is what a rename within a directory means. |
| `replace(target)` onto an existing file | `os.replace` is one atomic call | One `posix-rename@openssh.com` request where the server has it (atomic; a refusal leaves both entries as they were). Without it the target is removed and then this entry renamed, so a failure between the two loses the target; a bare failure from the server is treated as the extension being absent. | Removing first left a window in which the target did not exist on every overwrite. |
| `chmod(mode, follow_symlinks=False)` | supported where the platform has `lchmod` | `NotImplementedError` on the paramiko backend; supported on asyncssh | paramiko has no call for it. |
| `hardlink_to(target)` | supported | `NotImplementedError` on the paramiko backend; supported on asyncssh, and only onto a path of the same server | SFTPv3 has no core hard-link operation. |
| `chown(uid, gid)` | not a `pathlib` method | numeric ids only; a partial change reads the other field first and raises `NotImplementedError` when the server names owners instead of numbering them | SFTPv3 carries the owner as one numeric pair. |
| `os.fspath()` of an `SFTPURIPath` | the local path | the path on the *server* (`.path`); `host_fspath()` says so | Meant for building a command line that runs on that host. `os.fspath()` of an `SFTPPath` is `NotImplementedError`. |
