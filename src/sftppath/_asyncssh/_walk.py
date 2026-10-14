"""The native recursive copy and remove: concurrent requests on one connection."""

from __future__ import annotations

import asyncio as _asyncio
import errno as _errno
import functools as _functools
import stat as _stat

import asyncssh as _asyncssh
from pathlib_next import FileStat
from pathlib_next import providers as _providers

from ._attrs import _StatAdapter
from ._client import _aopen
from ._translate import _library_error, _path_error


class _TaskOwner:
    """Owns every task one recursive walk creates, so that none outlives it:
    `close()` cancels the ones still running and waits until they have all
    finished."""

    __slots__ = ("_tasks",)

    def __init__(self) -> None:
        self._tasks: "set[_asyncio.Task]" = set()

    def spawn(self, coro) -> "_asyncio.Task":
        task = _asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: "_asyncio.Task") -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            # A failure nobody awaited (a second one, behind the first that
            # was raised) is read here, or asyncio logs it as never retrieved.
            task.exception()

    async def close(self) -> None:
        pending = list(self._tasks)
        for task in pending:
            # Once: a task already unwinding from a cancel must not be cut
            # short in the cleanup requests it makes.
            task.cancel()
        interrupted = None
        while pending:
            try:
                await _asyncio.wait(pending)
            except _asyncio.CancelledError as error:
                interrupted = error
            pending = [task for task in pending if not task.done()]
        if interrupted is not None:
            raise interrupted


async def _in_thread(function, *args):
    """`asyncio.to_thread()` for a worker the caller cannot abandon: a thread
    cannot be interrupted, so a cancelled caller waits for it to return
    before it unwinds."""
    work = _asyncio.ensure_future(_asyncio.to_thread(function, *args))
    try:
        return await _asyncio.shield(work)
    except _asyncio.CancelledError:
        await _asyncio.wait([work])
        if not work.cancelled():
            work.exception()
        raise


async def _sftp_call(semaphore, make_awaitable, filename=None):
    """Run one asyncssh request inside `semaphore`, raising what the sync
    client raises for a failure of it."""
    async with semaphore:
        try:
            return await make_awaitable()
        except _asyncssh.Error as error:
            raise _library_error(error, filename) from None


async def _read_children(aclient, semaphore, current):
    """`[(child path, listed FileStat or None)]` for the entries of the
    directory `current`, in the server's order. The listing's own attributes
    come with it (they describe the entry, never what a link points at); None
    when the server did not say what kind of entry it is."""
    names = await _sftp_call(
        semaphore, lambda: aclient.readdir(current._wirepath), current._wirepath
    )
    children = []
    for entry in names:
        # The server chooses these names: one that is not a single component
        # inside `current` must never become a child path (see
        # `SFTPPath._scandir`). The walkers bypass `_scandir`, so they filter.
        if not _providers.is_safe_child_name(entry.filename):
            continue
        attrs = getattr(entry, "attrs", None)
        listed = None
        if attrs is not None:
            listed = FileStat.from_stat(_StatAdapter(attrs))
            if not _stat.S_IFMT(listed.st_mode):
                listed = None
        children.append((current / entry.filename, listed))
    return children


async def _wait_fail_fast(tasks):
    """Wait for `tasks`, raising the first failure as soon as it happens. The
    other tasks are left to the `_TaskOwner`, which cancels them."""
    pending = set(tasks)
    while pending:
        done, pending = await _asyncio.wait(
            pending, return_when=_asyncio.FIRST_EXCEPTION
        )
        for task in done:
            task.result()


