"""Token usage of a trial's CLI sessions, counted from their transcripts (spec review-scoring,
Per-trial metrics and Token usage counted once; design D7).

`count_usage` reads what `hermetic.collect` copies out of one container, the shim log and every
transcript, and sums usage per model over all sessions, subagents and side calls included, as the
ledger's uncached input, output, cache read and cache creation:
- Claude: every assistant message of every transcript, counted once by `message.id` (a transcript
  repeats a message's usage on each of its content blocks, and a fork copies its parent's
  messages), under `message.model`. Messages the CLI makes itself (model `<synthetic>`) are skipped.
- Codex, per thread: the `token_usage_record` entries of every rollout, counted once by thread,
  turn and response id and credited to the thread they name, under the model of the turn's
  `turn_context`. A thread with no such record anywhere falls back to its rollout's last
  `token_count` total, only when it starts from zero, never resets and runs one model. A bound
  rollout that is missing or unreadable falls back to the last `turn.completed.usage` on the stdout
  of its thread's last launch, under the same conditions, with the model its launches name.
- Codex's rollout does not record a model reroute; only a launch's stdout shows one, and only for
  the threads it starts. A Codex thread's usage has a verified model only when a captured stdout
  that reads whole, with no reroute, shows that thread.

The figure is flagged incomplete, with a reason per gap, when a session launch in the shim log has
no session with usage of its own, when a thread's usage cannot be recovered, or when a response's
model is in doubt. Malformed usage in a record that is read raises `UsageError`, naming the file and
line. The engine and the normalization step (#45) run in separate projects, so each counts its own
collection; `usage_record` puts both into the trial's `usage.json`, apart.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

from rbench.ledger.trials import USAGE_KINDS, no_repeated_keys
from rbench.trial.sessions import CLAUDE_ROOT, CLAUDE_SUBAGENT, CODEX_ROLLOUT, _children

Counts = dict[str, int]
# Counts and totals stay exact in any JSON reader (an IEEE double's integer range).
LIMIT = 2**53
REROUTE = "model rerouted: "  # Codex 0.171.0 `exec --json`: an error item with this message


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


@dataclass
class _Rollout:
    path: str
    thread: str
    forked: bool
    models: dict[str, set[str]] = field(default_factory=dict)  # turn id -> turn_context models
    responses: list[tuple[tuple, str, Counts, str]] = field(default_factory=list)
    snapshots: list[tuple[object, str]] = field(default_factory=list)  # (token_count info, where)


def count_usage(
    records: list[dict], transcripts: Path, codex_stdout: Mapping[int, Path] | None = None
) -> Usage:
    """The usage of every session in `transcripts` (container paths below that directory), checked
    against the shim log `records`. `codex_stdout` maps a launch number to the `codex exec --json`
    stdout the runner captured for it: the evidence of the model a launch ran, and the last
    fallback for a rollout that is missing."""
    stdout = {n: _read_stdout(path) for n, path in (codex_stdout or {}).items()}
    incomplete: list[str] = []
    seen: dict[tuple, tuple[str, Counts]] = {}  # what is counted once -> (model, counts)
    used: set[str] = set()  # Claude session ids and Codex threads with usage of their own
    rollouts: list[_Rollout] = []
    for file in sorted(p for p in transcripts.rglob("*.jsonl") if p.is_file()):
        path = "/" + file.relative_to(transcripts).as_posix()
        if CODEX_ROLLOUT.search(path):
            if (rollout := _rollout(file, path, incomplete)) is not None:
                rollouts.append(rollout)
        elif (m := CLAUDE_ROOT.search(path) or CLAUDE_SUBAGENT.search(path)) and _claude(
            file, path, m.group(1), CLAUDE_ROOT.search(path) is None, seen, incomplete
        ):
            used.add(m.group(1))
    used |= _codex(rollouts, seen, incomplete)

    launches = {r["launch"]: r for r in records if r["kind"] == "launch"}
    bindings = [r for r in records if r["kind"] == "binding"]
    threads = {b["path"]: (b.get("meta") or {}).get("id") for b in bindings}
    threads |= {r.path: r.thread for r in rollouts}
    read = {r.path for r in rollouts}
    for path in sorted({b["path"] for b in bindings} - read):
        if threads[path] in used:  # its usage is recorded in another rollout
            continue
        why = _from_stdout(path, bindings, launches, stdout, seen)
        if why is None:
            used.add(threads[path])
        else:
            incomplete.append(f"Codex rollout {path}: missing or unreadable, and {why}")

    sessions: dict[int | None, list[str]] = {}
    verified: set[str] = set()  # Codex threads a clean stdout shows, with no reroute
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
            if threads.get(session, session) not in used:
                incomplete.append(f"{where}: no usage of its own for its session {session}")
        if launch.get("cli") == "codex" and n in stdout:
            events, problem = stdout[n]
            reroutes = [
                f"{where}: {e['item']['message']}"
                for e in events
                if e.get("type") == "item.completed"
                and isinstance(e.get("item"), dict)
                and str(e["item"].get("message", "")).startswith(REROUTE)
            ]
            if problem:
                incomplete.append(
                    f"{where}: its stdout {problem}, so the models it shows are unverified"
                )
            incomplete += reroutes
            if not (problem or reroutes):
                verified |= {
                    e.get("thread_id") for e in events if e.get("type") == "thread.started"
                }
    # The rollout cannot show a reroute (Codex 0.171.0 does not persist one), and `exec --json`
    # shows only its primary thread's: a thread no clean stdout shows has an unverified model (D7).
    for thread in sorted({key[1] for key in seen if key[0] in ("codex", "thread")} - verified):
        incomplete.append(
            f"Codex thread {thread}: no clean stdout of a launch shows it, so the model its usage"
            " ran on is unverified"
        )

    models: dict[str, Counts] = {}
    for model, counts in seen.values():
        total = models.setdefault(model, dict.fromkeys(counts, 0))
        for k, v in counts.items():
            total[k] += v
            if total[k] >= LIMIT:
                raise UsageError(f"model {model!r}: the {k} total reaches 2**53")
    return Usage(dict(sorted(models.items())), list(dict.fromkeys(incomplete)))


def _lines(file: Path, path: str) -> tuple[list[tuple[int, dict]], list[str]]:
    """The JSON object lines of a transcript with their numbers, and a reason per line that is not
    one: a process killed mid-write leaves a partial line, and a repeated key is ambiguous."""
    lines, bad = [], []
    with file.open(encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            try:
                record = json.loads(line, object_pairs_hook=no_repeated_keys)
            except (ValueError, RecursionError) as e:
                if line.strip():
                    bad.append(
                        f"{path}:{n}: not a valid JSON record ({e}), so usage may be missing"
                    )
                continue
            if isinstance(record, dict):
                lines.append((n, record))
            else:
                bad.append(f"{path}:{n}: not a JSON object, so usage may be missing")
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


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _number(usage: dict, key: str, where: str, optional: bool = False) -> int:
    """A token counter; an optional one that is absent or null is 0, any other value must be an
    integer from 0 below 2**53."""
    value = usage.get(key)
    if optional and value is None:
        return 0
    if type(value) is not int or not 0 <= value < LIMIT:
        raise UsageError(f"{where}: {key}: expected a token count from 0 below 2**53")
    return value


def _claude_counts(usage: object, where: str) -> Counts:
    if not isinstance(usage, dict):
        raise UsageError(f"{where}: usage: expected an object")
    return {
        "input": _number(usage, "input_tokens", where),
        "output": _number(usage, "output_tokens", where),
        "cache_read": _number(usage, "cache_read_input_tokens", where, optional=True),
        "cache_creation": _number(usage, "cache_creation_input_tokens", where, optional=True),
    }


def _codex_counts(usage: object, where: str) -> Counts:
    """Codex's counters as the ledger's: `cached_input_tokens` is part of `input_tokens`, and
    `cache_write_input_tokens` counts as cache creation (provisional, D7)."""
    if not isinstance(usage, dict):
        raise UsageError(f"{where}: usage: expected an object")
    total = _number(usage, "input_tokens", where)
    cached = _number(usage, "cached_input_tokens", where)
    if cached > total:
        raise UsageError(f"{where}: cached_input_tokens exceeds input_tokens")
    return {
        "input": total - cached,
        "output": _number(usage, "output_tokens", where),
        "cache_read": cached,
        "cache_creation": _number(usage, "cache_write_input_tokens", where, optional=True),
    }


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
        if r.get("type") != "assistant":
            continue
        where = f"{path}:{n}"
        message = r.get("message")
        if not isinstance(message, dict):
            raise UsageError(f"{where}: an assistant record needs a message object")
        if message.get("usage") is None or message.get("model") == "<synthetic>":
            continue
        if not (_text(message.get("id")) and _text(message.get("model"))):
            raise UsageError(f"{where}: a message with usage needs an id and a model")
        counts = _claude_counts(message["usage"], f"{where}: message {message['id']}")
        _add(seen, ("claude", message["id"]), message["model"], counts, where)
        found = True
    return found


def _rollout(file: Path, path: str, incomplete: list[str]) -> _Rollout | None:
    """A Codex rollout's records, or None when it cannot be read as one."""
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
    rollout = _Rollout(path, meta["id"], meta.get("forked_from_id") is not None)
    for n, r in lines:
        where, kind, payload = f"{path}:{n}", r.get("type"), r.get("payload")
        if kind in ("turn_context", "token_usage_record") and not isinstance(payload, dict):
            raise UsageError(f"{where}: {kind}: expected a payload object")
        if kind == "turn_context":
            if not (_text(payload.get("turn_id")) and _text(payload.get("model"))):
                raise UsageError(f"{where}: turn_context: expected a turn id and a model")
            rollout.models.setdefault(payload["turn_id"], set()).add(payload["model"])
        elif kind == "token_usage_record":
            key = tuple(payload.get(k) for k in ("thread_id", "turn_id", "response_id"))
            if not all(_text(k) for k in key):
                raise UsageError(
                    f"{where}: token_usage_record: expected thread, turn and response ids"
                )
            counts = _codex_counts(payload.get("usage"), f"{where}: token_usage_record")
            rollout.responses.append((key, payload["turn_id"], counts, where))
        elif (
            kind == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "token_count"
            and payload.get("info") is not None  # null: a rate-limit update only
        ):
            rollout.snapshots.append((payload["info"], where))
    return rollout


