"""The public surface: what the package exports and the constructors as a type
checker reads them."""

from __future__ import annotations

import importlib
import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

import sftppath
from capabilities import exported

PACKAGE = pathlib.Path(sftppath.__file__).resolve().parent
HEADER = PACKAGE / "AGENTS.md"

# The names the shipped header documents, by the module they are imported from.
DOCUMENTED = {
    "sftppath": {
        "AsyncSFTPPath",
        "AsyncSSHBackend",
        "BaseSFTPBackend",
        "CheckFileSFTPBackend",
        "DEFAULT_PORT",
        "DEFAULT_SSH_CONFIG",
        "SFTPAuthenticationError",
        "SFTPBackend",
        "SFTPEndpoint",
        "SFTPHostKeyError",
        "SFTPPath",
        "SFTPPathError",
        "__version__",
    },
    "sftppath.exceptions": {
        "SFTPAuthenticationError",
        "SFTPHostKeyError",
        "SFTPPathError",
    },
}
URI_DOCUMENTED = {"sftppath.uri": {"SFTPURIBackend", "SFTPURIPath"}}


def _documented():
    found = dict(DOCUMENTED)
    if importlib.util.find_spec("uripath") is not None:
        found.update(URI_DOCUMENTED)
    return found


@pytest.mark.parametrize("module", sorted(_documented()))
def test_documented_names_stay_importable_from_where_they_are_documented(module):
    imported = importlib.import_module(module)
    missing = sorted(
        name for name in _documented()[module] if not exported(imported, name)
    )
    assert missing == []


@pytest.mark.parametrize("module", sorted(_documented()))
def test_a_public_module_exports_exactly_the_documented_names(module):
    exported = importlib.import_module(module).__all__
    assert set(exported) == _documented()[module]
    assert len(exported) == len(set(exported))


def test_the_lazily_loaded_names_are_exported():
    assert set(sftppath._LAZY) <= set(sftppath.__all__)
    for name in sftppath.__all__:
        assert exported(sftppath, name), name


def test_every_public_module_declares_all_and_the_others_are_private():
    import pkgutil

    public = sorted(
        info.name
        for info in pkgutil.walk_packages(sftppath.__path__, "sftppath.")
        if not any(part.startswith("_") for part in info.name.split(".")[1:])
    )
    assert public == ["sftppath.exceptions", "sftppath.uri"]
    for name in ["sftppath", *_documented()]:
        module = importlib.import_module(name)
        assert isinstance(module.__all__, (list, tuple)), name
        assert all(exported(module, member) for member in module.__all__), name


def _header_import_lines():
    text = HEADER.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    for block in blocks:
        for line in block.splitlines():
            if re.match(r"(from \S+ import |import )", line):
                yield line


def test_the_import_lines_of_the_header_run():
    for line in _header_import_lines():
        exec(line, {})


CONSUMER = """\
from sftppath import AsyncSSHBackend, BaseSFTPBackend, SFTPBackend, SFTPPath

backend: BaseSFTPBackend = SFTPBackend("h", user="u")
shared = SFTPPath("/srv/x", backend=backend)
own = SFTPPath("/srv/x", host="h", port=2222, user="u", password="p")
child = own / "y"
pure = own.parent.with_name("z")
"""


def _consumer(tmp_path):
    # Outside any dot-directory: pyright silently skips those.
    directory = tmp_path / "consumer"
    directory.mkdir()
    (directory / "consumer.py").write_text(CONSUMER, encoding="utf-8")
    return directory


def test_mypy_accepts_the_constructors(tmp_path, monkeypatch):
    api = pytest.importorskip("mypy.api")
    directory = _consumer(tmp_path)
    monkeypatch.setenv("MYPYPATH", str(PACKAGE.parent))
    out, err, status = api.run(
        [
            "--follow-imports=silent",
            "--no-incremental",
            "--cache-dir=" + str(tmp_path / "mypy-cache"),
            str(directory / "consumer.py"),
        ]
    )
    assert status == 0, out + err


@pytest.mark.allow_program("pyright", "basedpyright")
def test_pyright_accepts_the_constructors(tmp_path):
    checker = shutil.which("pyright") or shutil.which("basedpyright")
    if checker is None:
        pytest.skip("pyright is not installed")
    directory = _consumer(tmp_path)
    (directory / "pyrightconfig.json").write_text(
        json.dumps(
            {"typeCheckingMode": "standard", "extraPaths": [str(PACKAGE.parent)]}
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [checker, "--pythonpath", sys.executable, "consumer.py"],
        capture_output=True,
        text=True,
        cwd=directory,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_module_postpones_the_evaluation_of_annotations():
    # A bare `X | Y` in an annotation must not be evaluated on Python 3.9.
    without = sorted(
        str(path.relative_to(PACKAGE))
        for path in PACKAGE.rglob("*.py")
        if "from __future__ import annotations" not in path.read_text(encoding="utf-8")
    )
    assert without == []
