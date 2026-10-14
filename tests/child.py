"""Run a snippet in a fresh interpreter in which chosen modules cannot be
imported (`sys.modules[name] = None`), so nothing is uninstalled and no socket
is opened: the child raises on any connect or name lookup."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap

import sftppath

_PRELUDE = """
import socket, sys

def _refuse(*args, **kwargs):
    raise RuntimeError("network used")

socket.socket.connect = _refuse
socket.getaddrinfo = _refuse
for name in {blocked!r}:
    sys.modules[name] = None
"""

#: The tree `sftppath` was imported from: the checkout under test (or the copy
#: of it the mutation list tests), not whichever copy is installed.
SOURCE = pathlib.Path(sftppath.__file__).resolve().parent.parent


def run(code: str, blocked=()) -> subprocess.CompletedProcess:
    script = _PRELUDE.format(blocked=tuple(blocked)) + textwrap.dedent(code)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(SOURCE), env.get("PYTHONPATH")])
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
