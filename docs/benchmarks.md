# Benchmarks

`sftppath` includes a benchmark harness at `benchmarks/bench.py` that times
`SFTPPath` over paramiko and over asyncssh, and the `sftp:` scheme of `uripath`
on the same operations. It needs both SSH libraries and `uripath`:
`pip install -e ".[dev,all]"`.

## Run It

```bash
python benchmarks/bench.py            # run the benchmarks
python benchmarks/bench.py --help     # options and suites; runs nothing
```

A suite name after the options runs part of the matrix: `sftp-batch`,
`sftp-recursive`, `sftp-recursive-copy`, `sftp-recursive-large` or `sftp-async`
(`AsyncPath(SFTPPath(...))` against `AsyncSFTPPath`, which the default run also
includes).

## Saved Results

Every metric is timed over several samples. `--save` writes the run as JSON to
`benchmarks/results/<name>.json`, with the minimum, median and maximum
milliseconds per call of each metric plus the interpreter, platform and git
commit it ran on:

```bash
python benchmarks/bench.py --save
python benchmarks/bench.py --save --samples 9
```

`--samples N` applies one sample count to every case; without it each case uses
its own (1-5). The result schema, the suites and the committed results are
described in
[`benchmarks/README.md`](https://github.com/jose-pr/sftppath/blob/main/benchmarks/README.md).

Compare two runs on their medians, and only between runs from the same machine
and interpreter. A run on a developer machine is a sanity check; performance
claims in release notes come from CI runs.

## What It Covers

- `stat()`, listing (`iterdir()`), `walk()` and `glob()` of a small tree
- `read_bytes()`, `write_bytes()` of 256 KiB, `mkdir()`, `rename()`, `unlink()`
  and a single-file `copy()`
- recursive `copy()` and `rm()` of 4-, 9- and 128-file trees, and the asyncssh
  recursive copy at several `max_concurrency` values
- a cold connect and `stat()` on a new backend
- each of the above on a paramiko backend and on an asyncssh backend, and for
  both through `SFTPPath` (metric group `sftp`) and through
  `URIPath("sftp://...", backend=SFTPURIBackend(...))` (group `sftp-uri`), so
  the cost of the URI layer is the difference between the two groups

The cases run against an in-process asyncssh server on 127.0.0.1, so nothing
leaves the machine. Without `uripath` the `sftp-uri` group is skipped and the
run records why.

## Caveats

The server and the client share one machine and one interpreter, with no
network latency, so the numbers show the overhead of a backend and of the path
layer, not how a remote link behaves. Timings depend on the machine, the
interpreter and what else is running; compare medians, as described above.
