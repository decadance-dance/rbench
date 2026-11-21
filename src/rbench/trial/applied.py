"""The model and effort each CLI session of a trial requested and the ones that applied, as the CLI
reports them, per launch (spec engine-configurations, Applied model and effort are recorded; spec
run-ledger, Misconfigured configurations; design D5, D7).

`applied_sessions` reads what `hermetic.collect` copies out of one container, the shim log and
every transcript. A session is identified by its origin launch and may have resume launches; each
of its launches is judged against its own request:
- requested: the model and effort the launch's final argv names, read as the shim reads options
  (Claude `--model`, `--effort`; Codex `-m`, `--model`, `-c model=`, and for a review thread
  `-c review_model=`; `-c model_reasoning_effort=`), else, for Codex, the trial's `config.toml`.
  An override the CLI would refuse, or two different values, is an invalid request.
- applied, Claude: the models of the session's own assistant messages, a subagent being a session
  of its own. A message whose id is also in a session that started earlier is copied history (a
  fork's) and not counted (as in `rbench.trial.usage`). The effort is the level of a Stop or
  SubagentStop hook record of that session whose `claude` process is the launch's child.
- applied, Codex: the models and efforts of the thread's `turn_context` records, without the turns
  a fork copied from its parent, and the target of a reroute on the launch's own `exec --json`
  stdout, for a thread that stdout starts.
- A transcript line goes to the session's launch whose run (the collector's time of the launch and
  of its exit) holds the line's timestamp; with one launch and no times, to that launch.

A launch that ran another model than it requested, or a model when it requested none (a default),
or that made an invalid request, makes the trial `misconfigured`, with both models named. An
effort other than the requested one, or none reported (`efforts` null), flags the trial for the
report, as do a model not reported and lines no launch's run holds. Model names and levels are
CLI output: one that is not a plain name is recorded as `INVALID`, and a reason names only models
of `models.toml` and known levels.
"""

import math
import re
import tomllib
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rbench.engines.declaration import EFFORTS
from rbench.engines.pins import MODELS
from rbench.ledger.trials import store_sessions
from rbench.trial.sessions import (
    CLAUDE_ROOT,
    CLAUDE_SUBAGENT,
    CODEX_ROLLOUT,
    ORIGIN_SOURCES,
    STOPS,
    _children,
    _resumes,
)
from rbench.trial.usage import OverrideError, _lines, _read_stdout, option_values, reroutes_of

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/\[\]=-]{0,127}")
INVALID = "<invalid>"
LEVELS = frozenset(level for levels in EFFORTS.values() for level in levels)
UNLISTED = "an unlisted value (see sessions.json)"
REVIEW_SOURCE = {"subagent": "review"}  # Codex 0.171.0's review thread, in its session_meta
# What each setting is read from: argv flags and the `-c` (and `config.toml`) key.
CLAUDE_SETTINGS = {"model": (("--model",), None), "effort": (("--effort",), None)}
CODEX_SETTINGS = {"model": (("-m", "--model"), "model"), "effort": ((), "model_reasoning_effort")}
# Codex's review thread runs `review_model` at the session's effort (design D13).
REVIEW_SETTINGS = CODEX_SETTINGS | {"model": ((), "review_model")}

Runs = Mapping[int, tuple[float, float]]


@dataclass(frozen=True)
class Applied:
    """Per session: `cli`, `session` (a subagent's as `<session>/subagents/<id>`), its origin
    `launch` and `launches`: per launch its `requested` {model, effort} and `applied` {models,
    efforts}, `efforts` null when nothing reported one. `mismatched`: why the trial is
    `misconfigured`; `flagged`: what the report shows next to its scores."""

    sessions: list[dict]
    mismatched: list[str]
    flagged: list[str]


def _name(value: object) -> str:
    """A model name or level as reported, `INVALID` when it is not a plain name."""
    return value if isinstance(value, str) and NAME.fullmatch(value) else INVALID


def _text(value: object) -> str | None:
    """An id as read from a transcript: a string, or None."""
    return value if isinstance(value, str) else None


