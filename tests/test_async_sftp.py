"""`AsyncSFTPPath`: the methods that run on the caller's loop, the connection
they share, its construction and its errors, against the in-process server of
the `sftp_server` fixture. The shared contract is in `test_async_contract.py`.
"""

import asyncio
import errno
import pathlib
import threading
import time

import pytest

pytest.importorskip("asyncssh")
pytest.importorskip("pathapi.path._async", reason="pathapi has no AsyncPath")

import aio  # noqa: E402
import child  # noqa: E402
import sftppath  # noqa: E402
from sftp_support import make_backend, make_path  # noqa: E402
from sftppath import AsyncSFTPPath, SFTPPath  # noqa: E402
from sftppath._asyncssh import _session  # noqa: E402


@pytest.fixture
def sync_root(sftp_server, backends):
    """A sync path at the server's root, over an asyncssh backend."""
    known_hosts = pathlib.Path.home() / ".ssh" / "known_hosts"
    backend = make_backend("asyncssh", sftp_server, known_hosts=known_hosts)
    backends.append(backend)
    return make_path("path", sftp_server, backend)


@pytest.fixture
def root(sync_root):
    return AsyncSFTPPath(sync_root)


@pytest.fixture
def no_workers(monkeypatch):
    """Call inside a coroutine: from then on a worker thread fails the test."""

    def refuse(*args, **kwargs):
        raise AssertionError("a worker thread was used")

    def install():
        monkeypatch.setattr(asyncio, "to_thread", refuse)
        monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", refuse)

    return install


def test_the_native_methods_do_not_use_a_worker_thread(root, sync_root, no_workers):
    async def main():
        await root.stat()  # connects
        no_workers()
        assert (await (root / "a.txt").stat()).st_size == 1
        assert (await (root / "a.txt").lstat()).st_size == 1
        assert await (root / "sub").is_dir() and not await (root / "sub").is_file()
        assert await (root / "a.txt").is_file() and await (root / "sub").exists()
        assert not await (root / "nope").exists()
        assert sorted([p.name async for p in (root / "sub").iterdir()]) == [
            "c.py",
            "nested",
        ]
        walked = {str(p.path): (sorted(d), sorted(f)) async for p, d, f in root.walk()}
        assert walked["/sub"] == (["nested"], ["c.py"])
        assert await (root / "a.txt").read_bytes() == b"a"
        assert await (root / "a.txt").read_text() == "a"
        assert await (root / "w.txt").write_text("line\n") == 5
        assert await (root / "w.bin").write_bytes(b"\x00\x01") == 2
        async with (root / "o.bin").open("wb") as file:
            await file.write(b"abc")
        async with (root / "o.txt").open("w") as file:
            await file.write("x\ny\n")
        async with (root / "o.txt").open("r") as file:
            assert await file.readlines() == ["x\n", "y\n"]
        await (root / "d" / "e").mkdir(parents=True)
        await (root / "d" / "e" / "f").write_bytes(b"1")
        moved = await (root / "w.bin").rename(root / "d" / "m.bin")
        assert moved.path == sync_root / "d" / "m.bin"
        await (root / "d" / "e" / "f").unlink()
        await (root / "d" / "e").rmdir()
        await (root / "sub").copy(root / "copy", recursive=True)
        await (root / "copy").rm(recursive=True)

    aio.run(main())
    assert (sync_root / "w.txt").read_text() == "line\n"
    assert (sync_root / "d" / "m.bin").read_bytes() == b"\x00\x01"
    assert not (sync_root / "copy").exists()
    assert (sync_root / "o.bin").read_bytes() == b"abc"


def test_a_derived_path_is_an_async_sftp_path_on_the_same_backend(root, sync_root):
    child_path = root / "sub" / "c.py"
    assert type(child_path) is AsyncSFTPPath
    assert type(child_path.parent) is AsyncSFTPPath
    assert all(type(p) is AsyncSFTPPath for p in child_path.parents)
    assert type(child_path.with_name("x")) is AsyncSFTPPath
    assert type(child_path.relative_to(root / "sub")) is AsyncSFTPPath
    assert child_path.path.backend is sync_root.backend

    async def main():
        listed = [p async for p in root.glob("*.txt")]
        assert listed and all(type(p) is AsyncSFTPPath for p in listed)
        assert type(await (root / "sub").resolve()) is AsyncSFTPPath
        walked = [p async for p, _d, _f in root.walk()]
        assert all(type(p) is AsyncSFTPPath for p in walked)

    aio.run(main())


