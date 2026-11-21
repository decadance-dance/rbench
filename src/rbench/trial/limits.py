"""Rate and usage limits a trial's CLIs reported (spec run-ledger, Limit detection and stop;
design D8). Pinned: Claude Code 2.1.316, Codex CLI 0.171.0; `tests/fixtures/limits/` holds the
records.

`detect_limits` reads the error record that ends a turn in the session files `hermetic.collect`
copied out, not the engine's stdout:
- Claude, every root and subagent transcript: an assistant record with `error: "rate_limit"` and
  `apiErrorStatus: 429` is the usage limit when `apiError` is `usage_limit_reached`, a terminal
  rate limit otherwise, even when it says `apiErrorIsTransient`: the record ends the turn, so the
  CLI has given up.
- Codex, every rollout: a `task_complete` whose `error.codex_error_info` is `usage_limit_exceeded`
  is the usage limit; `rate_limit_exceeded`, or `response_too_many_failed_attempts` with
  `http_status_code` 429, is a terminal rate limit.

The typed fields win over the text. The text decides only for a record without them: a Claude API
error message starting with one of the CLI's own usage-limit prefixes, a Codex turn error without
`codex_error_info`, and, when no rollout holds a turn error at all, the `turn.failed` messages on
the `codex exec --json` stdout the runner captured. A line that is not a JSON object is skipped:
the files are engine output.
"""

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from rbench.trial.sessions import CLAUDE_ROOT, CLAUDE_SUBAGENT, CODEX_ROLLOUT

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


def detect_limits(transcripts: Path, codex_stdout: Iterable[Path] = ()) -> list[Limit]:
    """The limits the session files under `transcripts` (container paths below it) report, sorted
    and each once; `codex_stdout` are captured `codex exec --json` stdouts, read only when no
    rollout holds a turn error."""
    found: set[Limit] = set()
    turn_errors = False
    for file in sorted(p for p in transcripts.rglob("*.jsonl") if p.is_file()):
        path = "/" + file.relative_to(transcripts).as_posix()
        if CODEX_ROLLOUT.search(path):
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
        elif CLAUDE_ROOT.search(path) or CLAUDE_SUBAGENT.search(path):
            found |= {Limit("claude", k) for r in _records(file) if (k := _claude_kind(r))}
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
    if (
        "error" in record
        or "apiErrorStatus" in record
        or record.get("isApiErrorMessage") is not True
    ):
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
    if "codex_error_info" not in error:
        return _codex_text(error.get("message"))
    info = error["codex_error_info"]
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
