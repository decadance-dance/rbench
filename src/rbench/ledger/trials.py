"""Trial states and results (spec run-ledger, Trial states are checkpointed; design D8).

A trial directory holds:

    status.json    {"state": ..., "reason": ...} or, once done,
                   {"state": "done", "findings_sha256": ..., "usage_sha256": ...}
    findings.json  the trial's findings, a list
    usage.json     its token usage, an object
    raw/           engine output, transcripts and logs

Every file goes through `atomic_write`. `done` is written last and records the SHA-256 of
`findings.json` and `usage.json`: a runner killed while storing results leaves the trial in its
earlier state, and `read_results` returns result files only under a `done` status whose hashes
match them, so neither a half-stored result nor one left by an earlier attempt reads as complete.
"""

import hashlib
import json
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
RAW_DIR = "raw"


class LedgerError(ValueError):
    """A run directory or trial file that cannot be used as it is."""


@dataclass(frozen=True)
class TrialStatus:
    state: str
    reason: str | None = None
    findings_sha256: str | None = None
    usage_sha256: str | None = None


def dump_json(data: object) -> bytes:
    """Deterministic JSON for ledger files: sorted keys, fixed indentation, a final newline."""
    return (json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def set_state(trial_dir: Path, state: str, reason: str | None = None) -> None:
    """Record `state` for the trial in `trial_dir`; `done` goes through `store_results` only."""
    trial_dir = Path(trial_dir)
    if state == "done":
        raise LedgerError(f"{trial_dir}: a trial becomes done only by storing its results")
    if state not in STATES:
        raise LedgerError(f"{trial_dir}: unknown state {state!r}; expected one of {STATES}")
    if state in NEEDS_REASON:
        if not (isinstance(reason, str) and reason.strip()):
            raise LedgerError(f"{trial_dir}: state {state!r} needs a reason")
    elif reason is not None:
        raise LedgerError(f"{trial_dir}: state {state!r} takes no reason")
    status = {"state": state} | ({"reason": reason} if reason is not None else {})
    atomic_write(trial_dir / STATUS_FILE, dump_json(status))


def store_results(trial_dir: Path, findings: list, usage: dict) -> None:
    """Store the trial's findings and usage, then mark it done; done is the last write."""
    trial_dir = Path(trial_dir)
    if not isinstance(findings, list) or not isinstance(usage, dict):
        raise LedgerError(f"{trial_dir}: findings must be a list and usage an object")
    payloads = {FINDINGS_FILE: dump_json(findings), USAGE_FILE: dump_json(usage)}
    for name, payload in payloads.items():
        atomic_write(trial_dir / name, payload)
    status = {
        "state": "done",
        "findings_sha256": hashlib.sha256(payloads[FINDINGS_FILE]).hexdigest(),
        "usage_sha256": hashlib.sha256(payloads[USAGE_FILE]).hexdigest(),
    }
    atomic_write(trial_dir / STATUS_FILE, dump_json(status))


def read_status(trial_dir: Path) -> TrialStatus:
    """Read and validate the trial's `status.json`."""
    path = Path(trial_dir) / STATUS_FILE
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError) as e:
        raise LedgerError(f"{path}: cannot read the trial status: {e}") from e
    state = data.get("state") if isinstance(data, dict) else None
    if state not in STATES:
        raise LedgerError(f"{path}: no valid state")
    if state == "done":
        keys = {"state", "findings_sha256", "usage_sha256"}
    else:
        keys = {"state", "reason"} if state in NEEDS_REASON else {"state"}
    if data.keys() != keys or not all(isinstance(data[k], str) and data[k] for k in keys):
        raise LedgerError(f"{path}: state {state!r} needs exactly the fields {sorted(keys)}")
    return TrialStatus(**data)


def read_results(trial_dir: Path) -> tuple[list, dict]:
    """The findings and usage of a done trial; raises unless both match its done status."""
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
        results.append(json.loads(payload))
    return results[0], results[1]
