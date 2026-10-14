"""Files opened for writing that the program left open are written out when
the interpreter exits."""

from __future__ import annotations

import atexit as _atexit
import gc as _gc
import io as _io
import threading as _threading
import weakref as _weakref

#: Files `SFTPPath._open()` handed out for writing and nobody has closed.
_UNCLOSED: "_weakref.WeakSet" = _weakref.WeakSet()
_exit_hook_registered = False

#: Longest, in seconds, the exit hook waits for the unclosed files to be
#: written: a connection that has gone away must not hold the interpreter.
_EXIT_GRACE = 5.0


def _track_unclosed(file) -> None:
    global _exit_hook_registered
    try:
        _UNCLOSED.add(file)
    except TypeError:
        # A file that cannot be weakly referenced is not ours to flush.
        return
    if not _exit_hook_registered:
        _exit_hook_registered = True
        _atexit.register(_close_unclosed)


def _close_unclosed() -> None:
    """Write out the files left open at exit. It runs before the interpreter
    stops its threads, while the connections can still be used; at finalization
    it would be too late (the asyncssh loop no longer runs, and paramiko's
    transport has stopped)."""

    def close_all() -> None:
        for file in list(_UNCLOSED):
            # A text wrapper holds characters the file has not seen yet.
            for holder in _gc.get_referrers(file):
                if isinstance(holder, _io.TextIOWrapper) and holder.buffer is file:
                    try:
                        holder.flush()
                    except Exception:
                        pass
            try:
                file.close()
            except (ConnectionError, TimeoutError):
                return
            except Exception:
                pass

    worker = _threading.Thread(target=close_all, name="sftppath-exit", daemon=True)
    worker.start()
    worker.join(_EXIT_GRACE)
