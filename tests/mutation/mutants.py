"""The mutation list: one single-line change to a guard of the library per
entry, and the tests that must fail because of it.

`run.py` applies each entry to a temporary copy of `src/sftppath`. `old`
is the exact text of the line (or lines) to replace and must occur once in
`file`, which is relative to `src/sftppath`; when the code moves on and
the text goes, the entry is reported stale until it is re-expressed. `tests`
are pytest arguments (node ids, or a file when any of its tests may be the
one to catch the mutant) that all pass on the unmutated code. README.md says
how to add an entry.
"""

from __future__ import annotations

from typing import NamedTuple


class Mutant(NamedTuple):
    id: str
    file: str
    old: str
    new: str
    tests: tuple[str, ...]
    note: str
    platform: str | None = None


MUTANTS: list[Mutant] = []


def mutant(
    id: str,
    file: str,
    old: str,
    new: str,
    tests,
    note: str,
    platform: str | None = None,
) -> None:
    """Add an entry to the list. `tests` is one pytest argument or a sequence;
    `platform` is the `os.name` its tests need ("nt"), or None for any."""
    if isinstance(tests, str):
        tests = (tests,)
    MUTANTS.append(Mutant(id, file, old, new, tuple(tests), note, platform))


# --- the path: names, construction, text ------------------------------------------

mutant(
    "sftp-listing-keeps-unsafe-names",
    "_path.py",
    "            if not _providers.is_safe_child_name(name):\n",
    "            if False:\n",
    "tests/test_sftp.py::test_a_hostile_listing_name_never_becomes_a_child",
    "SFTPPath listing filter no-op",
)

mutant(
    "sftp-backend-and-endpoint-keywords-together",
    "_path.py",
    "        if backend is not None and endpoint:\n",
    "        if False:\n",
    "tests/test_sftp.py::test_backend_with_an_endpoint_keyword_is_a_type_error",
    "backend= together with host= is accepted and one of them is ignored",
)

mutant(
    "sftp-a-join-drops-the-backend",
    "_path.py",
    "        return type(self)(text, backend=self._backend)\n",
    "        return type(self)(text, backend=BaseSFTPBackend())\n",
    "tests/test_sftp.py::test_derived_paths_keep_the_very_same_backend",
    "a path derived from another is bound to a backend of its own",
)

mutant(
    "sftp-repr-shows-the-password",
    "_path.py",
    '            shown.append(f"host={backend.host!r}")\n',
    '            shown.append(f"host={backend.host!r}, password={backend.password!r}")\n',
    "tests/test_sftp.py::test_repr_names_the_endpoint_and_never_the_password",
    "repr() of a path prints the password",
)

mutant(
    "sftp-as-uri-shows-the-password",
    "_path.py",
    "            netloc = f\"{_urlparse.quote(backend.user, safe='')}@{netloc}\"\n",
    "            netloc = f\"{_urlparse.quote(backend.user, safe='')}:{backend.password}@{netloc}\"\n",
    "tests/test_sftp.py::test_as_uri_never_shows_the_password",
    "as_uri() carries the password",
)

mutant(
    "sftp-wire-path-is-not-rooted",
    "_path.py",
    '        return "/" + "/".join(name for name in self.segments if name)\n',
    '        return "/".join(name for name in self.segments if name)\n',
    "tests/test_sftp.py::test_chmod_follow_symlinks_true_delegates",
    "a request names the file without the leading slash",
)

mutant(
    "sftp-a-loopback-name-is-not-local",
    "_path.py",
    "        return address.is_loopback or address.is_unspecified\n",
    "        return False\n",
    "tests/test_sftp.py::test_is_local_is_true_for_a_loopback_address_and_localhost",
    "a path on the loopback interface is not local",
)

mutant(
    "sftp-rename-onto-another-server",
    "_path.py",
    "        elif not isinstance(target, SFTPPath) or not self._same_filesystem(target):\n",
    "        elif not isinstance(target, SFTPPath):\n",
    "tests/test_sftp.py::test_rename_onto_another_server_is_not_implemented",
    "rename() sends a path of another server to this one",
)

