"""Sync a directory tree from a remote SFTP server to the local filesystem
using PathSyncer. Requires an SSH library (`pip install "sftppath[paramiko]"`
or `"sftppath[asyncssh]"`), a real SSH server and credentials -- guarded under
`if __name__ == "__main__"` so importing this module is always safe, and
skipped (prints setup instructions, exit 0) unless SFTP_EXAMPLE_HOST is set.

Run directly:

    export SFTP_EXAMPLE_HOST=myhost
    export SFTP_EXAMPLE_USER=myuser        # optional
    export SFTP_EXAMPLE_PASSWORD=mypass    # optional
    export SFTP_EXAMPLE_REMOTE_PATH=/var/log   # optional, default shown below
    python examples/sftp_sync.py
"""

import os
import sys
import tempfile

from pathapi import LocalPath
from pathapi.sync import PathAndStat, PathSyncer
from sftppath import SFTPPath


def checksum(entry: PathAndStat):
    return entry.stat.st_size


def sync_from_sftp(remote: SFTPPath, local_root: LocalPath):
    # repr() of a path names the server and never the password.
    print(f"Syncing {remote!r} -> {local_root}")
    syncer = PathSyncer(checksum, remove_missing=True)
    syncer.sync(remote, local_root, dry_run=False)
    print("Synced:", sorted(p.name for p in local_root.iterdir()))


if __name__ == "__main__":
    host = os.environ.get("SFTP_EXAMPLE_HOST")
    if not host:
        print(
            "SFTP_EXAMPLE_HOST is not set -- skipping. See this file's "
            "module docstring for the required environment variables.",
            file=sys.stderr,
        )
        raise SystemExit(0)

    remote = SFTPPath(
        os.environ.get("SFTP_EXAMPLE_REMOTE_PATH", "/var/log"),
        host=host,
        user=os.environ.get("SFTP_EXAMPLE_USER"),
        password=os.environ.get("SFTP_EXAMPLE_PASSWORD"),
    )

    with tempfile.TemporaryDirectory() as tmp:
        try:
            sync_from_sftp(remote, LocalPath(tmp))
        except Exception as error:
            print(
                f"Could not sync from {remote!r} ({error}); skipping.", file=sys.stderr
            )
