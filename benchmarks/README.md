# Benchmarks

`bench.py` times `SFTPPath` on a loopback SFTP server, over `paramiko` and over
`asyncssh`: stat, listing, walk and glob, reads and writes, rename, unlink,
copy, recursive copy and remove, and a cold connect. The same operations run
again through the `sftp:` scheme of `uripath` when it is installed. Method notes
are on the docs site's Benchmarks page
([`docs/benchmarks.md`](../docs/benchmarks.md)).

Nothing here contacts an external host: the cases start an in-process asyncssh
server on 127.0.0.1 that accepts any login and serves a temporary directory, and
the backends read no ssh_config, agent or key.

## Reproduce

From the repository root, in a virtualenv with both SSH libraries and `uripath`
(the `all` extra):

```bash
python -m pip install -e ".[dev,all]"
python benchmarks/bench.py --save
```

That runs the default suite and writes
`benchmarks/results/<version>-py<major.minor>-<os>-<arch>.json`. Name another
suite after the options; its name is appended to the default file name:

```bash
python benchmarks/bench.py --help
python benchmarks/bench.py --save --samples 9 sftp-batch
SFTPPATH_BENCH_RECURSIVE=1 python benchmarks/bench.py --save --name my-run
```

| Option | Effect |
| --- | --- |
| `--save` | Write the run to `benchmarks/results/<name>.json`. |
| `--name NAME` | File name without `.json`. Default `<version>-py<major.minor>-<os>-<arch>[-<suite>]`. |
| `--results-dir DIR` | Write somewhere other than `benchmarks/results/`. |
| `--samples N` | Samples per metric for every case (1-100). Default: each case's own count, 1-5. |
| `--note TEXT` | Free text stored in the JSON. |

Without `uripath` the `sftp-uri` group is skipped and the reason is recorded in
`skipped`; without `paramiko` or `asyncssh` neither group runs.

## Suites

| Suite | Runs |
| --- | --- |
| (none) | The default suite: warm `stat()`, `iterdir()` of 72 entries, `walk()` and `glob()` of 80 files, `read_bytes()`, `write_bytes()` of 256 KiB, `mkdir()`, `rename()`, `unlink()`, `copy()` of a 256 KiB file, `rm(recursive=True)` of a 9-file tree, a cold connect + `stat()`, and the `sftp-async` rows. |
| `sftp-batch` | `read_bytes()` and `stat()` of 64 files, and `unlink()`. |
| `sftp-recursive` | `rm(recursive=True)` of a 9-file tree. |
| `sftp-recursive-copy` | `copy(recursive=True)` of a 4-file tree, and the same copy on asyncssh backends with `max_concurrency` 1 and 4. |
| `sftp-async` | `AsyncPath(SFTPPath(...))` against `AsyncSFTPPath`, both on asyncssh: a walk of a 128-file tree, `read_bytes()` of 64 files at once, and `copy(recursive=True)` and `rm(recursive=True)` of a 128-file tree. The default suite also runs it. Without `pathapi>=0.1.2` or asyncssh the group is skipped and the reason recorded. |
| `sftp-recursive-large` | `copy(recursive=True)` and `rm(recursive=True)` of 128-file trees, and the copy on asyncssh backends with `max_concurrency` 1, 4 and 8. |

`SFTPPATH_BENCH_RECURSIVE=1` adds the `sftp-recursive-copy` rows to the default
suite.

The group `sftp-async` has its own form: each case runs on a backend per form,
`threads` (`AsyncPath` over an `SFTPPath`, every call on a worker thread) and
`native` (`AsyncSFTPPath`, on one event loop that stays open for the run), and the
console shows `threads/native`: above 1, the native path was faster.

Every case runs on a paramiko backend and on an asyncssh backend, against the
same server and data. The group `sftp` is `SFTPPath("/", backend=...)`; the group
`sftp-uri` is `URIPath("sftp://...", backend=SFTPURIBackend(...))` with the same
backend class and options. A case that needs fresh state (the file to rename or
remove, the tree to remove) has it written to the served directory before the
timed call, so the time is the SFTP operation alone. The asyncssh backends bound
one request at 3 seconds, so a stalled request fails its case.

The console shows each case as `paramiko/asyncssh`: above 1, asyncssh was
faster.

## Comparing runs

Compare on the **median**: one sample's time hides run-to-run noise. Only
compare runs from the same machine and interpreter, and change one thing at a
time. The `python`, `interpreter` and `processor` fields say what a file was
measured on.

