"""Rate and usage limits a trial's CLIs reported (spec run-ledger, Limit detection and stop;
design D8). Pinned: Claude Code 2.1.316, Codex CLI 0.171.0; `tests/fixtures/limits/` holds the
records.

`detect_limits` reads the error record that ends a turn in the session files `hermetic.collect`
copied out, not the engine's stdout, and only in sessions the shim log binds to a shim launch: a
file that only looks like a transcript, such as one in the reviewed repository, decides nothing.
- Claude: the transcript at the `transcript_path` reported by the SessionStart hook of a session
  that ran under a `claude` process a shim started, read for that session's records, and the
  subagent transcripts beside it (`<transcript without .jsonl>/subagents/`). In them, an assistant record with `error: "rate_limit"` and
  `apiErrorStatus: 429` is the usage limit when `apiError` is `usage_limit_reached`, a terminal
  rate limit otherwise, even when it says `apiErrorIsTransient`: the record ends the turn, so the
  CLI has given up.
- Codex: a rollout a shim launch's CLI held open (a `binding` record). In it, a `task_complete` whose `error.codex_error_info` is `usage_limit_exceeded`
  is the usage limit; `rate_limit_exceeded`, or `response_too_many_failed_attempts` with
  `http_status_code` 429, is a terminal rate limit.

The typed fields win over the text. The text decides only for a record without them: a Claude API
error message starting with one of the CLI's own usage-limit prefixes, a Codex turn error whose
`codex_error_info` is absent or null, and, when no rollout holds a turn error at all, the `turn.failed` messages on
the `codex exec --json` stdout the runner captured. A line that is not a JSON object is skipped:
the files are engine output.
"""

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from rbench.trial.sessions import CLAUDE_SUBAGENT, _children

USAGE = "usage limit"
RATE = "rate limit"
# Claude Code 2.1.316: a 429 whose text starts with one of these gets `usage_limit_reached`.
CLAUDE_USAGE_PREFIXES = (
    "You've hit your",
    "You've reached your",
    "You're out of usage credits",
    "Your org is out of usage · add funds to continue",
    "Your org is out of usage · contact your admin",
    "Your seat type doesn't include usage credits",
    "Your seat type doesn't include usage",
    "Your usage allocation has been disabled by your admin",
    "Your group's usage limit is set to $0",
    "Fable 5 requires usage credits",
    "You're out of extra usage",
    "Your seat type doesn't include extra usage",
)
CODEX_USAGE_TEXT = "You've hit your usage limit"
CODEX_RETRY_TEXT = "exceeded retry limit, last status: 429"


@dataclass(frozen=True, order=True)
class Limit:
    """A limit one CLI reported: `cli` is `claude` or `codex`, `kind` is `USAGE` or `RATE`."""

    cli: str
    kind: str

    def __str__(self) -> str:
        return f"{self.cli.capitalize()} reported its {self.kind}"


def detect_limits(
    records: list[dict], transcripts: Path, codex_stdout: Iterable[Path] = ()
) -> list[Limit]:
    """The limits the sessions the shim log `records` binds report in `transcripts` (container
    paths below it), sorted and each once; `codex_stdout` are captured `codex exec --json` stdouts,
    read only when no bound rollout holds a turn error."""
    children = _children(records)
    roots = {  # the transcript path a shim-started Claude session's hook reported -> its id
        r["transcript_path"]: r["session_id"]
        for r in records
        if r["kind"] == "hook"
        and isinstance(r.get("transcript_path"), str)
        and (r.get("claude_pid"), r.get("claude_start")) in children
    }
    rollouts = {r["path"] for r in records if r["kind"] == "binding"}
    files = sorted(p for p in transcripts.rglob("*.jsonl") if p.is_file() and not p.is_symlink())
    paths = {"/" + f.relative_to(transcripts).as_posix(): f for f in files}
    found: set[Limit] = set()
    for path, file in paths.items():
        if path in roots:
            records_of = (r for r in _records(file) if r.get("sessionId") == roots[path])
        elif (m := CLAUDE_SUBAGENT.search(path)) and roots.get(
            path.rpartition("/subagents/")[0] + ".jsonl"
        ) == m.group(1):
            records_of = _records(file)
        else:
            continue
        found |= {Limit("claude", k) for r in records_of if (k := _claude_kind(r))}
    turn_errors = False
    for path, file in paths.items():
        if path in rollouts:
            for record in _records(file):
                payload = record.get("payload")
                if record.get("type") != "event_msg" or not isinstance(payload, dict):
                    continue
                error = payload.get("error")
                if payload.get("type") != "task_complete" or not isinstance(error, dict):
                    continue
                turn_errors = True
                if kind := _codex_kind(error):
                    found.add(Limit("codex", kind))
    if not turn_errors:
        for file in codex_stdout:
            for event in _records(file):
                error = event.get("error")
                if event.get("type") != "turn.failed" or not isinstance(error, dict):
                    continue
                if kind := _codex_text(error.get("message")):
                    found.add(Limit("codex", kind))
    return sorted(found)


def _records(file: Path) -> Iterator[dict]:
    try:
        with file.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _claude_kind(record: dict) -> str | None:
    if record.get("type") != "assistant":
        return None
    if record.get("error") == "rate_limit" and _is(record.get("apiErrorStatus"), 429):
        return USAGE if record.get("apiError") == "usage_limit_reached" else RATE
    typed = record.get("error") is not None or record.get("apiErrorStatus") is not None
    if typed or record.get("isApiErrorMessage") is not True:
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    texts = (
        [b.get("text") for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    )
    if any(isinstance(t, str) and t.startswith(CLAUDE_USAGE_PREFIXES) for t in texts):
        return USAGE
    return None


def _codex_kind(error: dict) -> str | None:
    # Codex 0.171.0 writes an unclassified error's `codex_error_info` as null.
    if (info := error.get("codex_error_info")) is None:
        return _codex_text(error.get("message"))
    if info == "usage_limit_exceeded":
        return USAGE
    if info == "rate_limit_exceeded":
        return RATE
    if isinstance(info, dict) and isinstance(
        failed := info.get("response_too_many_failed_attempts"), dict
    ):
        return RATE if _is(failed.get("http_status_code"), 429) else None
    return None


def _codex_text(message: object) -> str | None:
    if not isinstance(message, str):
        return None
    message = message.replace("’", "'")
    if message.startswith(CODEX_USAGE_TEXT):
        return USAGE
    if message.startswith(CODEX_RETRY_TEXT):
        return RATE
    return None


def _is(value: object, number: int) -> bool:
    return type(value) is int and value == number
