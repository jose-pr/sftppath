"""The shipped `AsyncPathContract` suite against `AsyncSFTPPath`.

The suite wraps its sync `root` with `AsyncPath`; here that name is the native
class, so every call it makes goes through `AsyncSFTPPath`. The sync `root`
reaches the same in-process server, which is how each result is compared.
"""

import pathlib

import pytest

pytest.importorskip("asyncssh")
pytest.importorskip("pathapi.path._async", reason="pathapi has no AsyncPath")

import pathapi.testing._async as contract_module  # noqa: E402
import aio  # noqa: E402
from pathapi.testing import AsyncPathContract  # noqa: E402
from sftp_support import make_backend, make_path  # noqa: E402
from sftppath import AsyncSFTPPath  # noqa: E402


class TestAsyncSFTPContract(AsyncPathContract):
    @pytest.fixture(autouse=True)
    def _native(self, monkeypatch):
        monkeypatch.setattr(contract_module, "AsyncPath", AsyncSFTPPath)

    @pytest.fixture
    def root(self, sftp_server, backends):
        known_hosts = pathlib.Path.home() / ".ssh" / "known_hosts"
        backend = make_backend("asyncssh", sftp_server, known_hosts=known_hosts)
        backends.append(backend)
        return make_path("path", sftp_server, backend)

    def _run(self, coroutine):
        return aio.run(coroutine)
