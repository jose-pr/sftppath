"""The documents against the code: what the README and the API pages name
exists, every public name is documented, and no document tells the history of
the package (the changelog is where a change is recorded)."""

from __future__ import annotations

import importlib
import importlib.util
import pathlib
import re
import types

import pytest

import sftppath

REPO = pathlib.Path(__file__).resolve().parent.parent
HEADER = pathlib.Path(sftppath.__file__).resolve().parent / "AGENTS.md"
README = REPO / "README.md"
API = sorted((REPO / "docs" / "api").rglob("*.md"))
PUBLIC_MODULES = ["sftppath", "sftppath.exceptions", "sftppath.uri"]
HISTORY = re.compile(
    r"\b(previously|formerly|no longer|used to|as before|was changed|"
    r"now returns|now raises|renamed from)\b",
    re.IGNORECASE,
)


def _directives():
    """`{dotted name: has a members list}` of every `:::` line of the API pages."""
    found = {}
    for page in API:
        lines = page.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if line.startswith("::: "):
                block = lines[index + 1 : index + 12]
                restricted = any(item.strip().startswith("members:") for item in block)
                found[line[4:].strip()] = restricted
    return found


def _resolve(dotted):
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        try:
            found = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        for part in parts[cut:]:
            found = getattr(found, part)
        return found
    raise ImportError(dotted)


def _uri_available():
    return importlib.util.find_spec("uripath") is not None


def _modules():
    for module in PUBLIC_MODULES:
        if module == "sftppath.uri" and importlib.util.find_spec("uripath") is None:
            continue
        yield module


def test_every_object_an_api_page_renders_exists():
    for dotted in _directives():
        if (
            dotted.startswith("sftppath.uri")
            and importlib.util.find_spec("uripath") is None
        ):
            continue
        assert _resolve(dotted) is not None, dotted


@pytest.mark.parametrize("module", list(_modules()))
def test_every_public_name_is_on_an_api_page(module):
    directives = _directives()
    prose = "\n".join(page.read_text(encoding="utf-8") for page in API)
    listed = "\n".join(
        line.strip()
        for page in API
        for line in page.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("- ")
    )
    whole = module in directives and not directives[module]
    rendered = set()
    for dotted, restricted in directives.items():
        if restricted or (dotted == "sftppath.uri" and not _uri_available()):
            continue
        target = _resolve(dotted)
        if isinstance(target, types.ModuleType):
            rendered |= set(getattr(target, "__all__", ()))
    imported = importlib.import_module(module)
    missing = [
        name
        for name in imported.__all__
        if not whole
        and name not in rendered
        and f"{module}.{name}" not in directives
        and f"- {name}" not in listed
        and f"`{name}`" not in prose
        and name not in {"__version__"}
    ]
    assert missing == []


@pytest.mark.parametrize("module", list(_modules()))
def test_every_public_name_is_in_the_shipped_header(module):
    text = HEADER.read_text(encoding="utf-8")
    imported = importlib.import_module(module)
    missing = [
        name
        for name in imported.__all__
        if not re.search(rf"(?<![\w.]){re.escape(name)}(?!\w)", text)
    ]
    assert missing == []


def test_the_readme_names_the_public_names_of_the_root():
    text = README.read_text(encoding="utf-8")
    missing = [
        name
        for name in sftppath.__all__
        if name != "__version__" and not re.search(rf"`{re.escape(name)}`", text)
    ]
    assert missing == []


DOCUMENTS = [
    README,
    HEADER,
    REPO / "docs" / "index.md",
    *sorted((REPO / "docs" / "guides").glob("*.md")),
    *API,
]


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.name)
def test_a_document_describes_the_package_as_it_is(document):
    text = document.read_text(encoding="utf-8")
    found = [
        f"line {text.count(chr(10), 0, match.start()) + 1}: {match.group(0)}"
        for match in HISTORY.finditer(text)
    ]
    assert found == []


def test_the_history_check_reports_a_phrase():
    assert HISTORY.search("The name was previously Sftp.")
    assert HISTORY.search("A query is no longer a str.")
    assert not HISTORY.search("The previous content is kept.")


def test_every_example_block_that_is_run_parses():
    import ast

    for document in (README, REPO / "docs" / "index.md", *DOCUMENTS[3:]):
        text = document.read_text(encoding="utf-8")
        for block in re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL):
            ast.parse(block)