def _codex(rollouts: list[_Rollout], seen: dict, incomplete: list[str]) -> set[str]:
    """Count the Codex threads' usage; the threads that have usage of their own."""
    models: dict[str, set[str]] = {}  # turn id -> the models turn_context names for it
    for rollout in rollouts:
        for turn, named in rollout.models.items():
            models.setdefault(turn, set()).update(named)
    owners = {key[0] for r in rollouts for key, *_ in r.responses}
    covered = set()
    for rollout in rollouts:
        for key, turn, counts, where in rollout.responses:
            named = models.get(turn, set())
            if len(named) != 1:
                incomplete.append(f"{where}: turn {turn} has {len(named)} turn_context models")
                continue
            _add(seen, ("codex", *key), next(iter(named)), counts, where)
            covered.add(key[0])
    for rollout in rollouts:
        if rollout.thread in owners or not rollout.snapshots:
            continue
        where = f"Codex thread {rollout.thread} ({rollout.path})"
        try:
            why = _snapshot_doubt(rollout)
        except UsageError as e:
            why = f"is malformed ({e})"
        if why is None:
            model = next(iter(set().union(*rollout.models.values())))
            info, at = rollout.snapshots[-1]
            counts = _codex_counts(info["total_token_usage"], at)
            _add(seen, ("thread", rollout.thread), model, counts, where)
            covered.add(rollout.thread)
        else:
            incomplete.append(f"{where}: no token_usage_record, and its token_count total {why}")
    return covered


