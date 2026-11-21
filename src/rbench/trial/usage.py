"""Token usage of a trial's CLI sessions, counted from their transcripts (spec review-scoring,
Per-trial metrics and Token usage counted once; design D7).

`count_usage` reads what `hermetic.collect` copies out of one container, the shim log and every
transcript, and sums usage per model over all sessions, subagents and side calls included, as the
ledger's uncached input, output, cache read and cache creation:
- Claude: every assistant message of every transcript, counted once by `message.id` (a transcript
  repeats a message's usage on each of its content blocks, and a fork copies its parent's
  messages), under `message.model`. Messages the CLI makes itself (model `<synthetic>`) are skipped.
- Codex, per rollout: its `token_usage_record` entries, counted once by thread, turn and response
  id, under the model of the turn's `turn_context`. Without them, the last `token_count` snapshot,
  only for a thread that starts from zero, never resets and runs one model. For a bound rollout
  that is missing or unreadable, the last `turn.completed.usage` on the stdout of the thread's last
  launch, only for a thread that starts from zero and whose launches name one model.

The figure is flagged incomplete, with a reason per gap, when a session launch in the shim log has
no session with usage, when a rollout's usage cannot be recovered, or when a response's model is in
doubt. The engine and the normalization step (#45) run in separate projects, so each counts its own
collection; `usage_record` puts both into the trial's `usage.json`, apart.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from rbench.ledger.trials import USAGE_KINDS
from rbench.trial.sessions import CLAUDE_ROOT, CLAUDE_SUBAGENT, CODEX_ROLLOUT, _children

Counts = dict[str, int]


class UsageError(ValueError):
    """A transcript record whose usage is malformed."""


@dataclass(frozen=True)
class Usage:
    """Usage per model ({"input", "output", "cache_read", "cache_creation"}) and why it is
    incomplete; `incomplete` is empty when every session's usage was recovered."""

    models: dict[str, Counts]
    incomplete: list[str]


NONE = Usage({}, [])


def usage_record(engine: Usage, normalization: Usage = NONE) -> dict:
    """The trial's `usage.json`: the engine's usage and the normalization step's, apart."""
    kinds = {"engine": engine, "normalization": normalization}
    return {k: kinds[k].models for k in USAGE_KINDS} | {
        "incomplete": {k: kinds[k].incomplete for k in USAGE_KINDS}
    }


def count_usage(
    records: list[dict], transcripts: Path, codex_stdout: Mapping[int, Path] | None = None
) -> Usage:
    """The usage of every session in `transcripts` (container paths below that directory), checked
    against the shim log `records`. `codex_stdout` maps a launch number to the `codex exec --json`
    stdout the runner captured for it, the last fallback for a rollout that is missing."""
    incomplete: list[str] = []
    seen: dict[tuple, tuple[str, Counts]] = {}  # what is counted once -> (model, counts)
    used: set[str] = set()  # Claude session ids and Codex rollout paths with usage
    read: set[str] = set()  # Codex rollouts that could be read
    for file in sorted(p for p in transcripts.rglob("*.jsonl") if p.is_file()):
        path = "/" + file.relative_to(transcripts).as_posix()
        if CODEX_ROLLOUT.search(path):
            found = _rollout(file, path, seen, incomplete)
            if found is not None:
                read.add(path)
            if found:
                used.add(path)
        elif (m := CLAUDE_ROOT.search(path) or CLAUDE_SUBAGENT.search(path)) and _claude(
            file, path, m.group(1), CLAUDE_ROOT.search(path) is None, seen, incomplete
        ):
            used.add(m.group(1))

    launches = {r["launch"]: r for r in records if r["kind"] == "launch"}
    bindings = [r for r in records if r["kind"] == "binding"]
    for path in sorted({b["path"] for b in bindings} - read):
        why = _from_stdout(path, bindings, launches, codex_stdout or {}, seen)
        if why is None:
            used.add(path)
        else:
            incomplete.append(f"Codex rollout {path}: missing or unreadable, and {why}")

    sessions: dict[int | None, list[str]] = {}
    children = _children(records)
    for r in records:
        if r["kind"] == "hook":
            launch = children.get((r.get("claude_pid"), r.get("claude_start")))
            sessions.setdefault(launch, []).append(r.get("session_id"))
        elif r["kind"] == "binding":
            sessions.setdefault(r["launch"], []).append(r["path"])
    for n, launch in sorted(launches.items()):
        if not launch.get("added"):  # the shims add flags to session launches only (D5)
            continue
        where = f"Launch {n} ({launch.get('cli')})"
        bound = sessions.get(n, [])
        if not bound:
            incomplete.append(f"{where}: no session in the shim log")
        for session in dict.fromkeys(bound):
            if session not in used:
                incomplete.append(f"{where}: no usage for its session {session}")

    models: dict[str, Counts] = {}
    for model, counts in seen.values():
        total = models.setdefault(model, dict.fromkeys(counts, 0))
        for k, v in counts.items():
            total[k] += v
    return Usage(dict(sorted(models.items())), list(dict.fromkeys(incomplete)))