A run on a developer machine is a sanity check. Performance claims in the
changelog or release notes come from CI runs.

## Result schema (`sftppath.bench/1`)

One JSON object per run:

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | string | `"sftppath.bench/1"`. |
| `name` | string | The file name without `.json`. |
| `suite` | string | `"default"` or the suite name. |
| `source` | string | `"ci"` when run under GitHub Actions, else `"local"`. |
| `note` | string or null | `--note` text. |
| `created_utc` | string | ISO 8601 UTC timestamp of the save. |
| `package` | object | `{"name": "sftppath", "version": <pyproject version>}`. |
| `git` | object or null | `{"commit": <HEAD sha>, "dirty": <tracked files modified>}`; null outside a git checkout. |
| `python` | string | Implementation and version, e.g. `"CPython 3.14.7"`. |
| `interpreter` | string | `<major.minor>-<os>-<arch>`, where `<os>` is `os.name` (`nt`/`posix`) or `darwin` and `<arch>` is the architecture the interpreter was built for (`sysconfig.get_platform()`). |
| `processor` | string | `<platform.system()>-<platform.machine()>` of the host. |
| `cpu_count` | integer | `os.cpu_count()`. |
| `env` | object | Benchmark environment switches (`SFTPPATH_BENCH_RECURSIVE`). |
| `metrics` | object | Timing metrics, keyed by metric name (below). |
| `counters` | object | Call-count probes, keyed by name: `{"group": ..., <call name>: <count>, ...}`. No SFTP suite records one, so it is empty. |
| `errors` | object | Cases that raised: `{"group": ..., "error": "<Type>: <message>"}`. Only the recursive cases are recorded this way; any other case that raises stops the run. |
| `skipped` | object | Groups that could not run, keyed by group: `{"group": ..., "reason": ...}`. |

Each entry in `metrics`:

| Field | Type | Meaning |
| --- | --- | --- |
| `group` | string | `sftp` (`SFTPPath`) or `sftp-uri` (the `sftp:` scheme). |
| `min_ms` | number | Fastest sample, in milliseconds per call. |
| `median_ms` | number | Median sample, in milliseconds per call. Compare on this. |
| `max_ms` | number | Slowest sample, in milliseconds per call. |
| `samples` | integer | Samples taken (N). |
| `calls_per_sample` | integer | Calls of the operation timed in each sample; each sample's time is divided by this. |

Metric names are `<group>: <backend> <case>`, e.g. `"sftp: asyncssh warm stat()"`
or `"sftp-uri: paramiko iterdir() 72 entries"`. A batch case (such as
`read_bytes() 64-file batch`) counts the whole batch as one call. The copy at a
given `max_concurrency` is `"sftp: asyncssh recursive copy mc=4"`, and
`"sftp: asyncssh large recursive copy mc=4"` for the 128-file tree.

Excerpt (values illustrative):

```json
{
  "schema": "sftppath.bench/1",
  "name": "0.10.0-py3.14-posix-x86_64",
  "suite": "default",
  "source": "local",
  "python": "CPython 3.14.7",
  "interpreter": "3.14-posix-x86_64",
  "metrics": {
    "sftp: asyncssh warm stat()": {
      "group": "sftp",
      "min_ms": 0.52,
      "median_ms": 0.57,
      "max_ms": 0.66,
      "samples": 5,
      "calls_per_sample": 1
    }
  }
}
```

## Committed results

`results/` is tracked, so before/after numbers stay recoverable from history.
The baselines are the default suite with `--samples 5`:

```bash
python benchmarks/bench.py --save --samples 5 \
  --name baseline-local-0.10.0-py3.14-nt-arm64 \
  --note "Local sanity baseline of the default suite; not CI, not a performance claim."
```

| File | What it is |
| --- | --- |
| `baseline-local-0.10.0-py3.14-nt-arm64.json` | Local sanity baseline of the default suite at 0.10.0 on a Windows developer machine, CPython 3.14 (native ARM64 build). Not CI; not a performance claim. |
| `baseline-local-0.10.0-py3.9-nt-amd64.json` | The same run on CPython 3.9, the supported floor (x64 build, run under emulation on the same ARM64 machine, so its `processor` reads `Windows-AMD64`). |
| `ci-<os>-py3.14.json` | The `benchmark` job of the test workflow on a hosted runner, replaced at each release; earlier ones are in the file's history. |
