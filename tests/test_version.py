"""`sftppath.__version__` and the places that name it."""

from __future__ import annotations

import importlib.metadata
import re

import sftppath


def test_the_version_is_the_installed_distributions():
    assert sftppath.__version__ == importlib.metadata.version("sftppath")
    assert re.fullmatch(r"\d+(\.\d+)*([a-z]+\d+)?", sftppath.__version__)
    assert "__version__" in sftppath.__all__


def test_the_version_is_read_from_metadata_and_not_written_in_the_source():
    import pathlib

    source = (pathlib.Path(sftppath.__file__).parent / "_version.py").read_text(
        encoding="utf-8"
    )
    assert "importlib" in source or "metadata" in source
    assert sftppath.__version__ not in source
