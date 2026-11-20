"""Atomic file writes: every ledger write goes through `atomic_write`."""

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, data: str | bytes) -> None:
    """Replace `path` with `data` so a reader sees the old file or the new one, never a part.

    The data goes to a temporary file in the same directory, is synced, then renamed over `path`.
    A process killed before the rename leaves `path` untouched and a `.<name>.*.tmp` file behind.

    The file is always written 0600, as `mkstemp` creates it, whatever the umask or the mode of the
    file it replaces: ledger results are private, and no other user needs to read them.

    An error before the rename leaves `path` as it was. An error from the final directory sync
    comes after the rename: `path` may already hold the new data, but whether the rename survives
    a power loss is unknown. A caller that retries or recovers should read `path` back, not assume
    the old content.
    """
    path = Path(path)
    payload = data.encode() if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    # Sync the directory too, so the rename itself survives a power loss.
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
