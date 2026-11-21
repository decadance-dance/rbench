"""Exclusive access and recovery of a run (spec run-ledger, Resume)."""

import fcntl
import json
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from rbench.atomic import atomic_write
from rbench.ledger.plan import LOCK_FILE, PLAN_FILE, TRIALS_DIR, Plan, PlannedTrial
from rbench.ledger.trials import LedgerError, dump_json, no_repeated_keys, read_status, set_state


def _read_holder(path: Path) -> str | None:
    raw = path.read_bytes()
    if not raw:
        return None
    try:
        holder = json.loads(raw, object_pairs_hook=no_repeated_keys)
    except (ValueError, RecursionError) as e:
        raise LedgerError(f"{path}: cannot read the lock holder: {e}") from e
    if not (
        isinstance(holder, dict)
        and holder.keys() == {"pid", "host", "started_at"}
        and type(holder["pid"]) is int
        and holder["pid"] > 0
        and all(isinstance(holder[key], str) and holder[key] for key in ("host", "started_at"))
    ):
        raise LedgerError(f"{path}: expected a lock holder with pid, host and start time")
    return f"pid {holder['pid']} on {holder['host']} (started {holder['started_at']})"


@contextmanager
def run_lock(run_dir: Path) -> Iterator[str | None]:
    """Hold exclusive access to the run and yield any stale holder left by a crash."""
    lock_path = run_dir / LOCK_FILE
    gate = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with (run_dir / PLAN_FILE).open("rb") as plan_file:
            # Serialize holder publication and release. The frozen plan's inode stays fixed
            # while ledger.lock is atomically replaced, and its flock dies with the process.
            fcntl.flock(gate, fcntl.LOCK_EX)
            previous = _read_holder(lock_path)
            try:
                fcntl.flock(plan_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = previous or "a runner whose holder details are unavailable"
                raise LedgerError(f"{lock_path}: run is locked by {holder}") from None
            atomic_write(
                lock_path,
                dump_json(
                    {
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "started_at": datetime.now(UTC).isoformat(),
                    }
                ),
            )
            fcntl.flock(gate, fcntl.LOCK_UN)
            try:
                yield previous
            finally:
                fcntl.flock(gate, fcntl.LOCK_EX)
                atomic_write(lock_path, b"")
    finally:
        os.close(gate)


def recover_trials(run_dir: Path, plan: Plan) -> tuple[PlannedTrial, ...]:
    """Under the run lock, recover interrupted work and return trials in frozen plan order."""
    queue = []
    left_running = []
    for trial in plan.trials:
        trial_dir = run_dir / TRIALS_DIR / trial.id
        state = read_status(trial_dir).state
        if state == "running":
            left_running.append(trial_dir)
        if state in ("pending", "interrupted", "running"):
            queue.append(trial)
    for trial_dir in left_running:
        set_state(trial_dir, "interrupted", "previous runner stopped while the trial was running")
    return tuple(queue)
