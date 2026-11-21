"""Findings normalization (spec engine-configurations, Findings normalization; design D9).

An engine whose declaration says `output = "markdown"` reports in prose. A pinned model with a
versioned prompt copies that report's findings into the ledger's finding format
(`rbench.ledger.trials`: file, start_line, end_line, message and, when the engine gives one,
severity). It copies and never judges. Structured output is converted by `parse_findings` alone,
with no model step.

The model runs hermetic, like the judge (spec review-scoring, Judge isolation and caching): `claude
-p` through the shim in a fresh compose project of the agent image, prepared with the trial homes
and the shim settings of design D5, with every tool off (`--tools ""`) and no repository copied in.
The report goes by stdin, between marker lines, as data. The caller owns the project: it creates
and starts it, collects its shim log and transcripts for the audit, and removes it, as for a trial.

The pin (model, effort, prompt version and the hash of the prompt with its output schema) joins
the settings of every Markdown configuration, so a change to any of them gives new configuration
ids (`rbench.matrix.expansion.Configuration.settings`).
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rbench.atomic import atomic_write

if TYPE_CHECKING:
    from rbench.trial.compose import TrialProject

# Design D9: copying must be complete, so medium rather than low effort; the tokens are recorded
# apart from the engine's (spec review-scoring, Per-trial metrics).
MODEL = "claude-sonnet-5-5"
EFFORT = "medium"
PROMPT_VERSION = "1"
RECORD_FILE = "normalization.json"
PROMPT_FILE = Path(__file__).with_name(f"normalization-prompt-v{PROMPT_VERSION}.md")
_LOCATED = {
    "type": "object",
    "additionalProperties": False,
    "required": ["file", "start_line", "end_line", "message", "severity"],
    "properties": {
        "file": {"type": "string", "minLength": 1},
        "start_line": {"type": "integer", "minimum": 1},
        "end_line": {"type": "integer", "minimum": 1},
        "message": {"type": "string", "minLength": 1},
        "severity": {"type": ["string", "null"]},
    },
}
_UNLOCATED = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {"text": {"type": "string", "minLength": 1}},
}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings", "unlocated"],
    "properties": {
        "findings": {"type": "array", "items": _LOCATED},
        "unlocated": {"type": "array", "items": _UNLOCATED},
    },
}
_SCHEMA_TEXT = json.dumps(SCHEMA, sort_keys=True, separators=(",", ":"))
PROMPT = PROMPT_FILE.read_text(encoding="utf-8")
# The settings every Markdown configuration's id hashes.
SETTINGS = {
    "model": MODEL,
    "effort": EFFORT,
    "prompt_version": PROMPT_VERSION,
    "prompt_sha256": hashlib.sha256(f"{PROMPT}\0{_SCHEMA_TEXT}".encode()).hexdigest(),
}
# Claude Code 2.1.316's `modelUsage` fields -> the ledger's usage keys.
_USAGE = {
    "inputTokens": "input",
    "outputTokens": "output",
    "cacheReadInputTokens": "cache_read",
    "cacheCreationInputTokens": "cache_creation",
}


class NormalizationError(ValueError):
    """A normalization result that cannot be used; the trial's findings are not stored."""


@dataclass(frozen=True)
class Normalization:
    """Findings in the ledger's format, the items left out for lack of a file or a line, and the
    step's token usage per model (the ledger's `normalization` usage kind)."""

    findings: list[dict]
    unlocated: list[str]
    usage: dict[str, dict[str, int]]

    def record(self) -> dict:
        """What the trial keeps under raw/: the pin, and the items that did not become findings,
        so that the loss is visible (design D9)."""
        return {"normalizer": SETTINGS, "findings": len(self.findings), "unlocated": self.unlocated}

    def write_record(self, raw_dir: Path) -> None:
        """Write `record()` to `raw_dir/normalization.json`, atomically."""
        from rbench.ledger.trials import dump_json  # see parse_result

        atomic_write(Path(raw_dir) / RECORD_FILE, dump_json(self.record()))


def _text(value: object) -> bool:
    """A non-blank string the ledger can write: JSON may carry lone surrogates, UTF-8 cannot."""
    if not (isinstance(value, str) and value.strip()):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _line(value: object) -> bool:
    return type(value) is int and value >= 1


