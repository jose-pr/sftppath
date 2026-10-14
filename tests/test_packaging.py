"""The packaging manifest: dependency bounds and what the distributions leave out."""

from __future__ import annotations

import pathlib

import pytest

packaging_requirements = pytest.importorskip("packaging.requirements")

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = pytest.importorskip("tomli")

PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


@pytest.fixture(scope="module")
def project():
    if not PYPROJECT.is_file():
        pytest.skip("pyproject.toml is not part of an installed copy")
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _declared_requirements(project):
    specs = list(project["project"].get("dependencies", []))
    for extra, deps in project["project"]["optional-dependencies"].items():
        if extra not in ("dev", "docs"):
            specs += deps
    for spec in specs:
        requirement = packaging_requirements.Requirement(spec)
        # The extras of this distribution name itself, with no bounds.
        if requirement.name.replace("_", "-") not in ("pathlib-next", "sftppath"):
            yield requirement


def _bounds(requirement):
    operators = {s.operator for s in requirement.specifier}
    return (
        bool(operators & {">=", ">", "==", "~="}),
        bool(operators & {"<", "<=", "==", "~="}),
    )


def test_every_dependency_declares_a_floor(project):
    unbounded = [str(r) for r in _declared_requirements(project) if not _bounds(r)[0]]
    assert unbounded == []


def test_every_dependency_declares_a_ceiling(project):
    unbounded = [str(r) for r in _declared_requirements(project) if not _bounds(r)[1]]
    assert unbounded == []


def test_distributions_leave_out_the_contributor_notes_and_agent_config(project):
    targets = project["tool"]["hatch"]["build"]["targets"]
    sdist = targets["sdist"]["exclude"]
    wheel = targets["wheel"]["exclude"]
    assert "/AGENTS.md" in sdist
    assert "CLAUDE*" in sdist
    assert "CLAUDE*" in wheel


def test_the_extras_are_the_two_libraries_the_scheme_and_their_union(project):
    metadata = project["project"]
    assert "scripts" not in metadata
    extras = metadata["optional-dependencies"]
    assert set(extras) == {"paramiko", "asyncssh", "uri", "all", "dev", "docs"}
    names = {
        extra: {packaging_requirements.Requirement(spec).name for spec in extras[extra]}
        for extra in ("paramiko", "asyncssh", "uri", "all")
    }
    assert names == {
        "paramiko": {"paramiko"},
        "asyncssh": {"asyncssh"},
        "uri": {"uripath"},
        "all": {"sftppath"},
    }
    assert extras["all"] == ["sftppath[paramiko,asyncssh,uri]"]


def test_the_only_required_dependency_is_the_core(project):
    required = [
        packaging_requirements.Requirement(spec)
        for spec in project["project"]["dependencies"]
    ]
    assert [r.name for r in required] == ["pathlib-next"]


def test_the_scheme_is_declared_unconditionally_as_an_entry_point(project):
    entry_points = project["project"]["entry-points"]
    assert entry_points == {"uripath.schemes": {"sftp": "sftppath.uri:SFTPURIPath"}}