def test_the_connection_is_shared_and_opened_on_first_use(root, sync_root, monkeypatch):
    opened = []
    real = _session._aconnect

    async def counting(*args, **kwargs):
        entry = await real(*args, **kwargs)
        opened.append(entry)
        return entry

    monkeypatch.setattr(_session, "_aconnect", counting)

    async def main():
        assert not opened
        built = AsyncSFTPPath(sync_root)
        assert not opened  # building a path connects nothing
        await asyncio.gather(*((built / n).read_bytes() for n in ("a.txt", "b.py")))
        await asyncio.gather(
            *(AsyncSFTPPath(sync_root / "a.txt").stat() for _ in range(6))
        )
        assert len(opened) == 1
        await built.aclose()
        await built.aclose()
        assert opened[0].conn.is_closed()
        await built.stat()
        assert len(opened) == 2

    aio.run(main())
    assert opened[1].conn.is_closed()  # the loop closing closed it


def test_async_with_closes_the_connection(root, monkeypatch):
    seen = []
    real = _session._aconnect

    async def counting(*args, **kwargs):
        seen.append(await real(*args, **kwargs))
        return seen[-1]

    monkeypatch.setattr(_session, "_aconnect", counting)

    async def main():
        async with root as inside:
            assert inside is root
            await inside.stat()
            assert not seen[0].conn.is_closed()
        assert seen[0].conn.is_closed()

    aio.run(main())


def test_each_loop_has_its_own_connection(root, monkeypatch):
    seen = []
    real = _session._aconnect

    async def counting(*args, **kwargs):
        seen.append(await real(*args, **kwargs))
        return seen[-1]

    monkeypatch.setattr(_session, "_aconnect", counting)

    async def once():
        return await (root / "a.txt").read_text()

    assert aio.run(once()) == "a"
    assert aio.run(once()) == "a"
    assert len(seen) == 2 and all(entry.conn.is_closed() for entry in seen)

    results = []
    thread = threading.Thread(target=lambda: results.append(aio.run(once())))
    thread.start()
    thread.join()
    assert results == ["a"] and len(seen) == 3


def test_a_connection_that_dropped_is_replaced(root, monkeypatch):
    async def main():
        await root.stat()
        link = await root._link()
        link.aclient.exit()
        await link.aclient.wait_closed()
        assert await (root / "a.txt").read_bytes() == b"a"

    aio.run(main())


def _listed(path):
    return list(path.iterdir())


async def _alisted(path):
    return [item async for item in path.iterdir()]


#: (async call, sync call) pairs that fail the same way on the same server.
_FAILURES = {
    "stat of a missing file": (
        lambda p: p("nope").stat(),
        lambda p: p("nope").stat(),
    ),
    "read of a missing file": (
        lambda p: p("nope").read_bytes(),
        lambda p: p("nope").read_bytes(),
    ),
    "write below a missing directory": (
        lambda p: p("nope/x").write_bytes(b""),
        lambda p: p("nope/x").write_bytes(b""),
    ),
    "unlink of a missing file": (
        lambda p: p("nope").unlink(),
        lambda p: p("nope").unlink(),
    ),
    "unlink of a directory": (
        lambda p: p("sub").unlink(),
        lambda p: p("sub").unlink(),
    ),
    "rmdir of a directory with entries": (
        lambda p: p("sub").rmdir(),
        lambda p: p("sub").rmdir(),
    ),
    "rmdir of a file": (
        lambda p: p("a.txt").rmdir(),
        lambda p: p("a.txt").rmdir(),
    ),
    "mkdir below a missing directory": (
        lambda p: p("sub/x/y").mkdir(),
        lambda p: p("sub/x/y").mkdir(),
    ),
    "mkdir of a directory": (
        lambda p: p("sub").mkdir(),
        lambda p: p("sub").mkdir(),
    ),
    "read of a directory": (
        lambda p: p("sub").read_bytes(),
        lambda p: p("sub").read_bytes(),
    ),
    "text read of a directory": (
        lambda p: p("sub").read_text(),
        lambda p: p("sub").read_text(),
    ),
    "rename of a missing file": (
        lambda p: p("nope").rename(p("other")),
        lambda p: p("nope").rename(p("other")),
    ),
    "listing of a file": (
        lambda p: _alisted(p("a.txt")),
        lambda p: _listed(p("a.txt")),
    ),
    "listing of a missing directory": (
        lambda p: _alisted(p("nope")),
        lambda p: _listed(p("nope")),
    ),
}