def parse_findings(data: object, where: str, *, severity_required: bool = False) -> list[dict]:
    """Validate findings given as the ledger stores them, or as the normalizer answers
    (`severity_required`: every item has `severity`, null when the engine gives none); return
    them in the ledger's format.

    Engine and model output is untrusted: one bad item rejects the whole list, naming `where`
    (the trial) and the item.
    """
    if not isinstance(data, list):
        raise NormalizationError(f"{where}: findings: expected a list")
    findings = []
    for i, item in enumerate(data):
        at = f"{where}: finding {i}"
        required = {"file", "start_line", "end_line", "message"}
        if not (isinstance(item, dict) and required <= item.keys() <= required | {"severity"}):
            raise NormalizationError(
                f"{at}: expected an object of file, start_line, end_line, message and severity"
            )
        if severity_required and "severity" not in item:
            raise NormalizationError(f"{at}: severity: missing; null when the engine gives none")
        for key in ("file", "message"):
            if not _text(item[key]):
                raise NormalizationError(f"{at}: {key}: expected a non-empty UTF-8 string")
        if not _line(item["start_line"]):
            raise NormalizationError(f"{at}: start_line: expected a line number from 1")
        if not (_line(item["end_line"]) and item["end_line"] >= item["start_line"]):
            raise NormalizationError(f"{at}: end_line: expected a line number from start_line")
        finding = {k: item[k] for k in sorted(required)}
        severity = item.get("severity")
        if severity is not None:
            if not _text(severity):
                raise NormalizationError(
                    f"{at}: severity: expected a non-empty UTF-8 string or null"
                )
            finding["severity"] = severity
        findings.append(finding)
    return findings


def _usage(model_usage: object, where: str) -> dict[str, dict[str, int]]:
    if not (isinstance(model_usage, dict) and MODEL in model_usage):
        # The pinned model must be the one that ran; a fallback is not the normalizer.
        raise NormalizationError(f"{where}: modelUsage does not show the pinned model {MODEL}")
    usage = {}
    for model, counts in sorted(model_usage.items()):
        if not _text(model):
            raise NormalizationError(f"{where}: modelUsage key {model!r}: expected a model id")
        if not (isinstance(counts, dict) and _USAGE.keys() <= counts.keys()):
            raise NormalizationError(f"{where}: modelUsage of {model!r}: missing token counts")
        for key in _USAGE:
            if type(counts[key]) is not int or counts[key] < 0:
                raise NormalizationError(f"{where}: modelUsage of {model!r}: bad {key}")
        usage[model] = {ours: counts[theirs] for theirs, ours in _USAGE.items()}
    return usage


def parse_result(stdout: str, where: str) -> Normalization:
    """Validate the `claude -p --output-format json` result of the normalizer, whole."""
    # Imported here: rbench.ledger imports the matrix, which imports this module for SETTINGS.
    from rbench.ledger.trials import no_repeated_keys

    try:
        result = json.loads(stdout, object_pairs_hook=no_repeated_keys)
    except (ValueError, RecursionError) as e:
        raise NormalizationError(f"{where}: the result is not one JSON object: {e}") from None
    if not isinstance(result, dict) or result.get("type") != "result":
        raise NormalizationError(f"{where}: expected a result object")
    if result.get("is_error") is not False:
        raise NormalizationError(
            f"{where}: the session ended in an error ({result.get('subtype')})"
        )
    answer = result.get("structured_output")
    if not (isinstance(answer, dict) and answer.keys() == {"findings", "unlocated"}):
        raise NormalizationError(f"{where}: structured_output: expected findings and unlocated")
    unlocated = answer["unlocated"]
    if not isinstance(unlocated, list):
        raise NormalizationError(f"{where}: unlocated: expected a list")
    texts = []
    for i, item in enumerate(unlocated):
        if not (isinstance(item, dict) and item.keys() == {"text"} and _text(item["text"])):
            raise NormalizationError(f"{where}: unlocated item {i}: expected {{text}}")
        texts.append(item["text"])
    findings = parse_findings(answer["findings"], where, severity_required=True)
    return Normalization(findings, texts, _usage(result.get("modelUsage"), where))


def prompt(report: str) -> str:
    """The normalizer's stdin: the versioned prompt, then the report between marker lines that
    name its hash, so the report cannot close its own block."""
    tag = hashlib.sha256(report.encode()).hexdigest()[:16]
    return f"{PROMPT}\n----- BEGIN REPORT {tag} -----\n{report}\n----- END REPORT {tag} -----\n"


def argv() -> list[str]:
    """The normalizer's launch; the shim adds the hermetic policy flags (design D5)."""
    return [
        "claude",
        "-p",
        "--model",
        MODEL,
        "--effort",
        EFFORT,
        "--tools",
        "",
        "--output-format",
        "json",
        "--json-schema",
        _SCHEMA_TEXT,
    ]


def normalize(project: "TrialProject", report: str, trial: str) -> Normalization:
    """Copy the findings of `report`, the Markdown output of trial `trial`, in `project`: a fresh
    compose project of the agent image that the caller has started (see the module docstring).

    A blank report has no findings and runs no model. Errors name the trial.
    """
    from rbench.trial.hermetic import prepare, shim_settings  # see parse_result

    where = f"trial {trial}: normalization"
    if not report.strip():
        return Normalization([], [], {})
    prepare(project, shim_settings({}))
    done = project.exec_agent(argv(), stdin=prompt(report))
    if done.returncode != 0:
        detail = (done.stderr.strip().splitlines() or ["no output"])[-1]
        raise NormalizationError(f"{where}: claude exited {done.returncode}: {detail}")
    return parse_result(done.stdout, where)