async def _concurrent_copy(
    path,
    target,
    overwrite: bool,
    follow_symlinks: bool,
    preserve_metadata: bool,
    max_concurrency: int,
    ignore_error,
    aclient=None,
    on_error=None,
):
    """Concurrent recursive child copies, bounded by max_concurrency.

    `aclient` must be resolved on the calling thread (`SFTPPath.copy` does):
    looking it up here, on the bridge loop, would open the connection
    through a blocking `_run()` on the loop thread itself. `on_error` runs in
    a worker thread, once per failing child, as `(error, source child)`; a
    child it declines fails the copy as soon as it does, as with no policy.

    Every task the walk creates belongs to one `_TaskOwner`, which cancels
    and awaits the ones still running before this coroutine returns or
    raises: a copy that has raised has stopped, and a destination file whose
    copy did not complete is removed. A source is opened before the
    destination it replaces is touched, and modes are applied as
    `Path.copy()` applies them (see `_copy_mode` in `pathlib_next`'s `Path`).
    """
    semaphore = _asyncio.Semaphore(max(1, max_concurrency))
    # Separate from `semaphore`, which every request in copy_file takes: holding one
    # of its slots for a whole file deadlocks once max_concurrency files are open.
    # This caps files open at once (two remote handles each).
    file_semaphore = _asyncio.Semaphore(max(1, max_concurrency))
    owner = _TaskOwner()
    # The setuid, setgid and sticky bits are applied only between two paths of
    # one class; a mode read from another class must not make a privileged file.
    keep_special_bits = type(path) is type(target)
    if aclient is None:
        aclient = path._sftpclient._aclient

    def sftp_call(make_awaitable, filename=None):
        return _sftp_call(semaphore, make_awaitable, filename)

    async def stat_path(current):
        stat_coro = aclient.stat if follow_symlinks else aclient.lstat
        attrs = await sftp_call(lambda: stat_coro(current._wirepath), current._wirepath)
        return FileStat.from_stat(_StatAdapter(attrs))

    async def exists_stat(current):
        try:
            return await stat_path(current)
        except FileNotFoundError:
            return None

    async def mkdir(current):
        # The mode `Path.mkdir()` sends; the source's is applied afterwards.
        attrs = _asyncssh.SFTPAttrs(permissions=0o777)
        await sftp_call(
            lambda: aclient.mkdir(current._wirepath, attrs), current._wirepath
        )

    async def unlink(current):
        await sftp_call(lambda: aclient.remove(current._wirepath), current._wirepath)

    async def apply_mode(current, source_stat):
        """Give `current` the permission bits `source_stat` reports, when it
        reports any (`Path.copy()`'s `_copy_mode`)."""
        if not (preserve_metadata and source_stat.mode_known and source_stat.st_mode):
            return
        mode = _stat.S_IMODE(source_stat.st_mode)
        if not keep_special_bits:
            mode &= ~(_stat.S_ISUID | _stat.S_ISGID | _stat.S_ISVTX)
        try:
            await sftp_call(
                lambda: aclient.chmod(current._wirepath, mode), current._wirepath
            )
        except NotImplementedError:
            pass

    async def quietly(make_awaitable, filename):
        # Cleanup of a copy that is already failing: its own failure is not
        # worth reporting over the one being raised.
        try:
            await sftp_call(make_awaitable, filename)
        except Exception:
            pass

    async def open_file(file, mode):
        """Open `file`. A cancellation that arrives with the request on the
        wire lets it finish and then undoes it (closes the handle, removes a
        file the open created): the request cannot be recalled, and an
        abandoned one leaves a handle and a zero-length file on the server."""
        request = _asyncio.ensure_future(
            sftp_call(lambda: _aopen(aclient, file._wirepath, mode), file._wirepath)
        )
        try:
            return await _asyncio.shield(request)
        except _asyncio.CancelledError:
            await _asyncio.wait([request])
            if not request.cancelled() and request.exception() is None:
                handle = request.result()
                await quietly(lambda: handle.close(), file._wirepath)
                if "w" in mode:
                    await quietly(
                        lambda: aclient.remove(file._wirepath), file._wirepath
                    )
            raise

    async def copy_file(src, dst):
        existing = await exists_stat(dst)
        if existing is not None:
            if existing.is_dir():
                raise _path_error(IsADirectoryError, _errno.EISDIR, dst)
            if not overwrite:
                raise _path_error(FileExistsError, _errno.EEXIST, dst)

        async with file_semaphore:
            # Open the source before the destination is touched at all: a
            # source that cannot be read leaves an existing destination intact.
            src_file = await open_file(src, "rb")
            try:
                if existing is not None:
                    try:
                        await unlink(dst)
                    except FileNotFoundError:
                        pass
                dst_file = await open_file(dst, "wb")
                try:
                    while True:
                        chunk = await sftp_call(
                            lambda: src_file.read(1024 * 1024), src._wirepath
                        )
                        if not chunk:
                            break
                        await sftp_call(
                            lambda chunk=chunk: dst_file.write(chunk), dst._wirepath
                        )
                    await sftp_call(lambda: dst_file.close(), dst._wirepath)
                except BaseException:
                    # A half-written file is not a copy of anything. Closed
                    # first: a server may refuse to remove an open file.
                    await quietly(lambda: dst_file.close(), dst._wirepath)
                    await quietly(lambda: aclient.remove(dst._wirepath), dst._wirepath)
                    raise
            finally:
                # A read handle: nothing is lost if its close fails.
                await quietly(lambda: src_file.close(), src._wirepath)

    async def copy_with_sync_fallback(src, dst):
        async with semaphore:
            await _in_thread(
                _functools.partial(
                    src.copy,
                    dst,
                    overwrite=overwrite,
                    follow_symlinks=follow_symlinks,
                    preserve_metadata=preserve_metadata,
                    recursive=True,
                    ignore_error=ignore_error,
                    **({} if on_error is None else {"on_error": on_error}),
                )
            )

    async def copy_node(src, dst, listed=None):
        # A listed entry that is not a link says what it is; a link is
        # followed (or not) by the request that stats it.
        if listed is not None and not (follow_symlinks and listed.is_symlink()):
            src_stat = listed
        else:
            src_stat = await stat_path(src)
        if src_stat.is_symlink():
            await copy_with_sync_fallback(src, dst)
            return

        if src_stat.is_dir():
            existing = await exists_stat(dst)
            if existing is not None:
                if not existing.is_dir():
                    raise _path_error(FileExistsError, _errno.EEXIST, dst)
                if not overwrite:
                    raise _path_error(FileExistsError, _errno.EEXIST, dst)
            else:
                await mkdir(dst)

            await settle(
                [
                    owner.spawn(guarded(child, dst / child.name, child_stat))
                    for child, child_stat in await _read_children(
                        aclient, semaphore, src
                    )
                ]
            )
        elif src_stat.is_file():
            await copy_file(src, dst)
        else:
            await copy_with_sync_fallback(src, dst)
            return

        await apply_mode(dst, src_stat)

    async def guarded(child, dst, listed):
        # `on_error` is asked about each failing child where it fails: a declined error
        # stops the copy at once, a swallowed one ends only that child. It is user code,
        # run off the loop thread, so it may call sync Path methods (via _run()).
        try:
            await copy_node(child, dst, listed)
        except Exception as error:
            if on_error is None or not await _in_thread(on_error, error, child):
                raise

    async def settle(tasks):
        # Path.copy()'s contract: a callable is notified and the error
        # suppressed, True suppresses, False/None fail fast. A failure that
        # is raised leaves the other tasks to the owner, which cancels them.
        if not tasks:
            return
        if ignore_error:
            await _asyncio.wait(tasks)
            for task in tasks:
                try:
                    task.result()
                except Exception as error:
                    if callable(ignore_error):
                        # User code: off the loop thread, so it may call sync
                        # Path methods (which _run() back onto this loop).
                        await _in_thread(ignore_error, error)
            return
        await _wait_fail_fast(tasks)

    try:
        await settle(
            [
                owner.spawn(guarded(child, target / child.name, child_stat))
                for child, child_stat in await _read_children(aclient, semaphore, path)
            ]
        )
        # The root last, after what is in it, like every other directory.
        await apply_mode(target, await stat_path(path))
    finally:
        await owner.close()