def _snapshot_doubt(rollout: _Rollout) -> str | None:
    """Why a thread's last cumulative `token_count` is not its own usage, or None (D7)."""
    if rollout.forked:
        return "includes its parent's usage (a fork)"
    for info, where in rollout.snapshots:
        if not isinstance(info, dict):
            raise UsageError(f"{where}: token_count info: expected an object")
    counts = [_codex_counts(info["total_token_usage"], at) for info, at in rollout.snapshots]
    if any(after[k] < before[k] for before, after in pairwise(counts) for k in after):
        return "was reset"
    models = set().union(*rollout.models.values())
    if len(models) != 1:
        return f"covers {len(models)} models"
    return None


def _from_stdout(
    path: str,
    bindings: list[dict],
    launches: Mapping[int, dict],
    stdout: Mapping[int, tuple[list[dict], str | None]],
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
    events, problem = stdout[last]
    if problem:
        return f"the stdout of launch {last} {problem}"
    if not any(e.get("type") == "thread.started" and e.get("thread_id") == thread for e in events):
        return f"the stdout of launch {last} is not thread {thread}'s"
    usage = [e.get("usage") for e in events if e.get("type") == "turn.completed"]
    if not usage:
        return f"the stdout of launch {last} has no turn.completed usage"
    try:
        counts = _codex_counts(usage[-1], f"the stdout of launch {last}: turn.completed")
    except UsageError as e:
        return str(e)
    if not any(counts.values()):
        # Codex 0.171.0 reports all zeros when it never received a token count.
        return f"the stdout of launch {last} reports no usage (all counters zero)"
    models = {_argv_model(launches.get(n, {}).get("final_argv") or []) for n in own}
    if len(models) != 1 or None in models:
        return "its launches do not name one model"
    _add(seen, ("thread", thread), models.pop(), counts, f"launch {last} stdout")
    return None


def _read_stdout(file: Path) -> tuple[list[dict], str | None]:
    """A `codex exec --json` stdout's events, and why it cannot be trusted whole, or None."""
    try:
        text = file.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [], f"is unreadable ({e.strerror})"
    events = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line, object_pairs_hook=no_repeated_keys)
        except (ValueError, RecursionError) as e:
            return events, f"line {n} is not a valid JSON record ({e})"
        if not isinstance(event, dict):
            return events, f"line {n} is not a JSON object"
        events.append(event)
    return events, None if events else "is empty"


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
