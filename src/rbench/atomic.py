"""Atomic file writes: every ledger write goes through `atomic_write`."""

import os
import stat
import tempfile
from pathlib import Path

# os.umask can only be read by setting it, which races with other threads; read it once at import.
# limit: a umask changed after import is not seen.
_UMASK = os.umask(0o022)
os.umask(_UMASK)


def atomic_write(path: Path, data: str | bytes) -> None:
    """Replace `path` with `data` so a reader sees the old file or the new one, never a part.

    The data goes to a private (0600) temporary file in the same directory and is synced. The file
    then takes the mode of the file it replaces, or 0666 minus the umask for a new file, and is
    renamed over `path`. A process killed before the rename leaves `path` untouched and a
    `.<name>.*.tmp` file behind.

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
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
            except FileNotFoundError:
                mode = 0o666 & ~_UMASK
            os.fchmod(f.fileno(), mode)
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
