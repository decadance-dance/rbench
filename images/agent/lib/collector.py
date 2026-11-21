#!/usr/bin/python3 -I
"""The trial's shim-log collector (design D5); the runner starts it as user rbench-collector.

It listens on a Unix socket that trial processes may connect to and appends each record it
accepts, as one JSON line, to a log they cannot write. Each record gets a sequence number `seq` and
the sender's pid and uid (`sender_pid`, `sender_uid`) from the socket's peer credentials
(SO_PEERCRED), never values the sender reports.

A connection's first record says what it is:
- `launch`: a shim registers a launch, which gets the next launch number; the same connection
  then sends the launch's `child` (the CLI's pid and start time, from the shim's own fork) and
  `exit` records;
- `watch`: a Codex shim's watcher, accepted only from a direct child of a shim whose launch is
  registered and open; it then sends `binding` records for that launch;
- `hook`: one record from Claude's SessionStart hook, accepted from any process, because a hook
  of a CLI process no shim started is the evidence the audit looks for.
Anything else is logged as `rejected` and the connection closed.

Python 3.11 (Debian bookworm), standard library only.
"""

import json
import os
import socket
import socketserver
import struct
import sys
import threading

SOCKET = "/run/rbench/collector.sock"
LOG = "/var/log/rbench/shim.jsonl"
PROC = "/proc"
MAX_RECORD = 8 << 20  # an argv with the prompt in it fits

# The fields each record kind may carry and their types; anything else rejects the record.
STR, INT, NUM_OR_NONE = (str,), (int,), (int, type(None))
FIELDS = {
    "launch": {
        "cli": STR,
        "argv": (list,),
        "final_argv": (list,),
        "added": (list,),
        "rewrites": (list,),
        "session_id": (str, type(None)),
        "cwd": STR,
        "homes": (dict,),
    },
    "child": {"pid": INT, "start": NUM_OR_NONE},
    "exit": {"status": INT},
    "watch": {},
    "binding": {"path": STR, "pid": INT, "start": NUM_OR_NONE, "flags": INT, "meta": (dict,)},
    "hook": {
        "session_id": STR,
        "source": STR,
        "transcript_path": STR,
        "cwd": STR,
        "model": STR,
        "claude_pid": NUM_OR_NONE,
        "claude_start": NUM_OR_NONE,
    },
}
REQUIRED = {
    "launch": {"cli", "argv", "final_argv", "added"},
    "child": {"pid"},
    "exit": {"status"},
    "binding": {"path", "pid"},
    "hook": {"session_id", "source"},
}


class Rejected(Exception):
    pass


def check(record: object, kinds: tuple[str, ...]) -> dict:
    """`record` if it is a valid record of one of `kinds`; raise `Rejected` saying why otherwise."""
    if not isinstance(record, dict):
        raise Rejected("not a JSON object")
    kind = record.get("kind")
    if kind not in kinds:
        raise Rejected(f"kind {kind!r} here; expected one of {', '.join(kinds)}")
    fields = FIELDS[kind]
    for key, value in record.items():
        if key == "kind":
            continue
        if key not in fields:
            raise Rejected(f"{kind}: unknown field {key!r}")
        if not isinstance(value, fields[key]) or isinstance(value, bool):
            raise Rejected(f"{kind}: field {key!r} has type {type(value).__name__}")
    if missing := REQUIRED.get(kind, set()) - record.keys():
        raise Rejected(f"{kind}: missing {', '.join(sorted(missing))}")
    for key in ("argv", "final_argv", "added"):
        if not all(isinstance(a, str) for a in record.get(key, [])):
            raise Rejected(f"{kind}: {key} holds a non-string")
    return record


def parent(pid: int) -> int | None:
    try:
        with open(f"{PROC}/{pid}/stat", encoding="ascii", errors="replace") as f:
            return int(f.read().rpartition(")")[2].split()[1])
    except (OSError, ValueError, IndexError):
        return None


class Log:
    """The append-only log and the registry of open launches."""

    def __init__(self, path: str) -> None:
        self.fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self.lock = threading.Lock()
        self.seq = 0
        self.launches = 0
        self.open: dict[int, int] = {}  # shim pid -> launch number, while its connection is open
        self.watched: set[int] = set()  # launches that have a watcher

    def write(self, record: dict) -> None:
        with self.lock:
            self.seq += 1
            line = json.dumps({"seq": self.seq, **record}, sort_keys=True) + "\n"
            os.write(self.fd, line.encode())

    def register(self, shim: int) -> int:
        with self.lock:
            self.launches += 1
            self.open[shim] = self.launches
            return self.launches

    def close(self, shim: int, launch: int) -> None:
        with self.lock:
            if self.open.get(shim) == launch:
                del self.open[shim]

    def watcher_of(self, pid: int) -> int | None:
        """The launch whose shim forked `pid`, if it has no watcher yet."""
        shim = parent(pid)
        with self.lock:
            launch = self.open.get(shim) if shim is not None else None
            if launch is None or launch in self.watched:
                return None
            self.watched.add(launch)
            return launch


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        log: Log = self.server.log  # type: ignore[attr-defined]
        creds = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        pid, uid, _gid = struct.unpack("3i", creds)
        peer = {"sender_pid": pid, "sender_uid": uid}
        launch = shim = None
        kinds: tuple[str, ...] = ("launch", "watch", "hook")
        try:
            while line := self.rfile.readline(MAX_RECORD + 1):
                if len(line) > MAX_RECORD or not line.endswith(b"\n"):
                    raise Rejected("record too long or cut off")
                try:
                    record = check(json.loads(line), kinds)
                except ValueError as e:
                    raise Rejected(f"not JSON: {e}") from e
                kind = record["kind"]
                if kind == "launch":
                    launch, shim = log.register(pid), pid
                    kinds = ("child", "exit")
                elif kind == "watch":
                    launch = log.watcher_of(pid)
                    if launch is None:
                        raise Rejected("watch: the sender's parent has no open launch")
                    kinds = ("binding",)
                log.write({**record, **peer, **({"launch": launch} if launch else {})})
                if kind in ("launch", "watch", "hook"):
                    self.wfile.write(b'{"ok": true}\n')
                if kind == "hook":
                    return
        except Rejected as e:
            log.write({"kind": "rejected", "reason": str(e), **peer})
        except OSError:
            pass
        finally:
            if launch is not None and shim is not None:
                log.close(shim, launch)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, log: Log) -> None:
        self.log = log
        if os.path.exists(path):
            os.unlink(path)
        super().__init__(path, Handler)
        os.chmod(path, 0o666)  # every trial process may connect; only this user reads the log


def main() -> None:
    socket_path, log_path = sys.argv[1:] or (SOCKET, LOG)  # tests pass their own paths
    Server(socket_path, Log(log_path)).serve_forever()


if __name__ == "__main__":
    main()
