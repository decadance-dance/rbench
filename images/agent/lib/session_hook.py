#!/usr/bin/python3 -I
"""Claude Code's SessionStart hook in the managed settings (design D5).

Claude runs it for every session it starts, through the shim or not. It reports the session id,
its source (`startup`, `resume`, `fork`, ...) and the `claude` process it runs under, as pid and
start time, to the trial's collector. The audit joins that process with the CLI children the
shims started.

It prints nothing, because Claude adds a SessionStart hook's output to the model's context, and it
always exits 0: a missing record leaves the session unbound, which fails the trial closed.

Python 3.11 (Debian bookworm), standard library only.
"""

import json
import os
import socket
import sys

SETTINGS = "/etc/rbench/shim.json"
CLAUDE = "/usr/local/bin/claude"
PROC = "/proc"
FIELDS = ("session_id", "source", "transcript_path", "cwd", "model")


def stat(pid: int) -> list[str]:
    """`/proc/<pid>/stat` after the command name: [state, ppid, ...]."""
    with open(f"{PROC}/{pid}/stat", encoding="ascii", errors="replace") as f:
        return f.read().rpartition(")")[2].split()


def claude_process() -> tuple[int | None, int | None]:
    """The nearest ancestor that runs the real `claude` binary, as (pid, start time)."""
    real = os.path.realpath(CLAUDE)
    pid = os.getppid()
    while pid > 1:
        try:
            fields = stat(pid)
            if os.path.realpath(f"{PROC}/{pid}/exe") == real:
                return pid, int(fields[19])
            pid = int(fields[1])
        except (OSError, ValueError, IndexError):
            break
    return None, None


def main() -> None:
    try:
        data = json.load(sys.stdin)
        record = {k: data[k] for k in FIELDS if isinstance(data.get(k), str)}
        record["kind"] = "hook"
        record["claude_pid"], record["claude_start"] = claude_process()
        with open(SETTINGS, encoding="utf-8") as f:
            path = json.load(f)["socket"]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(10)
            conn.connect(path)
            conn.sendall(json.dumps(record, sort_keys=True).encode() + b"\n")
            conn.recv(64)
    except (OSError, ValueError, KeyError, TypeError):
        pass


if __name__ == "__main__":
    main()