mutant(
    "sftp-copy-takes-the-native-walk-for-a-foreign-target",
    "_path.py",
    "            or not isinstance(target, SFTPPath)\n",
    "            or False\n",
    "tests/test_sftp.py::test_recursive_copy_to_another_class_or_server_is_not_the_backends_walk",
    "the concurrent walk writes a target that is not an SFTPPath on this server",
)

# --- the SSH libraries' guards ------------------------------------------------------

mutant(
    "paramiko-accepts-unknown-host-keys-by-default",
    "_paramiko.py",
    "            _paramiko.RejectPolicy() if hostkeypolicy is None else hostkeypolicy\n",
    "            _paramiko.AutoAddPolicy() if hostkeypolicy is None else hostkeypolicy\n",
    "tests/test_sftp_errors.py::test_an_unknown_host_key_is_a_host_key_error",
    "paramiko backend accepts unknown host keys by default",
)

mutant(
    "sftp-known-hosts-sequence-reads-the-first-file-only",
    "_paramiko.py",
    "            return [str(path) for path in known_hosts]\n",
    "            return [str(known_hosts[0])]\n",
    "tests/test_sftp_transport.py::test_paramiko_reads_every_known_hosts_file_of_a_sequence",
    "a list of known_hosts files is read only as far as its first",
)

mutant(
    "sftp-failed-client-keeps-its-transport",
    "_paramiko.py",
    "    except BaseException as error:\n        transport.close()\n        translated = _connect_error(error, backend.endpoint)",
    "    except BaseException as error:\n        translated = _connect_error(error, backend.endpoint)",
    "tests/test_sftp_transport.py::test_paramiko_closes_the_connection_when_the_sftp_subsystem_is_refused",
    "an SFTP client that fails to start leaves its connection open",
)

mutant(
    "sftp-failed-connect-keeps-its-proxy",
    "_paramiko.py",
    "                    sock.close()\n                except Exception:",
    "                    pass\n                except Exception:",
    "tests/test_sftp_transport.py::test_paramiko_closes_the_proxy_it_started_when_the_connection_fails",
    "a connection that fails leaves the ProxyCommand it started running",
)

mutant(
    "sftp-failed-connect-closes-the-callers-socket",
    "_paramiko.py",
    '            if sock is not None and "sock" not in self.connect_opts:',
    "            if sock is not None:",
    "tests/test_sftp_transport.py::test_paramiko_leaves_a_socket_the_caller_supplied_open_when_the_connection_fails",
    "a connection that fails closes a socket the caller supplied",
)

mutant(
    "asyncssh-timed-out-request-keeps-running",
    "_asyncssh/_loop.py",
    "        loop.call_soon_threadsafe(lambda: started[0].cancel())\n",
    "        pass\n",
    "tests/test_sftp_native_walk.py::test_walk_that_timed_out_has_stopped",
    "a request that timed out keeps running on the bridge loop",
)

mutant(
    "asyncssh-failed-copy-keeps-the-partial-file",
    "_asyncssh/_walk.py",
    "                    await quietly(lambda: aclient.remove(dst._wirepath), dst._wirepath)\n                    raise\n",
    "                    pass\n                    raise\n",
    "tests/test_sftp_native_walk.py::test_a_copy_that_fails_while_reading_leaves_no_file_behind",
    "a copy that fails leaves the half-written destination",
)

mutant(
    "sftp-asyncssh-failed-read-of-a-directory-stays-bare",
    "_asyncssh/_file.py",
    "                and translated.errno is None\n",
    "                and False\n",
    "tests/test_sftp_directory_read.py::test_a_sized_read_of_a_directory_is_a_directory_error",
    "a read of a directory OpenSSH opened raises a bare OSError('Failure') on asyncssh",
)

