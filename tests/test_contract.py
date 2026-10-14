"""The shipped `PathContract` suite against SFTP paths.

The suite runs once per SSH library (the `kind` fixture) and once per class of
path (the `flavor` fixture: an `SFTPPath`, and the `SFTPURIPath` of the `sftp:`
scheme where `uripath` is installed), against the in-process server of the
`sftp_server` fixture. A client library is independent of the library the
server uses.
"""

import pathlib

import pytest

from capabilities import stores_question_marks
from pathapi.testing import PathContract
from sftp_support import make_backend, make_path


class TestSFTPContract(PathContract):
    supports_question_mark_names = stores_question_marks()

    @pytest.fixture
    def root(self, kind, flavor, sftp_server, backends):
        # The host key is checked against the known_hosts file the fixture wrote.
        # A password is offered although the server accepts everyone: a client
        # with no agent, no key and no password does not try to log in at all.
        known_hosts = pathlib.Path.home() / ".ssh" / "known_hosts"
        backend = make_backend(kind, sftp_server, known_hosts=known_hosts)
        backends.append(backend)
        return make_path(flavor, sftp_server, backend)