async def _async_failure(make):
    try:
        await make()
    except OSError as error:
        return type(error), error.errno
    raise AssertionError("no error")


@pytest.mark.parametrize("case", sorted(_FAILURES))
def test_errors_are_those_of_the_sync_path(case, root, sync_root):
    with_async, with_sync = _FAILURES[case]
    try:
        with_sync(lambda name: sync_root / name)
    except OSError as error:
        expected = type(error), error.errno
    else:
        raise AssertionError("the sync call succeeded")
    got = aio.run(_async_failure(lambda: with_async(lambda name: root / name)))
    assert got == expected


def test_an_exclusive_open_of_an_existing_file_is_a_file_exists_error(root):
    async def main():
        with pytest.raises(FileExistsError):
            async with (root / "a.txt").open("xb"):
                pass
        async with (root / "fresh").open("xb") as file:
            await file.write(b"1")

    aio.run(main())


def test_mkdir_exist_ok_and_parents(root, sync_root):
    async def main():
        await (root / "p" / "q" / "r").mkdir(parents=True)
        await (root / "p" / "q" / "r").mkdir(parents=True, exist_ok=True)
        await (root / "p").mkdir(exist_ok=True)
        with pytest.raises(FileExistsError):
            await (root / "a.txt").mkdir(exist_ok=True)

    aio.run(main())
    assert (sync_root / "p" / "q" / "r").is_dir()


def test_rename_replaces_a_file_and_refuses_another_server(root, sync_root):
    async def main():
        await (root / "b.py").rename(root / "a.txt")
        assert await (root / "a.txt").read_text() == "b"
        renamed = await (root / "a.txt").rename("z.txt")
        assert renamed.name == "z.txt"
        other = SFTPPath("/x", host="127.0.0.2", port=1, ssh_config=None)
        with pytest.raises(NotImplementedError):
            await (root / "z.txt").rename(other)

    aio.run(main())
    assert (sync_root / "z.txt").read_text() == "b" and not (
        sync_root / "b.py"
    ).exists()


def test_text_files_follow_the_sync_encoding_and_newline_rules(root, sync_root):
    async def main():
        await (root / "crlf.txt").write_text("a\nb\n", newline="\r\n")
        await (root / "u16.txt").write_text("héllo", encoding="utf-16")
        assert await (root / "crlf.txt").read_text() == "a\nb\n"
        assert await (root / "crlf.txt").read_text(newline="") == "a\r\nb\r\n"
        assert await (root / "u16.txt").read_text(encoding="utf-16") == "héllo"
        big = "0123456789\n" * 20000
        async with (root / "big.txt").open("w") as file:
            for _ in range(2):
                await file.write(big)
            await file.flush()
            assert (sync_root / "big.txt").stat().st_size >= 64 * 1024
        assert await (root / "big.txt").read_text() == big * 2
        with pytest.raises(UnicodeEncodeError):
            await (root / "bad.txt").write_text("é", encoding="ascii")
        with pytest.raises(TypeError):
            await (root / "bad.txt").write_text(b"x")
        with pytest.raises(LookupError):
            await (root / "x.txt").write_text("x", encoding="no-such-codec")
        assert not await (root / "x.txt").exists()

    aio.run(main())
    assert (sync_root / "crlf.txt").read_bytes() == b"a\r\nb\r\n"
    assert (sync_root / "u16.txt").read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")


def test_open_modes_validate_like_open(root):
    async def main():
        with pytest.raises(ValueError):
            await root.open("rw").__aenter__()
        with pytest.raises(ValueError):
            await root.open("rbt").__aenter__()
        with pytest.raises(ValueError):
            await root.open("rb", encoding="utf-8").__aenter__()
        with pytest.raises(ValueError):
            await root.open("").__aenter__()

    aio.run(main())


