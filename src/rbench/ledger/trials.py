"""Trial states and results (spec run-ledger, Trial states are checkpointed; design D8).

A trial directory holds:

    status.json    {"state": ..., "reason": ...} or, once done,
                   {"state": "done", "findings_sha256": ..., "usage_sha256": ...,
                    "reused_from"?: <source run id>}
    findings.json  the trial's findings:
                   [{"file", "start_line", "end_line", "message", "severity"?}]
    usage.json     its token usage, the engine's and the normalization step's apart:
                   {"engine": {"<model>": {"input", "output", "cache_read", "cache_creation"}},
                    "normalization": {"<model>": {...}}}
    raw/           engine output, transcripts and logs
    audit.json     the contamination audit (`rbench.trial.audit`): {"contaminated": [<reason>],
                   "refused": [{"method", "target", "result"}]}, the refused connection
                   attempts listed in run health

Every file goes through `atomic_write`. `done` is written last and records the SHA-256 of
`findings.json` and `usage.json`: a runner killed while storing results leaves the trial in its
earlier state, and `read_results` returns result files only under a `done` status whose hashes
match them, so neither a half-stored result nor one left by an earlier attempt reads as complete.
A done trial refuses a second store. Findings and usage are validated whole on write and on read.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from rbench.atomic import atomic_write

STATES = ("pending", "running", "done", "failed", "interrupted", "contaminated", "misconfigured")
# States that say what went wrong (spec run-ledger: failed with reason `timeout`, a misconfigured
# configuration's reason, the contamination audit's reason, the limit that interrupted a trial).
NEEDS_REASON = frozenset({"failed", "interrupted", "contaminated", "misconfigured"})

STATUS_FILE = "status.json"
FINDINGS_FILE = "findings.json"
USAGE_FILE = "usage.json"
AUDIT_FILE = "audit.json"
RAW_DIR = "raw"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
FINDING_KEYS = {"file", "start_line", "end_line", "message"}
FINDING_OPTIONAL_KEYS = {"severity"}
USAGE_KEYS = {"input", "output", "cache_read", "cache_creation"}
# Normalization tokens are recorded apart from the engine's (spec engine-configurations, Findings
# normalization); one model can serve both in a trial. A step a trial does not run holds {}.
USAGE_KINDS = ("engine", "normalization")
_HEX64 = re.compile(r"[0-9a-f]{64}")


class LedgerError(ValueError):
    """A run directory or trial file that cannot be used as it is."""


@dataclass(frozen=True)
class TrialStatus:
    state: str
    reason: str | None = None
    findings_sha256: str | None = None
    usage_sha256: str | None = None
    reused_from: str | None = None


def dump_json(data: object) -> bytes:
    """Deterministic JSON for ledger files: sorted keys, fixed indentation, a final newline."""
    return (json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def no_repeated_keys(pairs: list[tuple[str, object]]) -> dict:
    """A `json` object hook that refuses a key given twice instead of keeping the last."""
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError(f"a key is repeated in an object with keys {sorted(data)}")
    return data


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_findings(findings: object, where: str) -> None:
    if not isinstance(findings, list):
        raise LedgerError(f"{where}: findings must be a list")
    for i, finding in enumerate(findings):
        at = f"{where}: finding {i}"
        if not isinstance(finding, dict):
            raise LedgerError(f"{at}: expected an object")
        keys = finding.keys()
        if not FINDING_KEYS <= keys <= FINDING_KEYS | FINDING_OPTIONAL_KEYS:
            raise LedgerError(
                f"{at}: expected the fields {', '.join(sorted(FINDING_KEYS))}"
                f" and optionally {', '.join(sorted(FINDING_OPTIONAL_KEYS))}"
            )
        for key in ("file", "message", *(FINDING_OPTIONAL_KEYS & keys)):
            if not _text(finding[key]):
                raise LedgerError(f"{at}: {key}: expected a non-empty string")
        start, end = finding["start_line"], finding["end_line"]
        if type(start) is not int or start < 1:
            raise LedgerError(f"{at}: start_line: expected a line number from 1")
        if type(end) is not int or end < start:
            raise LedgerError(f"{at}: end_line: expected a line number from start_line")


def _check_usage(usage: object, where: str) -> None:
    if not (isinstance(usage, dict) and usage.keys() == set(USAGE_KINDS)):
        raise LedgerError(f"{where}: usage must be an object of exactly {', '.join(USAGE_KINDS)}")
    for kind in USAGE_KINDS:
        if not isinstance(usage[kind], dict):
            raise LedgerError(f"{where}: usage.{kind} must be an object of models")
        for model, counts in usage[kind].items():
            _check_counts(model, counts, f"{where}: {kind} usage of model {model!r}")


def _check_counts(model: object, counts: object, at: str) -> None:
    if not _text(model):
        raise LedgerError(f"{at}: expected a model id")
    if not (isinstance(counts, dict) and counts.keys() == USAGE_KEYS):
        raise LedgerError(f"{at}: expected exactly {', '.join(sorted(USAGE_KEYS))}")
    for key, count in counts.items():
        if type(count) is not int or count < 0:
            raise LedgerError(f"{at}: {key}: expected a token count from 0")


def set_state(trial_dir: Path, state: str, reason: str | None = None) -> None:
    """Record `state` for the trial in `trial_dir`; `done` goes through `store_results` only."""
    trial_dir = Path(trial_dir)
    if state == "done":
        raise LedgerError(f"{trial_dir}: a trial becomes done only by storing its results")
    if state not in STATES:
        raise LedgerError(f"{trial_dir}: unknown state {state!r}; expected one of {STATES}")
    if state in NEEDS_REASON:
        if not _text(reason):
            raise LedgerError(f"{trial_dir}: state {state!r} needs a reason")
    elif reason is not None:
        raise LedgerError(f"{trial_dir}: state {state!r} takes no reason")
    status = {"state": state} | ({"reason": reason} if reason is not None else {})
    atomic_write(trial_dir / STATUS_FILE, dump_json(status))


def store_results(
    trial_dir: Path, findings: list, usage: dict, *, reused_from: str | None = None
) -> None:
    """Store the trial's findings and usage, then mark it done; done is the last write.

    Refused, before any file changes, for invalid results and for a trial that is already done
    or has no readable status: a done trial's results are a checkpoint that resume keeps.
    """
    trial_dir = Path(trial_dir)
    if reused_from is not None:
        _check_source_id(reused_from, trial_dir)
    _check_findings(findings, str(trial_dir))
    _check_usage(usage, str(trial_dir))
    if read_status(trial_dir).state == "done":
        raise LedgerError(f"{trial_dir}: the trial is already done; its results stay as stored")
    payloads = {FINDINGS_FILE: dump_json(findings), USAGE_FILE: dump_json(usage)}
    for name, payload in payloads.items():
        atomic_write(trial_dir / name, payload)
    status = {
        "state": "done",
        "findings_sha256": hashlib.sha256(payloads[FINDINGS_FILE]).hexdigest(),
        "usage_sha256": hashlib.sha256(payloads[USAGE_FILE]).hexdigest(),
    }
    if reused_from is not None:
        status["reused_from"] = reused_from
    atomic_write(trial_dir / STATUS_FILE, dump_json(status))


def _check_source_id(value: object, where: Path) -> None:
    if not (isinstance(value, str) and RUN_ID.fullmatch(value)):
        raise LedgerError(f"{where}: reused_from: expected a valid source run id")


def read_status(trial_dir: Path) -> TrialStatus:
    """Read and validate the trial's `status.json`."""
    path = Path(trial_dir) / STATUS_FILE
    try:
        data = json.loads(path.read_bytes(), object_pairs_hook=no_repeated_keys)
    except (OSError, ValueError, RecursionError) as e:
        raise LedgerError(f"{path}: cannot read the trial status: {e}") from e
    state = data.get("state") if isinstance(data, dict) else None
    if state not in STATES:
        raise LedgerError(f"{path}: no valid state")
    if state == "done":
        keys = {"state", "findings_sha256", "usage_sha256"}
        if "reused_from" in data:
            keys.add("reused_from")
            _check_source_id(data["reused_from"], path)
    else:
        keys = {"state", "reason"} if state in NEEDS_REASON else {"state"}
    if data.keys() != keys:
        raise LedgerError(f"{path}: state {state!r} needs exactly the fields {sorted(keys)}")
    if state in NEEDS_REASON and not _text(data["reason"]):
        raise LedgerError(f"{path}: reason: expected a non-empty string")
    for key in keys & {"findings_sha256", "usage_sha256"}:
        if not (isinstance(data[key], str) and _HEX64.fullmatch(data[key])):
            raise LedgerError(f"{path}: {key}: expected 64 lowercase hex digits")
    return TrialStatus(**data)


def read_results(trial_dir: Path) -> tuple[list, dict]:
    """The findings and usage of a done trial; raises unless both match its done status and
    are valid findings and usage."""
    trial_dir = Path(trial_dir)
    status = read_status(trial_dir)
    if status.state != "done":
        raise LedgerError(f"{trial_dir}: the trial is {status.state}, not done")
    results = []
    for name, want in ((FINDINGS_FILE, status.findings_sha256), (USAGE_FILE, status.usage_sha256)):
        try:
            payload = (trial_dir / name).read_bytes()
        except OSError as e:
            raise LedgerError(
                f"{trial_dir}: the trial is done, but {name} is unreadable: {e}"
            ) from e
        if hashlib.sha256(payload).hexdigest() != want:
            raise LedgerError(
                f"{trial_dir / name}: does not match the hash its done status records"
            )
        try:
            results.append(json.loads(payload, object_pairs_hook=no_repeated_keys))
        except (ValueError, RecursionError) as e:
            raise LedgerError(f"{trial_dir / name}: not valid JSON: {e}") from e
    findings, usage = results
    _check_findings(findings, str(trial_dir))
    _check_usage(usage, str(trial_dir))
    return findings, usage
