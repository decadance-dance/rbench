"""The shim log, the trial's launch record and the audit's session binding (design D5).

The collector's log holds, in `seq` order:
- per shim launch: `launch` (argv as the engine passed it, the argv the CLI got, the flags added
  and rewritten), `child` (the CLI's pid and start time, from the shim's own fork) and `exit`;
- `binding`: a Codex rollout the CLI of a launch held open for appending, with its session_meta;
- `hook`: a Claude session start, with the pid and start time of the `claude` process it ran under;
- `effort`: the effort that applied to a Claude turn (`Stop`) or subagent (`SubagentStop`), as the
  CLI reports it to the hook (#46);
- `rejected`: a record the collector refused.

Binding (spec trial-isolation, Policy enforced below the shims): every root session in the
transcripts must have exactly one origin launch, the shim launch that started it, and may have any
number of resume launches. `unbound_sessions` lists every session that breaks this, for the
contamination audit (#41).
- Claude: a hook joins its `claude` process with a launch's child. Sources `startup`, `fork` and
  `clear` start a session, so the launch is its origin; `resume` and `compact` continue one.
  A subagent transcript is bound through its session.
- Codex: a launch that resumes (`codex resume`, `codex exec resume`) binds a root thread as a
  resume, any other launch as its origin; a root thread no other launch bound has no origin. A child
  thread (its session_meta names a `parent_thread_id`) is bound when the launch that bound it also
  bound that parent root thread.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path

from rbench.ledger.plan import Image

CLAUDE_ROOT = re.compile(r"/projects/[^/]+/([^/]+)\.jsonl$")
CLAUDE_SUBAGENT = re.compile(r"/projects/[^/]+/([^/]+)/subagents/[^/]+\.jsonl$")
CODEX_ROLLOUT = re.compile(r"/rollout-[^/]*\.jsonl$")
ORIGIN_SOURCES = ("startup", "fork", "clear")
LAUNCH_KEYS = ("cli", "argv", "final_argv", "added", "rewrites", "session_id", "cwd", "homes")


# The fields the binding reads, by record kind, as the collector writes them
# (images/agent/lib/collector.py); a record that lacks one or holds another type is not read.
RECORD_FIELDS: dict[str, dict[str, tuple[type, ...]]] = {
    "launch": {"launch": (int,), "cli": (str,), "argv": (list,)},
    "child": {"launch": (int,), "pid": (int,), "start": (int, type(None))},
    "exit": {"launch": (int,), "status": (int,)},
    "binding": {"launch": (int,), "path": (str,), "meta": (dict, type(None))},
    "hook": {
        "session_id": (str,),
        "source": (str,),
        "claude_pid": (int, type(None)),
        "claude_start": (int, type(None)),
    },
    "effort": {
        "event": (str,),
        "session_id": (str,),
        "agent_id": (str,),
        "effort": (str,),
        "claude_pid": (int, type(None)),
        "claude_start": (int, type(None)),
    },
    "watch": {},
    "rejected": {},
}
OPTIONAL_FIELDS = {"start", "meta", "claude_pid", "claude_start", "agent_id", "effort"}


def _typed(value: object, types: tuple[type, ...]) -> bool:
    return isinstance(value, types) and not isinstance(value, bool)


class SessionError(ValueError):
    """A shim log or transcript that cannot be read as one."""


def read_shim_log(path: Path) -> list[dict]:
    """The records of a shim log, in `seq` order."""
    records = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
        except ValueError as e:
            raise SessionError(f"{path}:{n}: not JSON: {e}") from e
        if not (
            isinstance(record, dict)
            and _typed(record.get("seq"), (int,))
            and isinstance(record.get("kind"), str)
            and record["kind"] in RECORD_FIELDS
        ):
            raise SessionError(f"{path}:{n}: not a shim log record")
        for key, types in RECORD_FIELDS[record["kind"]].items():
            present = key in record or key not in OPTIONAL_FIELDS
            if present and not _typed(record.get(key), types):
                raise SessionError(f"{path}:{n}: {record['kind']} record: invalid {key}")
        if record["kind"] == "launch" and not all(isinstance(a, str) for a in record["argv"]):
            raise SessionError(f"{path}:{n}: launch record: invalid argv")
        records.append(record)
    launches = {r["launch"] for r in records if r["kind"] == "launch"}
    for r in records:
        if r["kind"] in ("child", "exit", "binding") and r["launch"] not in launches:
            raise SessionError(
                f"{path}: a {r['kind']} record of launch {r['launch']}, never logged"
            )
    return sorted(records, key=lambda r: r["seq"])


def _children(records: list[dict]) -> dict[tuple, int]:
    """(pid, start time) of each launch's CLI process -> the launch."""
    return {(r["pid"], r.get("start")): r["launch"] for r in records if r["kind"] == "child"}


