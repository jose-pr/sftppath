"""What importing and building paths needs, and what it never loads.

Each case runs in a fresh interpreter that blocks the import of the named
modules, so nothing is uninstalled and nothing connects."""

from __future__ import annotations

import pytest

from child import run

SSH_LIBRARIES = ["paramiko", "asyncssh"]


def test_importing_the_package_loads_neither_ssh_library_nor_uripath():
    result = run("""
        import sys
        import sftppath

        loaded = {"paramiko", "asyncssh", "uripath", "sftppath.uri"} & set(sys.modules)
        assert not loaded, loaded
        assert sftppath.SFTPPath and sftppath.BaseSFTPBackend
        print("ok")
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_building_and_deriving_a_path_with_a_given_backend_needs_no_ssh_library():
    result = run(
        """
        from sftppath import BaseSFTPBackend, SFTPPath

        class Backend(BaseSFTPBackend):
            def client(self):
                raise AssertionError("a pure operation asked for a client")

        backend = Backend()
        path = SFTPPath("/srv/data", backend=backend)
        derived = [path / "x", path.parent, path.with_name("y"), path.joinpath("a", "b")]
        assert all(p.backend is backend for p in derived)
        assert str(derived[0]) == "/srv/data/x"
        print("ok")
        """,
        SSH_LIBRARIES,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_without_either_ssh_library_the_endpoint_keywords_name_both_extras():
    result = run(
        """
        from sftppath import SFTPPath

        try:
            SFTPPath("/x", host="h")
        except ImportError as error:
            print(error)
        """,
        SSH_LIBRARIES,
    )
    assert result.returncode == 0, result.stderr
    assert "sftppath[asyncssh]" in result.stdout
    assert "sftppath[paramiko]" in result.stdout


def test_a_named_backend_that_is_not_installed_names_its_extra():
    for library in SSH_LIBRARIES:
        result = run(
            f"""
            import os
            os.environ["SFTPPATH_BACKEND"] = {library!r}
            from sftppath import SFTPPath

            try:
                SFTPPath("/x", host="h")
            except ImportError as error:
                print(error)
            """,
            [library],
        )
        assert result.returncode == 0, result.stderr
        assert f"sftppath[{library}]" in result.stdout


def test_the_uri_module_without_uripath_names_the_uri_extra():
    result = run(
        """
        try:
            import sftppath.uri
        except ImportError as error:
            print(error, error.name)
        """,
        ["uripath"],
    )
    assert result.returncode == 0, result.stderr
    assert 'pip install "sftppath[uri]"' in result.stdout
    assert result.stdout.split()[-1] == "uripath"


def test_importing_the_uri_module_loads_no_ssh_library():
    pytest.importorskip("uripath")
    result = run("""
        import sys
        import sftppath.uri

        loaded = {"paramiko", "asyncssh"} & set(sys.modules)
        assert not loaded, loaded
        print("ok")
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