def _known(value: str, known: Collection[str]) -> str:
    """`value` for a reason the ledger and the report show: a public model id or effort level, or
    a placeholder, since anything else came from a transcript and may hold task data."""
    return value if value in known else UNLISTED


def _time(value: object) -> float | None:
    """A transcript line's ISO timestamp as seconds since the epoch, or None."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


class _Evidence:
    """What one session shows, per launch of it."""

    def __init__(self, launches: list[int], runs: Runs) -> None:
        self.launches = launches
        self.runs = runs
        self.models: dict[int, set[str]] = {n: set() for n in launches}
        self.efforts: dict[int, set[str]] = {n: set() for n in launches}
        self.stray = 0  # lines no launch's run holds

    def launch_of(self, timestamp: object) -> int | None:
        if len(self.launches) == 1 and self.launches[0] not in self.runs:
            return self.launches[0]
        at = _time(timestamp)
        for n in self.launches:
            if at is not None and n in self.runs and self.runs[n][0] <= at <= self.runs[n][1]:
                return n
        return None

    def add(self, kind: str, value: object, timestamp: object) -> None:
        n = self.launch_of(timestamp)
        if n is None:
            self.stray += 1
        else:
            getattr(self, kind)[n].add(_name(value))


def applied_sessions(
    records: list[dict],
    transcripts: Path,
    codex_config: str | None = None,
    codex_stdout: Mapping[int, Path] | None = None,
) -> Applied:
    """The requested and applied model and effort of every session in `transcripts` (container
    paths below that directory), per launch of the shim log `records`. `codex_config` is the
    trial's `config.toml`; `codex_stdout` maps a launch number to the `codex exec --json` stdout
    the runner captured for it."""
    launches = {r["launch"]: r for r in records if r["kind"] == "launch"}
    runs = _runs(records)
    children = _children(records)

    # Claude: session id -> its launches, the origin first; the effort records bound to them.
    claude: dict[str, list[int]] = {}
    for r in records:
        launch = children.get((r.get("claude_pid"), r.get("claude_start")))
        if r["kind"] == "hook" and launch is not None:
            bound = claude.setdefault(r["session_id"], [])
            if launch not in bound:
                bound.insert(0 if r["source"] in ORIGIN_SOURCES else len(bound), launch)
    stops: dict[tuple[str, str | None], list[tuple[int, str]]] = {}
    for r in records:
        launch = children.get((r.get("claude_pid"), r.get("claude_start")))
        if (
            r["kind"] == "effort"
            and r["event"] in STOPS
            and ("agent_id" in r) == (r["event"] == "SubagentStop")
            and r.get("effort") in EFFORTS["claude"]
            and launch in claude.get(r["session_id"], [])
        ):
            stops.setdefault((r["session_id"], r.get("agent_id")), []).append((launch, r["effort"]))

    # Codex: rollout path -> the launches that bound it.
    bound_by: dict[str, list[int]] = {}
    for r in records:
        if r["kind"] == "binding" and r["launch"] not in bound_by.get(r["path"], []):
            bound_by.setdefault(r["path"], []).append(r["launch"])
    reroutes = {n: _reroutes(file) for n, file in (codex_stdout or {}).items()}

    # Regular files only, as in the audit: a planted link could lead outside the trial's snapshot.
    files = sorted(p for p in transcripts.rglob("*.jsonl") if p.is_file() and not p.is_symlink())
    claude_files, rollouts = [], []
    for file in files:
        path = "/" + file.relative_to(transcripts).as_posix()
        lines = _read(file, path)
        if CODEX_ROLLOUT.search(path):
            rollouts.append((path, lines))
        elif m := CLAUDE_ROOT.search(path):
            if any(r.get("sessionId") == m[1] for _, r in lines):  # else a repository file
                claude_files.append((m[1], None, lines))
        elif m := CLAUDE_SUBAGENT.search(path):
            claude_files.append((m[1], file.stem.removeprefix("agent-"), lines))

    config = _config(codex_config)
    sessions = _claude_sessions(claude_files, claude, stops, launches, runs)
    sessions += _codex_sessions(rollouts, bound_by, reroutes, launches, runs, config)
    return _judge(sessions)


def _claude_sessions(
    files: list[tuple[str, str | None, list[tuple[int, dict]]]],
    claude: Mapping[str, list[int]],
    stops: Mapping[tuple[str, str | None], list[tuple[int, str]]],
    launches: Mapping[int, dict],
    runs: Runs,
) -> list[dict]:
    # A message id in several sessions is the one's that started first: the others forked it.
    first = {s: (claude[s][0] if claude.get(s) else math.inf) for s, _, _ in files}
    owner: dict[object, str] = {}
    for session, _, lines in sorted(files, key=lambda f: first[f[0]]):
        for _, r in lines:
            message = r.get("message")
            id_ = _text(message.get("id")) if isinstance(message, dict) else None
            if r.get("type") == "assistant" and id_ is not None:
                owner.setdefault(id_, session)
    sessions = []
    for session, agent, lines in files:
        evidence = _Evidence(claude.get(session, []), runs)
        for _, r in lines:
            message = r.get("message")
            if not (r.get("type") == "assistant" and isinstance(message, dict)):
                continue
            if message.get("model") in (None, "<synthetic>"):
                continue
            if agent is None and owner.get(_text(message.get("id")) or "", session) != session:
                continue  # copied from the session this one forked
            evidence.add("models", message["model"], r.get("timestamp"))
        for launch, level in stops.get((session, agent), []):
            evidence.efforts[launch].add(level)
        name = session if agent is None else f"{session}/subagents/{agent}"
        requests = {
            n: _request(launches[n], "claude", CLAUDE_SETTINGS, {}) for n in evidence.launches
        }
        sessions.append(_entry("claude", name, evidence, requests))
    return sessions


def _codex_sessions(
    rollouts: list[tuple[str, list[tuple[int, dict]]]],
    bound_by: Mapping[str, list[int]],
    reroutes: Mapping[int, tuple[set[str], list[tuple[str, str] | None]]],
    launches: Mapping[int, dict],
    runs: Runs,
    config: Mapping[str, str],
) -> list[dict]:
    threads = {}
    for path, lines in rollouts:
        first = lines[0][1] if lines else {}
        meta = first.get("payload") if first.get("type") == "session_meta" else None
        if isinstance(meta, dict) and isinstance(meta.get("id"), str):
            turns = [
                r for _, r in lines
                if r.get("type") == "turn_context" and isinstance(r.get("payload"), dict)
            ]  # fmt: skip
            threads[path] = (meta, turns)
    turn_ids: dict[str, set] = {}
    for meta, turns in threads.values():
        turn_ids.setdefault(meta["id"], set()).update(
            _text(t["payload"].get("turn_id")) for t in turns
        )
    sessions = []
    for path, (meta, turns) in threads.items():
        bound = bound_by.get(path, [])
        origins = [n for n in bound if not _resumes(launches[n])]
        order = origins[:1] + [n for n in bound if n not in origins[:1]]
        evidence = _Evidence(order, runs)
        inherited = turn_ids.get(_text(meta.get("forked_from_id")), set()) - {None}
        for turn in turns:
            payload = turn["payload"]
            if _text(payload.get("turn_id")) in inherited:
                continue  # copied from the thread this one forked
            if payload.get("model") is not None:
                evidence.add("models", payload["model"], turn.get("timestamp"))
            if payload.get("effort") is not None:
                evidence.add("efforts", payload["effort"], turn.get("timestamp"))
        settings = REVIEW_SETTINGS if meta.get("source") == REVIEW_SOURCE else CODEX_SETTINGS
        requests = {n: _request(launches[n], "codex", settings, config) for n in order}
        for n in order:
            started, found = reroutes.get(n, (set(), []))
            if meta["id"] in started:
                for reroute in found:
                    # Evidence only when it reroutes the model the launch requested.
                    ok = reroute is not None and reroute[0] == requests[n]["model"]
                    evidence.models[n].add(_name(reroute[1]) if ok else INVALID)
        sessions.append(_entry("codex", meta["id"], evidence, requests))
    return sessions


def _entry(cli: str, session: str, evidence: _Evidence, requests: Mapping[int, dict]) -> dict:
    return {
        "cli": cli,
        "session": _name(session),
        "launch": evidence.launches[0] if evidence.launches else None,
        "launches": [
            {
                "launch": n,
                "requested": requests[n],
                "applied": {
                    "models": sorted(evidence.models[n]),
                    "efforts": sorted(evidence.efforts[n]) or None,
                },
            }
            for n in evidence.launches
        ],
        "stray": evidence.stray,
    }


def _judge(sessions: list[dict]) -> Applied:
    mismatched, flagged = [], []
    for i, s in enumerate(sessions):
        what = f"{s['cli']} session {i} of sessions.json"
        if s.pop("stray"):
            flagged.append(f"{what}: transcript lines that no launch's run holds")
        if not s["launches"]:
            flagged.append(f"{what}: no launch in the shim log")
        for entry in s["launches"]:
            where = f"{what}, launch {entry['launch']},"
            want, got = entry["requested"], entry["applied"]
            if INVALID in want.values():
                mismatched.append(f"{where} sets an invalid model or effort on its argv")
                continue
            if not got["models"]:
                flagged.append(f"{where} model not reported")
            for model in got["models"]:
                if model != want["model"]:
                    asked = "no model" if want["model"] is None else _known(want["model"], MODELS)
                    mismatched.append(f"{where} ran {_known(model, MODELS)}; it requested {asked}")
            if got["efforts"] is None:
                flagged.append(f"{where} effort not reported")
            elif want["effort"] is not None:
                for effort in got["efforts"]:
                    if effort != want["effort"]:
                        flagged.append(
                            f"{where} ran effort {_known(effort, LEVELS)}; it requested"
                            f" {_known(want['effort'], LEVELS)}"
                        )
    return Applied(sessions, mismatched, flagged)


def record_applied(trial_dir: Path, applied: Applied) -> None:
    """Write the trial's `sessions.json`."""
    record = {
        "sessions": applied.sessions,
        "misconfigured": applied.mismatched,
        "flagged": applied.flagged,
    }
    store_sessions(Path(trial_dir), record)