def test_binary_files_read_lines_seek_and_truncate(root, sync_root):
    (sync_root / "lines.bin").write_bytes(b"one\ntwo\r\nthree")

    async def main():
        async with (root / "lines.bin").open("rb") as file:
            assert await file.readline() == b"one\n"
            assert await file.tell() == 4
            assert await file.readline(2) == b"tw"
            assert await file.read(1) == b"o"
            assert await file.seek(-5, 2) == 9
            assert await file.read() == b"three"
            assert await file.read() == b""
            await file.seek(0)
            assert [line async for line in file] == [b"one\n", b"two\r\n", b"three"]
            assert file.readable() and not file.writable()
            with pytest.raises(OSError):
                await file.write(b"x")
        assert file.closed
        with pytest.raises(ValueError):
            await file.read()
        async with (root / "rw.bin").open("w+b") as file:
            await file.write(b"hello world")
            await file.seek(6)
            assert await file.read(5) == b"world"
            await file.truncate(5)
            await file.seek(0)
            assert await file.read() == b"hello"
        async with (root / "rw.bin").open("ab") as file:
            await file.write(b"!")

    aio.run(main())
    assert (sync_root / "rw.bin").read_bytes() == b"hello!"


def test_walk_prunes_goes_bottom_up_and_reports_errors(root, sync_root):
    async def main():
        seen = []
        async for path, dirs, _files in root.walk():
            seen.append(path.name)
            if "sub" in dirs:
                dirs.remove("sub")
        assert "nested" not in seen and "sub" not in seen[1:]
        bottom = [str(p.path) async for p, _d, _f in root.walk(top_down=False)]
        assert bottom[-1] == "/" and bottom.index("/sub/nested") < bottom.index("/sub")
        errors = []
        gone = [x async for x in (root / "nope").walk(on_error=errors.append)]
        assert gone == [] and isinstance(errors[0], FileNotFoundError)
        assert [x async for x in (root / "nope").walk()] == []

    aio.run(main())


def test_walk_matches_the_sync_walk_with_a_link(root, sync_root):
    (sync_root / "ln").symlink_to("sub")

    async def main():
        for follow in (False, True):
            got = sorted(
                [
                    (str(p.path), sorted(d), sorted(f))
                    async for p, d, f in root.walk(follow_symlinks=follow)
                ]
            )
            want = sorted(
                (str(p), sorted(d), sorted(f))
                for p, d, f in sync_root.walk(follow_symlinks=follow)
            )
            assert got == want, follow

    aio.run(main())


def test_rm_and_copy_fall_back_for_what_is_not_a_tree_on_one_server(
    root, sync_root, monkeypatch
):
    async def main():
        await (root / "a.txt").copy(root / "a2.txt")
        assert await (root / "a2.txt").read_text() == "a"
        await (root / "a2.txt").rm()
        with pytest.raises(FileNotFoundError):
            await (root / "a2.txt").rm()
        await (root / "a2.txt").rm(missing_ok=True)
        with pytest.raises(FileExistsError):
            await (root / "sub").copy(root / "empty_dir", recursive=True)
        with pytest.raises(OSError) as raised:
            await (root / "sub").copy(root / "sub" / "nested" / "in", recursive=True)
        assert raised.value.errno == errno.EINVAL
        await (root / "sub").copy(root / "empty_dir", recursive=True, overwrite=True)
        assert await (root / "empty_dir" / "nested" / "d.py").read_text() == "d"
        with pytest.raises(FileNotFoundError):
            await (root / "nope").rm(recursive=True)
        await (root / "nope").rm(recursive=True, missing_ok=True)
        seen = []
        await (root / "sub").rm(
            recursive=True, ignore_error=lambda error, path: seen.append(path) or True
        )

    aio.run(main())
    assert not (sync_root / "sub").exists()