async def _concurrent_rm(
    path,
    *,
    max_concurrency: int,
    missing_ok: bool,
    on_error,
    aclient=None,
):
    """Native asyncssh recursive remove, bounded by max_concurrency.

    `aclient` must be resolved on the calling thread (`SFTPPath.rm` does):
    see `_concurrent_copy`, which also explains the `_TaskOwner`. `on_error`
    runs in a worker thread, so it may call sync `Path` methods. Only the
    root is stat-ed: every entry below it is a file or a directory by what
    its parent's listing said.
    """
    semaphore = _asyncio.Semaphore(max(1, max_concurrency))
    owner = _TaskOwner()
    # Errors `on_error` declined, on their way up through the enclosing
    # directories: each error is offered once.
    declined: "list[Exception]" = []
    if aclient is None:
        aclient = path._sftpclient._aclient

    def sftp_call(make_awaitable, filename=None):
        return _sftp_call(semaphore, make_awaitable, filename)

    async def handled(error, current) -> bool:
        if on_error is None or any(error is seen for seen in declined):
            return False
        try:
            swallow = await _in_thread(on_error, error, current)
        except BaseException as raised:
            # Its own exception is not offered back by the directories above.
            declined.append(raised)
            raise
        if swallow:
            return True
        declined.append(error)
        return False

    async def stat_path(current):
        attrs = await sftp_call(
            lambda: aclient.lstat(current._wirepath), current._wirepath
        )
        return FileStat.from_stat(_StatAdapter(attrs))

    async def remove_file(current):
        await sftp_call(lambda: aclient.remove(current._wirepath), current._wirepath)

    async def remove_dir(current):
        await sftp_call(lambda: aclient.rmdir(current._wirepath), current._wirepath)

    async def rm_one(current, listed=None, *, root: bool = False):
        try:
            try:
                stat = listed if listed is not None else await stat_path(current)
            except FileNotFoundError:
                # `missing_ok` is about the path rm() was called on, not about
                # an entry that vanished below it.
                if root and missing_ok:
                    return
                raise
            if stat.is_dir():
                tasks = [
                    owner.spawn(rm_one(child, child_stat))
                    for child, child_stat in await _read_children(
                        aclient, semaphore, current
                    )
                ]
                if tasks:
                    await _wait_fail_fast(tasks)
                await remove_dir(current)
            else:
                await remove_file(current)
        except Exception as error:
            if not await handled(error, current):
                raise

    try:
        await rm_one(path, root=True)
    finally:
        await owner.close()