mutant(
    "sftp-asyncssh-empty-read-of-a-directory-is-empty",
    "_asyncssh/_file.py",
    "        if size < 0 and not data and self._is_directory():\n",
    "        if False:\n",
    "tests/test_sftp_directory_read.py::test_read_bytes_of_a_directory_is_a_directory_error",
    "read_bytes() of a directory Windows OpenSSH opened returns b'' on asyncssh",
)

mutant(
    "sftp-paramiko-failed-read-of-a-directory-stays-bare",
    "_paramiko.py",
    "            if error.errno is None and self._is_directory():\n",
    "            if False:\n",
    "tests/test_sftp_directory_read.py::test_read_bytes_of_a_directory_is_a_directory_error",
    "a read of a directory OpenSSH opened raises a bare OSError('Failure') on paramiko",
)

mutant(
    "sftp-paramiko-empty-read-of-a-directory-is-empty",
    "_paramiko.py",
    "        if (size is None or size < 0) and not data and self._is_directory():\n",
    "        if False:\n",
    "tests/test_sftp_directory_read.py::test_a_directory_whose_reads_answer_eof_is_a_directory_error",
    "read_bytes() of a directory whose reads answer EOF returns b'' on paramiko",
)

# --- check-file ---------------------------------------------------------------------

mutant(
    "checkfile-reply-names-another-algorithm",
    "_checkfile.py",
    "    if reply_algorithm != algorithm:\n",
    "    if False:\n",
    "tests/test_sftp.py::test_check_file_reply_naming_another_algorithm_is_refused_at_the_requested_size",
    "check-file reply naming another algorithm is trusted",
)

mutant(
    "checkfile-digest-of-the-wrong-length",
    "_checkfile.py",
    "    if len(digest) != expected:\n",
    "    if False:\n",
    "tests/test_sftp.py::test_check_file_reply_parser_rejects_malformed_replies",
    "a check-file reply with a digest of the wrong length is trusted",
)

mutant(
    "sftp-check-file-asks-for-another-extension",
    "_paramiko.py",
    "            _checkfile.EXTENSION,\n            file.handle,\n",
    '            "check-file@openssh.com",\n            file.handle,\n',
    "tests/test_sync_sftp_parity.py::test_checksum_is_the_server_side_digest",
    "the paramiko backend asks for an extension OpenSSH does not have",
)

mutant(
    "sftp-check-file-asks-for-two-algorithms",
    "_paramiko.py",
    "            algorithm,  # hash-algorithm-list: just the one wanted\n",
    '            algorithm + ",md5",  # hash-algorithm-list: just the one wanted\n',
    "tests/test_sync_sftp_parity.py::test_checksum_is_the_server_side_digest",
    "the request names more algorithms than the one wanted",
)

mutant(
    "sftp-check-file-refusal-is-not-remembered",
    "_checkfile.py",
    "                if refused(error):\n                    record.refused.add(algorithm)\n",
    "                if False:\n                    record.refused.add(algorithm)\n",
    "tests/test_sync_sftp_parity.py::test_checksum_refusal_costs_one_request_per_connection",
    "a server without the extension is asked again for every file",
)

mutant(
    "sftp-check-file-leaks-its-handle",
    "_checkfile.py",
    "        finally:\n            file.close()\n        digest = parse_reply(payload, algorithm)",
    "        finally:\n            pass\n        digest = parse_reply(payload, algorithm)",
    (
        "tests/test_sftp.py::test_paramiko_checksum_closes_handle_even_when_request_raises",
        "tests/test_sftp.py::test_paramiko_checksum_sends_correct_extended_request",
    ),
    "a check-file request leaves the handle it opened, whether or not it succeeds",
)

# --- the sftp: scheme ---------------------------------------------------------------

mutant(
    "sftp-uri-backend-is-built-for-every-request",
    "uri.py",
    "            found = self._backends.get(key)\n",
    "            found = None\n",
    "tests/test_uri_scheme.py::test_backend_for_gives_one_backend_per_server_and_account_with_the_options_given",
    "paths to one server do not share a backend, so each opens a connection",
)