def _lines(file: Path, path: str) -> tuple[list[tuple[int, dict]], list[str]]:
    """The JSON object lines of a transcript with their numbers, and a reason per line that is not
    one (a process killed mid-write leaves a partial line)."""
    lines, bad = [], []
    with file.open(encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            if isinstance(record, dict):
                lines.append((n, record))
            elif line.strip():
                bad.append(f"{path}:{n}: not a JSON record, so its usage may be missing")
    return lines, bad


def _add(seen: dict, key: tuple, model: str, counts: Counts, where: str) -> None:
    """Count `counts` once under `key`; a repeat carries the same usage or a later, larger
    snapshot of it."""
    if key in seen:
        before, old = seen[key]
        if before != model:
            raise UsageError(f"{where}: {key[-1]} was counted for model {before!r}, now {model!r}")
        counts = {k: max(v, old[k]) for k, v in counts.items()}
    seen[key] = (model, counts)


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _claude(
    file: Path, path: str, session: str, subagent: bool, seen: dict, incomplete: list[str]
) -> bool:
    """Count a Claude transcript's messages; whether it holds any usage."""
    try:
        lines, bad = _lines(file, path)
    except OSError as e:
        incomplete.append(f"Claude transcript {path}: unreadable: {e}")
        return False
    if not subagent and not any(r.get("sessionId") == session for _, r in lines):
        return False  # a file in a reviewed repository, not a transcript
    incomplete += bad
    found = False
    for n, r in lines:
        message = r.get("message")
        if r.get("type") != "assistant" or not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if usage is None or message.get("model") == "<synthetic>":
            continue
        where = f"{path}:{n}"
        if not (_text(message.get("id")) and _text(message.get("model"))):
            raise UsageError(f"{where}: a message with usage needs an id and a model")
        counts = _claude_counts(usage)
        if counts is None:
            raise UsageError(f"{where}: message {message['id']}: malformed usage")
        _add(seen, ("claude", message["id"]), message["model"], counts, where)
        found = True
    return found


def _claude_counts(usage: object) -> Counts | None:
    if not isinstance(usage, dict):
        return None
    counts = {
        "input": usage.get("input_tokens"),
        "output": usage.get("output_tokens"),
        "cache_read": usage.get("cache_read_input_tokens") or 0,
        "cache_creation": usage.get("cache_creation_input_tokens") or 0,
    }
    return counts if all(_count(v) for v in counts.values()) else None


def _codex_counts(usage: object) -> Counts | None:
    """Codex's counters as the ledger's: `cached_input_tokens` is part of `input_tokens`, and
    `cache_write_input_tokens` counts as cache creation (provisional, D7)."""
    if not isinstance(usage, dict):
        return None
    total, cached = usage.get("input_tokens"), usage.get("cached_input_tokens")
    output, written = usage.get("output_tokens"), usage.get("cache_write_input_tokens", 0)
    if not (all(_count(v) for v in (total, cached, output, written)) and cached <= total):
        return None
    return {
        "input": total - cached,
        "output": output,
        "cache_read": cached,
        "cache_creation": written,
    }


def _rollout(file: Path, path: str, seen: dict, incomplete: list[str]) -> bool | None:
    """Count a Codex rollout; whether it holds usage, or None when it cannot be read as one."""
    try:
        lines, bad = _lines(file, path)
    except OSError:
        return None
    meta = (
        lines[0][1].get("payload") if lines and lines[0][1].get("type") == "session_meta" else None
    )
    if not (isinstance(meta, dict) and _text(meta.get("id"))):
        return None
    incomplete += bad
    thread = meta["id"]
    where = f"Codex thread {thread} ({path})"
    models: dict[str, str] = {}  # turn id -> the model its turn_context names
    responses: list[tuple[tuple, str, Counts, str]] = []
    snapshots: list[object] = []
    rerouted = False
    for n, r in lines:
        payload = r.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type") if r.get("type") == "event_msg" else r.get("type")
        if kind == "turn_context" and _text(payload.get("turn_id")) and _text(payload.get("model")):
            models[payload["turn_id"]] = payload["model"]
        elif kind == "token_usage_record":
            key = tuple(payload.get(k) for k in ("thread_id", "turn_id", "response_id"))
            counts = _codex_counts(payload.get("usage"))
            if not all(_text(k) for k in key) or counts is None:
                raise UsageError(f"{path}:{n}: malformed token_usage_record")
            responses.append((("codex", *key), payload["turn_id"], counts, f"{path}:{n}"))
        elif kind == "token_count" and isinstance(payload.get("info"), dict):
            snapshots.append(payload["info"].get("total_token_usage"))
        elif kind == "model_reroute":
            rerouted = True
    if rerouted and (responses or snapshots):
        incomplete.append(
            f"{where}: a request was rerouted to another model, so models are in doubt"
        )
    if responses:
        for key, turn, counts, at in responses:
            if turn in models:
                _add(seen, key, models[turn], counts, at)
            else:
                incomplete.append(f"{at}: no turn_context names the model of turn {turn}")
        return True
    if not snapshots:
        return False
    why = _snapshot_doubt(meta, snapshots, set(models.values()))
    if why is not None:
        incomplete.append(f"{where}: no token_usage_record, and its token_count total {why}")
        return False
    _add(seen, ("thread", thread), models.popitem()[1], _codex_counts(snapshots[-1]), where)
    return True


def _snapshot_doubt(meta: dict, snapshots: list[object], models: set[str]) -> str | None:
    """Why a thread's last cumulative `token_count` is not its own usage, or None (D7)."""
    if meta.get("forked_from_id") is not None:
        return "includes its parent's usage (a fork)"
    counts = [_codex_counts(s) for s in snapshots]
    if any(c is None for c in counts):
        return "has invalid counters"
    if any(after[k] < before[k] for before, after in pairwise(counts) for k in after):
        return "was reset"
    if len(models) != 1:
        return f"covers {len(models)} models"
    return None


def _from_stdout(
    path: str,
    bindings: list[dict],
    launches: Mapping[int, dict],
    stdout: Mapping[int, Path],
    seen: dict,
) -> str | None:
    """Count a missing rollout's thread from its last launch's stdout; why it cannot, or None."""
    meta = next((b.get("meta") for b in reversed(bindings) if b["path"] == path), None) or {}
    thread = meta.get("id")
    if not _text(thread):
        return "its binding names no thread"
    if meta.get("forked_from_id") is not None:
        return "its stdout total includes its parent's usage (a fork)"
    own = sorted({b["launch"] for b in bindings if (b.get("meta") or {}).get("id") == thread})
    last = own[-1]
    if last not in stdout:
        return f"no stdout of its last launch {last}"
    events = _stdout_events(stdout[last])
    if not any(e.get("type") == "thread.started" and e.get("thread_id") == thread for e in events):
        return f"the stdout of launch {last} is not thread {thread}'s"
    usage = [e.get("usage") for e in events if e.get("type") == "turn.completed"]
    counts = _codex_counts(usage[-1]) if usage else None
    if counts is None:
        return f"the stdout of launch {last} has no valid turn.completed usage"
    models = {_argv_model(launches.get(n, {}).get("final_argv") or []) for n in own}
    if len(models) != 1 or None in models:
        return "its launches do not name one model"
    _add(seen, ("thread", thread), models.pop(), counts, f"launch {last} stdout")
    return None


def _stdout_events(file: Path) -> list[dict]:
    try:
        text = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _argv_model(argv: Iterable[str]) -> str | None:
    """The one model a Codex argv sets (`-m`, `--model`, `-c model=...`) before `--`, or None."""
    argv = list(argv)
    found = set()
    for i, a in enumerate(argv):
        if a == "--":
            break
        value = argv[i + 1] if i + 1 < len(argv) else ""
        if a in ("-m", "--model"):
            found.add(value)
        elif a.startswith("--model="):
            found.add(a.split("=", 1)[1])
        elif a in ("-c", "--config") and value.split("=", 1)[0].strip() == "model":
            setting = value.split("=", 1)[1].strip()
            # limit: a TOML basic or literal string, or a bare word, as Codex's own `-c` takes it
            try:
                found.add(json.loads(setting) if setting.startswith('"') else setting.strip("'"))
            except ValueError:
                found.add(setting)
    return found.pop() if len(found) == 1 and _text(next(iter(found))) else None
