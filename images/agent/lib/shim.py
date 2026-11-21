#!/usr/bin/python3 -I
"""The `claude` and `codex` shims of a trial (design D5); `/opt/rbench/bin/<cli>` links here.

The shim registers the launch with the trial's collector, starts the real CLI as its own child
with the engine's stdio, forwards signals to it and exits with its status. For a session launch
it adds the policy flags from the trial's settings file, the model and effort of a role delivered
by flag when the engine's argv has none, a session id for a fresh Claude launch, and rewrites
Codex's `--sandbox read-only`, which cannot run in the container. It never changes the prompt.
For Codex it forks a watcher that binds each rollout the CLI holds open for appending.

Python 3.11 (Debian bookworm), standard library only.
"""

import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import uuid

SETTINGS = "/etc/rbench/shim.json"
REAL = {"claude": "/usr/local/bin/claude", "codex": "/usr/local/bin/codex"}
PROC = "/proc"
POLL_SECONDS = 0.05
FORWARDED = (
    signal.SIGHUP,
    signal.SIGINT,
    signal.SIGQUIT,
    signal.SIGTERM,
    signal.SIGUSR1,
    signal.SIGUSR2,
)

# Claude Code 2.1.316 `claude --help`, Commands. A launch whose first argument is one of these, or
# that asks for the version or help, is not a session and gets no flags.
# limit: a command after leading options (`claude --debug mcp list`) is taken for a session.
CLAUDE_COMMANDS = frozenset(
    [
        "agents",
        "attach",
        "auth",
        "auto-mode",
        "doctor",
        "gateway",
        "import",
        "install",
        "logs",
        "mcp",
        "plugin",
        "plugins",
        "purge",
        "respawn",
        "rm",
        "setup-token",
        "stop",
        "kill",
        "ultrareview",
        "update",
        "upgrade",
    ]
)
CLAUDE_INFO = frozenset({"-v", "--version", "-h", "--help"})
CLAUDE_CONTINUATION = ("-c", "--continue", "-r", "--resume", "--from-pr")

# Codex CLI 0.171.0 `codex --help`: the top-level options that take a value, and the commands.
CODEX_VALUE_OPTIONS = frozenset(
    [
        "-c",
        "--config",
        "--enable",
        "--disable",
        "--remote",
        "--remote-auth-token-env",
        "-i",
        "--image",
        "-m",
        "--model",
        "--local-provider",
        "-p",
        "--profile",
        "-s",
        "--sandbox",
        "-C",
        "--cd",
        "--add-dir",
        "-a",
        "--ask-for-approval",
    ]
)
CODEX_COMMANDS = frozenset(
    [
        "agents",
        "exec",
        "e",
        "review",
        "login",
        "logout",
        "mcp",
        "plugin",
        "app-server",
        "remote-control",
        "completion",
        "update",
        "doctor",
        "sandbox",
        "debug",
        "apply",
        "a",
        "resume",
        "queue",
        "archive",
        "delete",
        "migrate-rollouts",
        "migrate-nfs-db",
        "unarchive",
        "fork",
        "cloud",
        "exec-server",
        "features",
        "help",
    ]
)
CODEX_SESSIONS = frozenset({"exec", "e", "review", "resume", "fork"})  # and the TUI
CODEX_INFO = frozenset({"-V", "--version", "-h", "--help"})
CODEX_EXEC = frozenset({"exec", "e"})

ROLLOUT = re.compile(r"/sessions/(.+/)?rollout-[^/]*\.jsonl$")
O_ACCMODE, O_WRONLY, O_RDWR, O_APPEND = 0o3, 0o1, 0o2, 0o2000  # Linux, x86-64 and arm64


class ShimError(Exception):
    pass


def has_option(argv: list[str], *names: str) -> bool:
    """Whether `argv` passes one of the options `names`, also as `--name=value` or `-xvalue`."""
    for a in argv:
        if a in names or (a.startswith("--") and a.split("=", 1)[0] in names):
            return True
        if not a.startswith("--") and len(a) > 2 and a[:2] in names:
            return True
    return False


def claude_launch(argv: list[str], settings: dict) -> tuple[list[str], list[str], str | None]:
    """The argv for the real `claude`, the flags added and the session id the shim set."""
    if (argv and argv[0] in CLAUDE_COMMANDS) or any(a in CLAUDE_INFO for a in argv):
        return list(argv), [], None
    added = list(settings.get("flags", []))
    if "model" in settings and not has_option(argv, "--model"):
        added += ["--model", settings["model"]]
    if "effort" in settings and not has_option(argv, "--effort"):
        added += ["--effort", settings["effort"]]
    session = None
    continuation = has_option(argv, *CLAUDE_CONTINUATION)
    # Claude refuses --session-id with a continuation unless it forks (design D5).
    if not has_option(argv, "--session-id") and (
        not continuation or has_option(argv, "--fork-session")
    ):
        session = str(uuid.uuid4())
        added += ["--session-id", session]
    return added + list(argv), added, session