def launch_record(image: Image, records: list[dict]) -> dict:
    """What the trial records about its CLI launches: the image digest and its CLI versions, the
    argv of every launch, and each session's init data with the launch it is bound to."""
    launches: dict[int, dict] = {}
    for r in records:
        if r["kind"] == "launch":
            launches[r["launch"]] = {"launch": r["launch"]} | {k: r.get(k) for k in LAUNCH_KEYS}
        elif r["kind"] == "child":
            launches[r["launch"]] |= {"pid": r["pid"], "start": r.get("start")}
        elif r["kind"] == "exit":
            launches[r["launch"]]["exit"] = r["status"]
    children = _children(records)
    sessions = []
    for r in records:
        if r["kind"] == "hook":
            launch = children.get((r.get("claude_pid"), r.get("claude_start")))
            keys = ("session_id", "source", "transcript_path", "cwd", "model")
            sessions.append({"cli": "claude", "launch": launch} | {k: r[k] for k in keys if k in r})
        elif r["kind"] == "binding":
            sessions.append(
                {"cli": "codex", "launch": r["launch"], "path": r["path"], "meta": r.get("meta")}
            )
    return {
        "image": image.ref,
        "clis": dict(sorted(image.clis.items())),
        "launches": [launches[n] for n in sorted(launches)],
        "sessions": sessions,
        "rejected": [r for r in records if r["kind"] == "rejected"],
    }


def _lines(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def _find(value: object, key: str) -> str | None:
    """The first string value of `key` anywhere in a JSON value."""
    if isinstance(value, dict):
        if isinstance(value.get(key), str):
            return value[key]
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    return next((v for v in (_find(c, key) for c in children) if v is not None), None)


def unbound_sessions(records: list[dict], transcripts: Path) -> list[str]:
    """Every session in `transcripts` (container paths below that directory) that does not bind
    to exactly one origin launch, every hook of a `claude` process no shim started, and every
    launch whose exit the log does not hold, so that what it recorded may be incomplete (D5)."""
    exited = {r["launch"] for r in records if r["kind"] == "exit"}
    problems = [
        f"Launch {r['launch']} ({r.get('cli')}): no exit record in the shim log, so its records "
        "may be incomplete"
        for r in records
        if r["kind"] == "launch" and r["launch"] not in exited
    ]
    children = _children(records)

    # Claude: session id -> origin launches.
    origins: dict[str, list[int]] = {}
    for r in records:
        if r["kind"] != "hook":
            continue
        launch = children.get((r.get("claude_pid"), r.get("claude_start")))
        if launch is None:
            problems.append(
                f"Claude session {r['session_id']}: its claude process (pid "
                f"{r.get('claude_pid')}) was not started by a shim"
            )
        elif r["source"] in ORIGIN_SOURCES:
            origins.setdefault(r["session_id"], []).append(launch)

    # Codex: rollout path -> launches that bound it, in order, and the launches that resume.
    resumes = {r["launch"] for r in records if r["kind"] == "launch" and _resumes(r)}
    bound: dict[str, list[int]] = {}
    roots: dict[int, set[str]] = {}  # launch -> root thread ids it bound
    for r in records:
        if r["kind"] == "binding":
            bound.setdefault(r["path"], []).append(r["launch"])
            meta = r.get("meta") or {}
            if isinstance(meta.get("id"), str) and _find(meta, "parent_thread_id") is None:
                roots.setdefault(r["launch"], set()).add(meta["id"])

    # Regular files only: a symlink could lead the audit to a file outside the trial's snapshot.
    files = sorted(p for p in transcripts.rglob("*.jsonl") if p.is_file() and not p.is_symlink())
    claude_roots = set()
    for file in files:
        path = "/" + file.relative_to(transcripts).as_posix()
        if CODEX_ROLLOUT.search(path):
            problems += _codex_problems(path, file, bound, roots, resumes)
        elif m := CLAUDE_ROOT.search(path):
            session = m.group(1)
            if not any(r.get("sessionId") == session for r in _lines(file)):
                continue  # a file in a reviewed repository, not a transcript
            claude_roots.add(session)
            launches = origins.get(session, [])
            if len(launches) != 1:
                what = "no origin launch" if not launches else f"origin launches {launches}"
                problems.append(f"Claude session {session} ({path}): {what} in the shim log")
    for file in files:
        path = "/" + file.relative_to(transcripts).as_posix()
        m = CLAUDE_SUBAGENT.search(path)
        if m and not CODEX_ROLLOUT.search(path) and m.group(1) not in claude_roots:
            problems.append(f"Claude subagent transcript {path}: no transcript of its session")
    return problems


def _resumes(launch: dict) -> bool:
    """Whether a Codex launch resumes a session: `resume` among its arguments before `--`."""
    # It may also be a prompt or an option's value; that launch then fails closed, as a resume.
    argv = launch.get("argv") or []
    return (
        launch.get("cli") == "codex"
        and "resume" in argv[: argv.index("--") if "--" in argv else None]
    )


def _codex_problems(
    path: str,
    file: Path,
    bound: dict[str, list[int]],
    roots: dict[int, set[str]],
    resumes: set[int],
) -> list[str]:
    first = next(_lines(file), {})
    meta = first.get("payload") if first.get("type") == "session_meta" else None
    if not isinstance(meta, dict) or not isinstance(meta.get("id"), str):
        return [f"Codex rollout {path}: no session_meta with a thread id"]
    thread, launches = meta["id"], bound.get(path, [])
    if not launches:
        return [f"Codex thread {thread} ({path}): no origin launch in the shim log"]
    parent = _find(meta, "parent_thread_id")
    if parent is not None:
        if any(parent in roots.get(n, set()) for n in launches):
            return []
        why = f"its parent thread {parent} is not a root thread of launch {launches[0]}"
        return [f"Codex thread {thread} ({path}): {why}"]
    origins = [n for n in launches if n not in resumes]
    if len(origins) == 1:
        return []
    what = f"origin launches {origins}" if origins else f"no origin launch, only resumes {launches}"
    return [f"Codex thread {thread} ({path}): {what} in the shim log"]