mutant(
    "sftp-uri-copy-takes-the-native-walk-for-another-host",
    "uri.py",
    "            or not self._same_location(target)\n            or not self._same_filesystem(target)\n",
    "            or False\n            or not self._same_filesystem(target)\n",
    "tests/test_uri_scheme.py::test_the_walk_is_not_used_for_a_foreign_target_or_a_second_supplied_backend",
    "a directory copied to another host takes the walk that writes over this connection",
)

mutant(
    "sftp-uri-remove-callback-gets-the-core-path",
    "uri.py",
    "                    return ignore_error(error, self._from_core(path))\n",
    "                    return ignore_error(error, path)\n",
    (
        "tests/test_sftp_native_walk.py::test_error_the_handler_declined_is_offered_once",
        "tests/test_sftp_native_walk.py::test_errors_the_handler_swallows_are_offered_one_by_one",
    ),
    "the handler of rm() receives the path of the core class and not the URI path",
)

mutant(
    "sftp-path-needs-an-ssh-library-to-be-built",
    "_path.py",
    "            return _MissingBackend(host, port, user, str(error))\n",
    "            raise\n",
    "tests/test_isolation.py::test_without_either_ssh_library_names_are_listed_and_a_request_names_both_extras",
    "a path named with no SSH library installed fails when it is built, not when it is used",
)

mutant(
    "sftp-missing-library-of-a-backend-name-is-not-the-extra",
    "__init__.py",
    '        if (error.name or "").partition(".")[0] != library:\n',
    "        if True:\n",
    "tests/test_isolation.py::test_without_either_ssh_library_names_are_listed_and_a_request_names_both_extras",
    "naming a backend whose library is missing raises ModuleNotFoundError, not the ImportError that names the extra",
)

mutant(
    "sftp-path-rm-always-passes-max-workers",
    "_path.py",
    '                follow_binds=follow_binds,\n                # Only when given: pathapi 0.1.1 has no such keyword.\n                **({} if max_workers is None else {"max_workers": max_workers}),\n',
    "                follow_binds=follow_binds,\n                max_workers=max_workers,\n",
    "tests/test_sftp.py::test_the_generic_walks_get_max_workers_only_when_it_is_given",
    "rm() hands pathapi a max_workers it was not given, which a release without the keyword refuses",
)

mutant(
    "sftp-path-copy-always-passes-max-workers",
    "_path.py",
    '                **extra,\n                **({} if max_workers is None else {"max_workers": max_workers}),\n',
    "                **extra,\n                max_workers=max_workers,\n",
    "tests/test_sftp.py::test_the_generic_walks_get_max_workers_only_when_it_is_given",
    "copy() hands pathapi a max_workers it was not given, which a release without the keyword refuses",
)

mutant(
    "sftp-uri-rm-always-passes-max-workers",
    "uri.py",
    '            follow_binds=follow_binds,\n            # Only when given: pathapi 0.1.1 has no such keyword.\n            **({} if max_workers is None else {"max_workers": max_workers}),\n',
    "            follow_binds=follow_binds,\n            max_workers=max_workers,\n",
    "tests/test_uri_scheme.py::test_the_generic_walks_get_max_workers_only_when_it_is_given",
    "the URI rm() hands pathapi a max_workers it was not given",
)

mutant(
    "sftp-uri-copy-always-passes-max-workers",
    "uri.py",
    '                **({} if offering is None else {"on_error": offering}),\n                **({} if max_workers is None else {"max_workers": max_workers}),\n',
    '                **({} if offering is None else {"on_error": offering}),\n                max_workers=max_workers,\n',
    "tests/test_uri_scheme.py::test_the_generic_walks_get_max_workers_only_when_it_is_given",
    "the URI copy() hands pathapi a max_workers it was not given",
)

# --- AsyncSFTPPath ---------------------------------------------------------------