def codex_command(argv: list[str]) -> int | None:
    """The index of Codex's command in `argv`, or None for the TUI (no command, maybe a prompt)."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            return None
        if a.startswith("-"):
            i += 2 if a in CODEX_VALUE_OPTIONS else 1
            continue
        return i if a in CODEX_COMMANDS else None  # else the TUI's prompt
    return None


def _config_keys(argv: list[str]) -> set[str]:
    """The keys that `-c key=value` overrides set anywhere in `argv`."""
    keys = set()
    for i, a in enumerate(argv):
        if a in ("-c", "--config") and i + 1 < len(argv):
            value = argv[i + 1]
        elif a.startswith("--config="):
            value = a[len("--config=") :]
        elif a.startswith("-c") and len(a) > 2 and not a.startswith("--"):
            value = a[2:]
        else:
            continue
        keys.add(value.split("=", 1)[0].strip())
    return keys


def codex_launch(argv: list[str], settings: dict) -> tuple[list[str], list[str], list[dict]]:
    """The argv for the real `codex`, the flags added and the engine flags rewritten."""
    command = codex_command(argv)
    name = argv[command] if command is not None else None
    if any(a in CODEX_INFO for a in argv) or (name is not None and name not in CODEX_SESSIONS):
        return list(argv), [], []
    final, rewrites = list(argv), []
    for i, a in enumerate(argv):
        # The engine's read-only sandbox needs user namespaces the container lacks (design D5).
        if a in ("-s", "--sandbox") and i + 1 < len(argv) and argv[i + 1] == "read-only":
            final[i + 1] = "danger-full-access"
            rewrites.append({"index": i + 1, "from": "read-only", "to": "danger-full-access"})
        elif a in ("--sandbox=read-only", "-sread-only"):
            final[i] = a.replace("read-only", "danger-full-access")
            rewrites.append({"index": i, "from": a, "to": final[i]})
    added = list(settings.get("flags", []))
    keys = _config_keys(argv)
    if "model" in settings and "model" not in keys and not has_option(argv, "-m", "--model"):
        added += ["-c", "model=" + json.dumps(settings["model"])]
    if "effort" in settings and "model_reasoning_effort" not in keys:
        added += ["-c", "model_reasoning_effort=" + json.dumps(settings["effort"])]
    exec_flags = []
    if command is not None and name in CODEX_EXEC:  # `--ignore-rules` is an `exec` option
        exec_flags = list(settings.get("exec_flags", []))
        final[command + 1 : command + 1] = exec_flags
    return added + final, added + exec_flags, rewrites


def start_time(pid: int) -> int | None:
    """A process's start time in clock ticks since boot: with its pid, it names the process."""
    try:
        with open(f"{PROC}/{pid}/stat", encoding="ascii", errors="replace") as f:
            stat = f.read()
    except OSError:
        return None
    return int(stat.rpartition(")")[2].split()[19])


