"""The model and effort each CLI session of a trial requested and the ones that applied, as the CLI
reports them (spec engine-configurations, Applied model and effort are recorded; spec run-ledger,
Misconfigured configurations; design D5, D7).

`applied_sessions` reads what `hermetic.collect` copies out of one container, the shim log and
every transcript, and gives one entry per session:
- requested: the model and effort its launch names on the argv the CLI got (Claude `--model`,
  `--effort`; Codex `-m`, `--model`, `-c model=`, `-c model_reasoning_effort=`), which the shim
  adds for roles delivered by flag, else, for Codex, the trial's `config.toml`. The adapters'
  `check` holds those argvs to the plan.
- applied, Claude: the models of the session's assistant messages in its transcript, a subagent
  being a session of its own, and the effort levels the image's Stop and SubagentStop hook logged
  for it (Claude Code 2.1.316 reports the applied effort only there).
- applied, Codex: the models and efforts of the thread's `turn_context` records, and the model a
  reroute on its launch's `exec --json` stdout names (the rollout does not record a reroute, D7).

A session that ran another model than it requested, a Codex reroute included, makes the trial
`misconfigured` with both models named (in a reason, only a model of `models.toml`); one with no requested model ran a default one, so it
does too. An effort that differs from the requested one, or that no record reports, flags the
trial for the report. The model names and levels are CLI output: one that is not a plain name is
recorded as `INVALID`, which no request matches.
"""

import re
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

from rbench.atomic import atomic_write
from rbench.engines.declaration import EFFORTS
from rbench.engines.pins import MODELS
from rbench.ledger.trials import SESSIONS_FILE, dump_json
from rbench.trial.sessions import (
    CLAUDE_ROOT,
    CLAUDE_SUBAGENT,
    CODEX_ROLLOUT,
    ORIGIN_SOURCES,
    _children,
    _resumes,
)
from rbench.trial.usage import REROUTE, _lines, _read_stdout, argv_setting

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/\[\]=-]{0,127}")
INVALID = "<invalid>"
# Codex 0.171.0 `exec --json`: "model rerouted: <from> -> <to> (<reason>)".
_REROUTED = re.compile(re.escape(REROUTE) + r"(\S+) -> (\S+)")
CLAUDE_FLAGS = {"model": (("--model",), None), "effort": (("--effort",), None)}
CODEX_FLAGS = {"model": (("-m", "--model"), "model"), "effort": ((), "model_reasoning_effort")}
CONFIG_KEYS = {"model": "model", "effort": "model_reasoning_effort"}
LEVELS = frozenset(level for levels in EFFORTS.values() for level in levels)


@dataclass(frozen=True)
class Applied:
    """Per session: `cli`, `session` (a subagent's as `<session>/subagents/<id>`), `launch`, `requested` {model, effort} and `applied`
    {models, efforts}. `mismatched`: why the trial is `misconfigured`; `flagged`: the effort
    drifts the report shows."""

    sessions: list[dict]
    mismatched: list[str]
    flagged: list[str]


def _name(value: object) -> str:
    """A model name or level as reported, `INVALID` when it is not a plain name."""
    return value if isinstance(value, str) and NAME.fullmatch(value) else INVALID


def applied_sessions(
    records: list[dict],
    transcripts: Path,
    codex_config: str | None = None,
    codex_stdout: Mapping[int, Path] | None = None,
) -> Applied:
    """The requested and applied model and effort of every session in `transcripts` (container
    paths below that directory), bound to its launch through the shim log `records`.
    `codex_config` is the trial's `config.toml`; `codex_stdout` maps a launch number to the
    `codex exec --json` stdout the runner captured for it."""
    launches = {r["launch"]: r for r in records if r["kind"] == "launch"}
    children = _children(records)
    origins: dict[str, int] = {}  # Claude session id -> its origin launch
    for r in records:
        if r["kind"] == "hook" and r["source"] in ORIGIN_SOURCES:
            launch = children.get((r.get("claude_pid"), r.get("claude_start")))
            if launch is not None:
                origins.setdefault(r["session_id"], launch)
    efforts: dict[tuple[str, str | None], list[str]] = {}
    for r in records:
        if r["kind"] == "effort" and "effort" in r:
            efforts.setdefault((r["session_id"], r.get("agent_id")), []).append(r["effort"])
    bound: dict[str, list[int]] = {}
    for r in records:
        if r["kind"] == "binding":
            bound.setdefault(r["path"], []).append(r["launch"])
    reroutes = _reroutes(codex_stdout or {})
    config = _config(codex_config)

    sessions = []
    # Regular files only, as in the audit: a planted link could lead outside the trial's snapshot.
    files = sorted(p for p in transcripts.rglob("*.jsonl") if p.is_file() and not p.is_symlink())
    for file in files:
        path = "/" + file.relative_to(transcripts).as_posix()
        if CODEX_ROLLOUT.search(path):
            launch = _origin(bound.get(path, []), launches)
            entry = _codex(file, path, launch, reroutes)
            if entry is not None:
                requested = _requested(launches.get(launch), CODEX_FLAGS, config)
                sessions.append(entry | {"launch": launch, "requested": requested})
        elif m := CLAUDE_ROOT.search(path) or CLAUDE_SUBAGENT.search(path):
            session, agent = m.group(1), None
            if CLAUDE_ROOT.search(path) is None:
                agent = file.stem.removeprefix("agent-")
            lines, _ = _read(file, path)
            if agent is None and not any(r.get("sessionId") == session for _, r in lines):
                continue  # a file in a reviewed repository, not a transcript
            launch = origins.get(session)
            models = {
                _name(r["message"].get("model"))
                for _, r in lines
                if r.get("type") == "assistant"
                and isinstance(r.get("message"), dict)
                and r["message"].get("model") not in (None, "<synthetic>")
            }
            levels = {_name(e) for e in efforts.get((session, agent), [])}
            sessions.append({
                "cli": "claude",
                "session": _name(session if agent is None else f"{session}/subagents/{agent}"),
                "launch": launch,
                "requested": _requested(launches.get(launch), CLAUDE_FLAGS, {}),
                "applied": {"models": sorted(models), "efforts": sorted(levels)},
            })  # fmt: skip

    mismatched, flagged = [], []
    for n, s in enumerate(sessions):
        where = f"{s['cli']} session {n} of sessions.json (launch {s['launch']})"
        want, got = s["requested"], s["applied"]
        if not got["models"]:
            flagged.append(f"{where}: model not reported")
        for model in got["models"]:
            if model != want["model"]:
                asked = "no model" if want["model"] is None else _known(want["model"], MODELS)
                mismatched.append(f"{where} ran {_known(model, MODELS)}; it requested {asked}")
        if not got["efforts"]:
            flagged.append(f"{where}: effort not reported")
        elif want["effort"] is not None:
            for effort in got["efforts"]:
                if effort != want["effort"]:
                    flagged.append(
                        f"{where} ran effort {_known(effort, LEVELS)}; it requested"
                        f" {_known(want['effort'], LEVELS)}"
                    )
    return Applied(sessions, mismatched, flagged)