def _read(file: Path, path: str) -> list[tuple[int, dict]]:
    try:
        return _lines(file, path)[0]
    except OSError:
        return []


def _runs(records: Iterable[dict]) -> dict[int, tuple[float, float]]:
    """Launch -> the collector's times of the launch and of its exit, when both are logged."""
    start = {r["launch"]: r["time"] for r in records if r["kind"] == "launch" and "time" in r}
    return {
        r["launch"]: (start[r["launch"]], r["time"])
        for r in records
        if r["kind"] == "exit" and "time" in r and r["launch"] in start
    }


def _config(text: str | None) -> dict[str, str]:
    """The `config.toml` settings a Codex request may come from."""
    try:
        data = tomllib.loads(text or "")
    except tomllib.TOMLDecodeError:
        return {}
    keys = ("model", "review_model", "model_reasoning_effort")
    return {k: data[k] for k in keys if isinstance(data.get(k), str)}


def _request(launch: dict, cli: str, settings: Mapping, config: Mapping[str, str]) -> dict:
    """What a launch requested, per setting: None when it names none, `INVALID` when its argv
    sets one the CLI would refuse, or two different values."""
    argv = launch.get("final_argv") or []
    request = {}
    for setting, (flags, key) in settings.items():
        try:
            found = set(option_values(cli, argv, flags, key))
        except OverrideError:
            found = {INVALID}
        if not found and key in config:
            found = {config[key]}
        request[setting] = _name(found.pop()) if len(found) == 1 else INVALID if found else None
    return request


def _reroutes(file: Path) -> tuple[set[str], list[tuple[str, str] | None]]:
    """The threads a launch's stdout starts and the reroutes it shows."""
    events, _ = _read_stdout(file)
    started = {e["thread_id"] for e in events if e.get("type") == "thread.started"}
    return started, reroutes_of(events)
