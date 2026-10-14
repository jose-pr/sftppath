"""Probes for what the machine running the tests can do, and the skip markers
built on them. A test that needs a file name holding `?` asks whether this
machine can store one, not which operating system it runs on. The same goes
for the SSH libraries: a test that lists names does not need them installed."""

from __future__ import annotations

import importlib.util
import os
import tempfile

import sftppath


def stores_question_marks() -> bool:
    """A file name holding `?` can be created here."""
    with tempfile.TemporaryDirectory() as scratch:
        try:
            with open(os.path.join(scratch, "q?x"), "w"):
                pass
        except OSError:
            return False
        return True


#: Each lazily exported backend of `sftppath` and the library it needs.
LIBRARY_OF = {name: library for name, (_module, library) in sftppath._LAZY.items()}


def installed(library: str) -> bool:
    return importlib.util.find_spec(library) is not None


def library_missing(name: str) -> bool:
    """`name` is a backend whose library is not installed."""
    library = LIBRARY_OF.get(name)
    return library is not None and not installed(library)


def exported(module, name: str) -> bool:
    """`module` offers `name`. A backend whose library is missing is offered
    when it is listed and using it raises an `ImportError` that names its
    extra."""
    try:
        return hasattr(module, name)
    except ImportError as error:
        library = LIBRARY_OF.get(name)
        return (
            library is not None
            and name in dir(module)
            and f'pip install "sftppath[{library}]"' in str(error)
        )