def _known(value: str, known: Collection[str]) -> str:
    """`value` for a reason the ledger and the report show: a public model id or effort level, or
    a placeholder, since anything else came from a transcript and may hold task data."""
    return value if value in known else "an unlisted value (see sessions.json)"


def record_applied(trial_dir: Path, applied: Applied) -> None:
    """Write the trial's `sessions.json`."""
    record = {
        "sessions": applied.sessions,
        "misconfigured": applied.mismatched,
        "flagged": applied.flagged,
    }
    atomic_write(Path(trial_dir) / SESSIONS_FILE, dump_json(record))


def _read(file: Path, path: str) -> tuple[list[tuple[int, dict]], list[str]]:
    try:
        return _lines(file, path)
    except OSError:
        return [], []


def _origin(launches: list[int], records: Mapping[int, dict]) -> int | None:
    """The launch that started a rollout's thread: the first that bound it without resuming."""
    origins = [n for n in launches if not _resumes(records.get(n, {}))]
    return (origins or launches or [None])[0]


def _config(text: str | None) -> dict[str, str]:
    """The model and effort the trial's `config.toml` sets."""
    try:
        data = tomllib.loads(text or "")
    except tomllib.TOMLDecodeError:
        return {}
    return {k: data[v] for k, v in CONFIG_KEYS.items() if isinstance(data.get(v), str)}


def _requested(launch: dict | None, flags: dict, config: Mapping[str, str]) -> dict:
    argv = (launch or {}).get("final_argv") or []
    values = {k: argv_setting(argv, *v) or config.get(k) for k, v in flags.items()}
    return {k: None if v is None else _name(v) for k, v in values.items()}


def _reroutes(stdout: Mapping[int, Path]) -> dict[int, tuple[set[str], list[str]]]:
    """Per launch: the threads its stdout starts and the models its reroutes name."""
    result = {}
    for n, file in stdout.items():
        events, _ = _read_stdout(file)
        threads = {e["thread_id"] for e in events if e.get("type") == "thread.started"}
        models = []
        for e in events:
            item = e.get("item") if e.get("type") == "item.completed" else None
            message = item.get("message") if isinstance(item, dict) else None
            if isinstance(message, str) and message.startswith(REROUTE):
                m = _REROUTED.match(message)
                models.append(_name(m.group(2)) if m else INVALID)
        if models:
            result[n] = (threads, models)
    return result


def _codex(
    file: Path, path: str, launch: int | None, reroutes: Mapping[int, tuple[set[str], list[str]]]
) -> dict | None:
    lines, _ = _read(file, path)
    meta = (
        lines[0][1].get("payload") if lines and lines[0][1].get("type") == "session_meta" else None
    )
    if not (isinstance(meta, dict) and isinstance(meta.get("id"), str)):
        return None
    thread = meta["id"]
    models, efforts = set(), set()
    for _, r in lines:
        payload = r.get("payload")
        if r.get("type") == "turn_context" and isinstance(payload, dict):
            if payload.get("model") is not None:
                models.add(_name(payload["model"]))
            if payload.get("effort") is not None:
                efforts.add(_name(payload["effort"]))
    threads, rerouted = reroutes.get(launch, (set(), [])) if launch is not None else (set(), [])
    if thread in threads:
        models |= set(rerouted)
    return {
        "cli": "codex",
        "session": _name(thread),
        "applied": {"models": sorted(models), "efforts": sorted(efforts)},
    }