def parent(pid: int) -> int | None:
    try:
        with open(f"{PROC}/{pid}/stat", encoding="ascii", errors="replace") as f:
            return int(f.read().rpartition(")")[2].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def descendants(root: int) -> list[int]:
    """`root` and every process below it."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir(PROC):
        if entry.isdigit() and (ppid := parent(int(entry))) is not None:
            children.setdefault(ppid, []).append(int(entry))
    found, todo = [], [root]
    while todo:
        pid = todo.pop()
        found.append(pid)
        todo.extend(children.get(pid, []))
    return found


def open_rollouts(pid: int) -> list[tuple[str, int]]:
    """The rollout files `pid` holds open for appending, with their open flags (D5)."""
    found = []
    try:
        fds = os.listdir(f"{PROC}/{pid}/fd")
    except OSError:
        return found
    for fd in fds:
        try:
            path = os.readlink(f"{PROC}/{pid}/fd/{fd}")
            if not ROLLOUT.search(path):
                continue
            with open(f"{PROC}/{pid}/fdinfo/{fd}", encoding="ascii") as f:
                flags = next(
                    int(v, 8) for k, _, v in (ln.partition(":") for ln in f) if k == "flags"
                )
        except (OSError, StopIteration, ValueError):
            continue
        if flags & O_ACCMODE in (O_WRONLY, O_RDWR) and flags & O_APPEND:
            found.append((path, flags))
    return found


def session_meta(path: str) -> dict | None:
    """The `session_meta` payload on a rollout's first line, once Codex has written it."""
    try:
        with open(path, encoding="utf-8") as f:
            line = f.readline()
        record = json.loads(line)
    except (OSError, ValueError):
        return None
    if not (isinstance(record, dict) and record.get("type") == "session_meta"):
        return None
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else None


# The session init data a binding records: what identifies the thread and how it started. The
# instructions and the account fields stay in the rollout.
META_KEYS = (
    "id",
    "session_id",
    "forked_from_id",
    "timestamp",
    "cwd",
    "originator",
    "cli_version",
    "source",
    "thread_source",
    "model_provider",
)


def watch(child: int, conn: socket.socket, stop: int) -> None:
    """Bind every rollout `child` or its descendants hold open for appending, until `stop` closes.

    A rollout is reported once per launch, when its session_meta is readable.
    """
    pending: dict[str, dict] = {}
    reported: set[str] = set()
    done = False
    while True:
        for pid in descendants(child):
            for path, flags in open_rollouts(pid):
                if path not in reported and path not in pending:
                    pending[path] = {"path": path, "pid": pid, "start": start_time(pid)}
                    pending[path]["flags"] = flags
        for path, binding in list(pending.items()):
            meta = session_meta(path)
            if meta is None and not done:
                continue
            meta = meta or {}
            binding["meta"] = {k: meta[k] for k in META_KEYS if k in meta}
            send(conn, {"kind": "binding", **binding})
            reported.add(path)
            del pending[path]
        if done:
            return
        done = bool(select.select([stop], [], [], POLL_SECONDS)[0])


def send(conn: socket.socket, record: dict) -> None:
    conn.sendall(json.dumps(record, sort_keys=True).encode() + b"\n")


def quiet_send(conn: socket.socket, record: dict) -> None:
    try:
        send(conn, record)
    except OSError:
        pass


def connect(path: str, first: dict) -> socket.socket:
    """Open a connection to the collector and have it accept `first`."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(path)
        send(conn, first)
        reply = conn.makefile("r", encoding="utf-8").readline()
    except OSError as e:
        raise ShimError(f"cannot reach the collector at {path}: {e}") from e
    if reply.strip() != '{"ok": true}':
        raise ShimError(f"the collector refused the {first['kind']} record: {reply.strip()!r}")
    return conn


def fork_watcher(child: int, socket_path: str) -> tuple[int, int]:
    """Fork the watcher; return its pid and the pipe end whose closing stops it."""
    stop, go = os.pipe()
    pid = os.fork()
    if pid:
        os.close(stop)
        return pid, go
    status = 1
    try:
        for signum in FORWARDED:  # the shim forwards them; its end stops the watcher
            signal.signal(signum, signal.SIG_IGN)
        os.close(go)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):  # the engine's stdio stays with the CLI alone
            os.dup2(null, fd)
        watch(child, connect(socket_path, {"kind": "watch"}), stop)
        status = 0
    finally:
        os._exit(status)


def load_settings(cli: str) -> dict:
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError) as e:
        raise ShimError(f"cannot read the trial's shim settings {SETTINGS}: {e}") from e
    if not (isinstance(settings, dict) and isinstance(settings.get("socket"), str)):
        raise ShimError(f"{SETTINGS}: no collector socket")
    if not isinstance(settings.get(cli, {}), dict):
        raise ShimError(f"{SETTINGS}: {cli} settings are not an object")
    return settings


def run(cli: str, argv: list[str]) -> int:
    settings = load_settings(cli)
    own = settings.get(cli, {})
    record = {"kind": "launch", "cli": cli, "argv": argv, "cwd": os.getcwd()}
    record["homes"] = {k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")}
    if cli == "claude":
        final, added, session = claude_launch(argv, own)
        record |= {"added": added, "session_id": session}
    else:
        final, added, rewrites = codex_launch(argv, own)
        record |= {"added": added, "rewrites": rewrites}
    record["final_argv"] = final
    conn = connect(settings["socket"], record)

    child = None
    pending: list[int] = []

    def forward(signum: int, _frame: object) -> None:
        if child is None:
            pending.append(signum)
        else:
            child.send_signal(signum)

    for signum in FORWARDED:
        signal.signal(signum, forward)
    try:
        child = subprocess.Popen([REAL[cli], *final])
    except OSError as e:
        raise ShimError(f"cannot start {REAL[cli]}: {e}") from e
    for signum in pending:
        child.send_signal(signum)
    # From here on the CLI runs: a lost record makes its sessions unbound, which fails the trial
    # closed, so the shim keeps serving the engine rather than leave the CLI orphaned.
    quiet_send(conn, {"kind": "child", "pid": child.pid, "start": start_time(child.pid)})
    watcher = fork_watcher(child.pid, settings["socket"]) if cli == "codex" else None
    status = child.wait()
    if watcher:
        os.close(watcher[1])
        os.waitpid(watcher[0], 0)
    quiet_send(conn, {"kind": "exit", "status": status})
    conn.close()
    return status


def main() -> None:
    cli = os.path.basename(sys.argv[0])
    if cli not in REAL:
        sys.exit(f"rbench shim: unknown CLI {cli!r}; expected one of {sorted(REAL)}")
    try:
        status = run(cli, sys.argv[1:])
    except ShimError as e:
        sys.exit(f"rbench shim ({cli}): {e}")
    if status < 0:  # the CLI died by a signal: die by the same one
        signal.signal(-status, signal.SIG_DFL)
        os.kill(os.getpid(), -status)
    sys.exit(status if status >= 0 else 128 - status)


if __name__ == "__main__":
    main()
