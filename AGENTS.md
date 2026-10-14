# sftppath — contributor orientation

Orientation for working in a checkout of this repository: layout, environments,
commands, CI and release. It is not the API reference and it does not ship.

| Header | Covers |
| --- | --- |
| [`src/sftppath/AGENTS.md`](src/sftppath/AGENTS.md) | The public API: `SFTPPath`, the backends, the `sftp:` scheme of `uripath`, the exceptions and the environment variable, with signatures and gotchas. It ships in the wheel, so it stays self-contained (no repo-relative links) and changes in the same commit as any public API change. |

[`docs/divergences.md`](docs/divergences.md) records every deliberate difference
from `pathlib.Path` that this package owns; the ones of the generic `Path`
operations are on the site of `pathlib-next`. `pathlib.Path` parity is the
contract; a behavioral divergence that is not recorded is a bug.

## Layout

| Path | Contents |
| --- | --- |
| `src/sftppath/` | The package (`src/` layout). `py.typed` and the API header ship with it. |
| `src/sftppath/__init__.py`, `exceptions.py`, `uri.py` | The public modules: the root exports `SFTPPath` and the backends (the two real ones lazily, so `import sftppath` loads no SSH library), `exceptions.py` the errors, `uri.py` the `sftp:` scheme (imports `uripath`; without it the import raises an `ImportError` naming the `uri` extra). |
| `src/sftppath/_path.py`, `_base.py` | `SFTPPath` (the protocol logic: requests, error mapping, rename and replace, the native-walk dispatch, on `pathlib_next.providers.TreePath`) and the backend contract. |
| `src/sftppath/_paramiko.py`, `_asyncssh/` | The two backends. `_asyncssh/` is a package of private modules: the shared loop (`_loop`), error translation (`_translate`), attributes, file and client wrappers, the connection cache, connecting, the backend and the native recursive walks (`_walk`). |
| `src/sftppath/_checkfile.py`, `_sshconfig.py`, `_errors.py`, `_select.py`, `_exit.py` | The `check-file-handle` checksums, ssh_config handling and host validation, the error builders, backend selection and the flush of files left open at exit. |
| `tests/` | The pytest suite. `tests/conftest.py` holds the loopback servers' fixtures, `tests/sftp_loopback.py` the counting SSH server, `tests/sftp_support.py` the helpers that build a backend and a path; `tests/mutation/` is the mutation list; `tests/probes/` are child processes that tests run. |
| `benchmarks/` | `bench.py` and committed JSON results; see [`benchmarks/README.md`](benchmarks/README.md). Not shipped. |
| `examples/` | Runnable scripts. Networked ones skip (exit 0) unless their environment variables are set. |
| `docs/`, `mkdocs.yml` | MkDocs site: hand-written pages plus a `mkdocstrings` API reference. |
| `.github/workflows/` | `test.yml`, `release.yml`, `docs.yml`. |
| `CHANGELOG.md` | Keep a Changelog. Released sections are frozen records. |

## Environment

Python 3.9 is the floor (`requires-python = ">=3.9"`) and 3.14 is the latest
supported. Test on both ends before claiming a change works.

Keep one virtualenv per interpreter under `.venv/<version>-<os>-<arch>/`
(gitignored), where `<os>` is `os.name` (`nt`/`posix`) or `darwin`, and `<arch>`
is the architecture the interpreter was built for:

```bash
python -m venv .venv/3.14-posix-x86_64
.venv/3.14-posix-x86_64/bin/python -m pip install -e ".[dev,all]"
```

On Windows the interpreter is `.venv\<name>\Scripts\python.exe`. `dev` holds the
test and build tools; `all` is both SSH libraries and `uripath`. The tests use
the SSH libraries as servers too, so a suite run without one skips what needs
it.

`pathlib-next` and `uripath` are dependencies. While the releases this package
asks for are not on PyPI, install both editable from their checkouts first (`pip
install -e ../pathlib-next -e ../uripath`), then this package.