def test_a_recursive_copy_and_rm_of_many_files_run_on_the_loop(
    root, sync_root, no_workers
):
    for number in range(40):
        directory = sync_root / "tree" / f"d{number % 5}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"f{number}").write_bytes(b"x" * number)

    async def main():
        await root.stat()
        no_workers()
        await (root / "tree").copy(root / "tree2", recursive=True)
        await (root / "tree").rm(recursive=True)

    aio.run(main())
    assert sum(1 for p in (sync_root / "tree2").rglob("f*")) == 40
    assert not (sync_root / "tree").exists()


def test_construction(sync_root, sftp_server, backends):
    built = AsyncSFTPPath(
        "/x", host="127.0.0.1", port=sftp_server.port, ssh_config=None
    )
    assert isinstance(built.path, SFTPPath)
    assert type(built.path.backend).__name__ == "AsyncSSHBackend"
    assert AsyncSFTPPath(built).path == built.path
    assert (built / "y").path == SFTPPath("/x/y", backend=built.path.backend)
    assert repr(built).startswith("AsyncSFTPPath(")
    with pytest.raises(TypeError):
        AsyncSFTPPath("/x")
    with pytest.raises(TypeError):
        AsyncSFTPPath("/x", backend=built.path.backend, host="h")
    with pytest.raises(TypeError):
        AsyncSFTPPath("/x", port=22)
    with pytest.raises(TypeError):
        AsyncSFTPPath(pathlib.PurePosixPath("/x"))


def test_a_path_on_another_backend_class_is_refused(sync_root, sftp_server, backends):
    pytest.importorskip("paramiko")
    other = make_backend("paramiko", sftp_server)
    backends.append(other)
    with pytest.raises(TypeError, match="AsyncSSHBackend"):
        AsyncSFTPPath(SFTPPath("/", backend=other))


def test_it_is_a_subclass_of_the_generic_async_path(root):
    import inspect

    import pathapi

    assert isinstance(root, pathapi.AsyncPath)
    for name in ("stat", "exists", "read_bytes", "write_text", "mkdir", "rm", "copy"):
        assert inspect.iscoroutinefunction(getattr(AsyncSFTPPath, name)), name
    for name in ("iterdir", "walk", "open", "glob", "rglob"):
        assert not inspect.iscoroutinefunction(getattr(AsyncSFTPPath, name)), name
    assert list(inspect.signature(AsyncSFTPPath.rm).parameters)[:4] == [
        "self",
        "recursive",
        "missing_ok",
        "ignore_error",
    ]


