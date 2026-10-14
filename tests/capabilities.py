"""Probes for what the machine running the tests can do, and the skip markers
built on them. A test that needs a file name holding `?` asks whether this
machine can store one, not which operating system it runs on."""

from __future__ import annotations

import os
import tempfile


def stores_question_marks() -> bool:
    """A file name holding `?` can be created here."""
    with tempfile.TemporaryDirectory() as scratch:
        try:
            with open(os.path.join(scratch, "q?x"), "w"):
                pass
        except OSError:
            return False
        return True