mutant(
    "sftp-async-listing-keeps-unsafe-names",
    "_async.py",
    "            if is_safe_child_name(name):\n                yield self._child(name)\n",
    "            if True:\n                yield self._child(name)\n",
    "tests/test_async_sftp.py::test_a_hostile_listing_name_never_becomes_a_child",
    "AsyncSFTPPath.iterdir() yields a name the server chose that is not one component",
)

mutant(
    "sftp-async-walk-keeps-unsafe-names",
    "_async.py",
    "                if not is_safe_child_name(name):\n                    if on_error is not None:\n",
    "                if False:\n                    if on_error is not None:\n",
    "tests/test_async_sftp.py::test_a_hostile_listing_name_never_becomes_a_child",
    "AsyncSFTPPath.walk() descends into a hostile listed name",
)

mutant(
    "sftp-async-walk-trusts-a-typeless-listing",
    "_async.py",
    "                    if listed is None or follow_symlinks:\n",
    "                    if follow_symlinks:\n",
    "tests/test_async_sftp.py::test_a_listed_entry_that_does_not_say_what_it_is_is_asked_about",
    "AsyncSFTPPath.walk() takes an entry whose listing carries no type for a file",
)

mutant(
    "sftp-async-exclusive-open-hides-the-existing-file",
    "_async.py",
    '                "x" in mode\n                and not self._path._unreachable(error)\n',
    "                False\n                and not self._path._unreachable(error)\n",
    "tests/test_async_sftp.py::test_an_exclusive_open_of_an_existing_file_is_a_file_exists_error",
    "AsyncSFTPPath.open('x') of an existing file raises the bare failure of SFTPv3",
)

mutant(
    "sftp-async-unlink-of-a-directory-keeps-the-bare-failure",
    "_async.py",
    "        if found is None or not found.is_dir():\n            return None\n        return IsADirectoryError(",
    "        if True:\n            return None\n        return IsADirectoryError(",
    "tests/test_async_sftp.py::test_a_bare_failure_is_told_apart_by_asking_about_the_entry",
    "AsyncSFTPPath.unlink() of a directory is not IsADirectoryError",
)

mutant(
    "sftp-async-rename-onto-another-server",
    "_async.py",
    "            if not isinstance(target, SFTPPath) or not sync._same_filesystem(target):\n",
    "            if not isinstance(target, SFTPPath):\n",
    "tests/test_async_sftp.py::test_rename_replaces_a_file_and_refuses_another_server",
    "AsyncSFTPPath.rename() sends another server's path to this one",
)

mutant(
    "sftp-async-text-file-skips-its-last-write",
    "_asyncssh/_aiofile.py",
    '            if self._kind == "w":\n                await self._drain()\n        finally:\n',
    "            if False:\n                await self._drain()\n        finally:\n",
    "tests/test_async_sftp.py::test_a_text_file_sends_nothing_until_it_is_flushed_or_closed",
    "an AsyncSFTPPath text file closed with text still buffered loses it",
)

mutant(
    "sftp-async-close-stops-when-cancelled",
    "_asyncssh/_aiofile.py",
    "            await _complete(self._finish())\n",
    "            await self._finish()\n",
    "tests/test_async_sftp.py::test_a_cancelled_close_is_complete_before_the_cancellation_arrives",
    "a cancelled AsyncSFTPPath file close leaves the server's handle open",
)

mutant(
    "sftp-async-dead-connection-is-reused",
    "_asyncssh/_session.py",
    "        entry = slot.entry\n        if entry is not None and _entry_is_alive(entry):\n            return _Link(entry.client._aclient, entry.client)\n        if slot.lock is None:\n",
    "        entry = slot.entry\n        if entry is not None:\n            return _Link(entry.client._aclient, entry.client)\n        if slot.lock is None:\n",
    "tests/test_async_sftp.py::test_a_connection_that_dropped_is_replaced",
    "AsyncSFTPPath keeps using a connection the server closed",
)