A missing library does not fail the suite: its tests are skipped instead, so
check `pytest -rs` before trusting a green run. A venv holding both SSH libraries
and no `uripath` runs everything but the `sftp:` scheme (`test.yml` has a job
for it), and one holding only this package and `pytest` runs the tests that need
nothing else.

## Checks

```bash
python -m pytest -q -rs                              # full suite (pythonpath=src is configured)
python -m pytest -q --cov=sftppath --cov-report=term-missing
python -m black src/ tests/ benchmarks/ examples/    # formatting; --check to verify
mkdocs build --strict                                # docs must build with no warnings
python -m build && python -m twine check dist/*      # sdist + wheel into dist/
python tests/mutation/run.py --jobs 4                # every guard's tests must fail on a broken copy
python -m pytest -q -rs tests/test_public_surface.py -k "mypy_accepts or pyright_accepts"
```

- **The mutation list** (`tests/mutation/`) applies one single-line change to a
  guard of a temporary copy of `src/sftppath` and checks that the tests named for
  it fail. Run it on demand, never in CI, after writing or changing a safety
  test; a mutant whose line has moved is `STALE` and fails the run. Its
  `README.md` says how to add one. It ships in the sdist with `tests/`.
- **Tests block non-loopback network access** and starting any program but the
  interpreter (`tests/hermetic.py`, applied in `tests/conftest.py`). The SSH
  servers the tests start listen on the loopback interface with throwaway keys;
  no test reads `~/.ssh` (the session's home is an empty directory). Mark a test
  `@pytest.mark.allow_network` only when it must resolve a real name,
  `@pytest.mark.allow_program("tool")` when it runs one. `filterwarnings = error`
  includes `ResourceWarning`: close what a fixture opens.
- A test asserts what the server received, not just that no exception was
  raised, and never asserts how fast something happened or how far an
  operation got. A helper server stops promptly, on Linux too: a thread blocked
  in `accept()` is not woken by closing the listening socket from another thread
  there.
- A behaviour of a path runs for both classes: the `flavor` fixture is an
  `SFTPPath` and an `SFTPURIPath` (skipped without `uripath`), the `kind` fixture
  is each SSH library.
- **Typing**: the two typing tests run `mypy` and `pyright` (or `basedpyright`)
  on a consumer script and skip where neither is installed; the `typing` job
  installs them.
- **The documents are tested**: the header's stubs are compared with the code
  (`tests/test_shipped_header.py`) and the README and the API pages must name the
  public names (`tests/test_documentation.py`).

### Benchmarks

`python benchmarks/bench.py --help` lists the options. `--save` writes a JSON
result per (version, interpreter, platform) into `benchmarks/results/`; the
schema and the reproduce command are in
[`benchmarks/README.md`](benchmarks/README.md). A local run is a sanity check;
performance claims in the changelog or release notes come from CI runs
(`test.yml` with `benchmark` set).

### CI

Workflows live in `.github/workflows/`:

- `test.yml` runs on `workflow_dispatch` (inputs: `ref`, `pathlib_next_ref`,
  `uripath_ref`, `benchmark`) or on a pushed `ci-*` tag, never on ordinary
  pushes. To test a commit without the dashboard, push a uniquely named
  throwaway tag (`ci-<topic>-<timestamp>`), follow the run to completion, then
  delete the tag locally and on the remote. Jobs: `test` (Ubuntu on 3.9 to
  3.14, Windows and macOS on 3.9 and 3.14), `without-uripath`, `bare` (the
  package and `pytest` only, no extra), `floors` (every dependency at the lower
  bound `pyproject.toml` declares, on 3.9), `typing`, and `benchmark` (manual
  runs only). Change a range in `pyproject.toml` only, and name the API that
  needs a raised floor in a comment beside it.
- `release.yml` runs on a `v*` tag: test, `without-uripath`, `floors` and
  `typing` gates, build (the tag must name the version built, `twine check`), an
  installed-wheel smoke test with and without the extras, a strict docs build,
  then the GitHub release whose notes come from that version's `CHANGELOG.md`
  section. For a final tag only (no `-` in it) it then publishes to PyPI through
  Trusted Publishing and dispatches `docs.yml` at the tag.
- `docs.yml` owns every GitHub Pages deploy: on a push to `main` that touches the
  docs sources and on `workflow_dispatch`, which `release.yml` fires for a final
  tag.
- **Which `pathlib-next` and `uripath` the jobs install.** Every job that
  installs the package first installs them from
  `git+https://github.com/jose-pr/<repository>@<ref>` when a ref is given: the
  `pathlib_next_ref` and `uripath_ref` inputs of a manual run of `test.yml` or
  `docs.yml`, else the repository variables `PATHLIB_NEXT_REF` and `URIPATH_REF`
  (the only source for a `ci-*` tag and for `release.yml`). With neither, pip
  takes the release range from PyPI. A ref must hold a version inside that
  range. Set the variables while the releases are not on PyPI (`gh variable set
  PATHLIB_NEXT_REF --body <branch-or-sha>`), delete them afterwards.
  `release.yml` refuses a final tag while either is set.

## Conventions

- **Formatting is black**, pinned to `target-version = ["py39"]` so it never
  emits syntax the floor cannot parse. No linter is enforced; the typing tests
  are the type check.
- **Every file is LF** (`.gitattributes` sets `* text=auto eol=lf`). On Windows,
  black writes CRLF: convert the files back to LF after formatting and check
  `git diff --stat` for whitespace-only churn.
- **3.9 compatibility**: any module using `X | Y` in a runtime-evaluated
  annotation needs `from __future__ import annotations`.
- **Comments** say what the code cannot, in one to three lines, about the code
  as it is: no history, and nothing that refers outside this repository.
- **`*.local.*` files** are per-machine overrides: gitignored and excluded from
  both build targets. Keep hostnames and credentials in those, never in tracked
  files.
- Logical commits in `type: description` form (`feat:`, `fix:`, `docs:`,
  `chore:`); keep code with its tests, and docs, config and CI in separate
  commits. No agent attribution in commit messages: no `Co-Authored-By:` naming
  a model or assistant, no `*-Session:` trailers, no session URLs, no
  "generated with" footers.

## Releasing

1. Move the `[Unreleased]` entries under a new `## [x.y.z] - <date>` heading and
   add its link definition at the bottom of `CHANGELOG.md`.
2. Bump `version` in `pyproject.toml` in the same commit (PEP 440 syntax there;
   SemVer in tags and the changelog).
3. Before 1.0, bump the minor only when the documented API breaks. New methods,
   new optional arguments and fixes are patch releases.
   A fix that corrects behaviour (to `pathlib` parity, to what the
   documentation intends, or to remove a way to lose data) is a patch even
   where an earlier header described the old behaviour; when a caller can
   notice it, its changelog entry goes under `Changed` and says what to do.
4. Run the full suite on the floor and latest interpreters, `mkdocs build
   --strict`, `python -m build`, `twine check`, and the maintainer's leak check
   (a scan for private references and agent-attribution commit trailers). Judge
   it by exit code.
5. Push `main`, then the `v*` tag. Publishing is irreversible, so the tag is
   pushed only with the maintainer's explicit consent for that specific
   release. The releases the dependency ranges ask of `pathlib-next` and
   `uripath` must be on PyPI first, and the repository variables
   `PATHLIB_NEXT_REF` and `URIPATH_REF` deleted.

Once per repository, before the first release: create `jose-pr/sftppath` on
GitHub; register PyPI Trusted Publishing for the project `sftppath` (workflow
`release.yml`, environment `pypi`); set Pages to build from GitHub Actions and
allow `v*` tags in the `github-pages` environment (`gh api -X POST
repos/jose-pr/sftppath/pages -f build_type=workflow`, `gh api -X POST
repos/jose-pr/sftppath/environments/github-pages/deployment-branch-policies -f
name='v*' -f type=tag`); and let workflows write (Settings, Actions, General,
Workflow permissions).

Changelog entries say what changed and what a user must do about it. They do
not describe how the work was done.