def test_without_asyncpath_the_package_imports_and_the_class_says_what_it_needs():
    result = child.run(
        """
        import sftppath
        try:
            sftppath.AsyncSFTPPath
        except ImportError as error:
            print("pathapi>=0.1.2" in str(error), error.name)
        else:
            print("no error")
        """,
        blocked=("pathapi.path._async",),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True", "pathapi"]


def test_without_asyncssh_the_class_names_the_extra():
    result = child.run(
        """
        import sftppath
        try:
            sftppath.AsyncSFTPPath
        except ImportError as error:
            print('sftppath[asyncssh]' in str(error))
        """,
        blocked=("asyncssh",),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True"]


def test_the_class_is_exported():
    assert "AsyncSFTPPath" in sftppath.__all__
    assert sftppath.AsyncSFTPPath is AsyncSFTPPath


# --- against a server that counts and reshapes what it receives ---------------


@pytest.fixture
def wired(sftp_loopback, sftp_root, sftp_wire, backends):
    """`(AsyncSFTPPath at the root, the served directory, the wire)`."""
    import asyncssh  # noqa: F401

    backend = make_backend("asyncssh", sftp_loopback)
    backends.append(backend)
    return (
        AsyncSFTPPath(make_path("path", sftp_loopback, backend)),
        sftp_root,
        sftp_wire,
    )


def _asked_about(wire, ending):
    """How many stat requests named a path ending in `ending`."""
    return sum(
        1
        for name in ("stat", "lstat")
        for path in wire.paths(name)
        if path.endswith(ending)
    )


def test_a_hostile_listing_name_never_becomes_a_child(wired):
    import asyncssh

    root, served, wire = wired
    (served / "dir").mkdir()
    (served / "dir" / "ok.txt").write_bytes(b"x")
    wire.listing = lambda path: [
        asyncssh.SFTPName(name, attrs=asyncssh.SFTPAttrs(size=1))
        for name in (b"../victim.txt", b"a/b")
    ]

    async def main():
        listed = [p.name async for p in (root / "dir").iterdir()]
        errors = []
        walked = [
            (p.name, d, f)
            async for p, d, f in (root / "dir").walk(on_error=errors.append)
        ]
        return listed, walked, errors

    listed, walked, errors = aio.run(main())
    assert listed == ["ok.txt"]
    assert walked == [("dir", [], ["ok.txt"])]
    assert [type(error) for error in errors] == [ValueError, ValueError]


def test_a_listed_entry_that_does_not_say_what_it_is_is_asked_about(wired):
    import asyncssh

    root, served, wire = wired
    (served / "dir").mkdir()
    wire.listing = lambda path: [
        asyncssh.SFTPName(b"typeless", attrs=asyncssh.SFTPAttrs(size=3))
    ]

    async def main():
        await root.stat()
        wire.reset()
        return [(p.name, d, f) async for p, d, f in (root / "dir").walk()]

    assert aio.run(main()) == [("dir", [], ["typeless"])]
    assert _asked_about(wire, b"/dir/typeless") == 1


def test_a_walk_is_one_request_per_directory(wired):
    root, served, wire = wired
    for number in range(3):
        (served / f"d{number}").mkdir()
        (served / f"d{number}" / "f").write_bytes(b"1")

    async def main():
        await root.stat()
        wire.reset()
        return [p async for p, _d, _f in root.walk()]

    assert len(aio.run(main())) == 4
    assert wire.calls["scandir"] == 4
    # The listing says what each entry is: none is asked about.
    assert _asked_about(wire, b"/f") == 0


def test_a_cancelled_close_is_complete_before_the_cancellation_arrives(wired):
    root, served, wire = wired
    closed = threading.Event()

    def slow(server, *args):
        time.sleep(0.3)

    def done(result, *args):
        closed.set()
        return result

    async def main():
        file = await (root / "late.bin").open("wb")
        await file.write(b"data")
        wire.before["close"] = slow
        wire.after["close"] = done
        closing = asyncio.ensure_future(file.aclose())
        await asyncio.sleep(0.05)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        # The server answered the close before the cancellation reached us.
        assert closed.is_set()
        assert file.closed

    aio.run(main())


def test_a_text_file_sends_nothing_until_it_is_flushed_or_closed(wired):
    root, served, wire = wired

    async def main():
        async with (root / "t.txt").open("w") as file:
            await file.write("abc")
            sent = wire.calls["write"]
            await file.flush()
            assert wire.calls["write"] == sent + 1
            await file.write("def")
            assert wire.calls["write"] == sent + 1
        assert wire.calls["write"] == sent + 2

    aio.run(main())
    assert (served / "t.txt").read_text() == "abcdef"


def test_a_copy_into_itself_is_refused_before_anything_is_created(wired):
    root, served, wire = wired
    (served / "src").mkdir()

    async def main():
        with pytest.raises(OSError) as raised:
            await (root / "src").copy(root / "src" / "inside", recursive=True)
        return raised.value.errno

    assert aio.run(main()) == errno.EINVAL
    assert list((served / "src").iterdir()) == []


def test_a_bare_failure_is_told_apart_by_asking_about_the_entry(wired):
    import asyncssh

    root, served, wire = wired
    (served / "full").mkdir()
    (served / "full" / "x").write_bytes(b"1")
    (served / "file").write_bytes(b"1")

    def fail(server, *args):
        raise asyncssh.SFTPFailure("Failure")

    wire.before["remove"] = fail
    wire.before["rmdir"] = fail
    wire.before["mkdir"] = fail

    async def failure(call):
        try:
            await call
        except OSError as error:
            return type(error), error.errno

    async def main():
        return [
            await failure((root / "full").unlink()),
            await failure((root / "full").rmdir()),
            await failure((root / "file").rmdir()),
            await failure((root / "full").mkdir()),
        ]

    assert aio.run(main()) == [
        (IsADirectoryError, errno.EISDIR),
        (OSError, errno.ENOTEMPTY),
        (NotADirectoryError, errno.ENOTDIR),
        (FileExistsError, errno.EEXIST),
    ]
