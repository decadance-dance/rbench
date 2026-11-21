"""Resume a run from its frozen plan and trial checkpoints."""

import json
import os
import signal
import socket
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from test_ledger import FINDING, USAGE, cli_inputs, plan_json

import rbench.ledger.plan as plan_module
from rbench.cli import ExitCode, main
from rbench.ledger import (
    FINDINGS_FILE,
    LOCK_FILE,
    PLAN_FILE,
    STATUS_FILE,
    TRIALS_DIR,
    USAGE_FILE,
    TrialStatus,
    load_plan,
    read_status,
    set_state,
    store_results,
)
from rbench.ledger.resume import recover_trials, run_lock


def test_resume_refuses_second_runner_and_names_live_holder(tmp_path: Path) -> None:
    run_dir, _ = plan_json(tmp_path)
    lock = run_dir / LOCK_FILE
    trial_dir = run_dir / TRIALS_DIR / load_plan(run_dir).trials[0].id
    set_state(trial_dir, "running")

    with run_lock(run_dir):
        snapshot = lock.read_bytes()
        holder = json.loads(snapshot)
        assert holder["pid"] == os.getpid()
        assert holder["host"] == socket.gethostname()
        assert datetime.fromisoformat(holder["started_at"]).utcoffset().total_seconds() == 0
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600

        second = subprocess.run(
            [sys.executable, "-m", "rbench", "resume", str(run_dir)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert second.returncode == ExitCode.ERROR
        assert f"{lock}: run is locked by pid {holder['pid']} on {holder['host']}" in second.stderr
        assert holder["started_at"] in second.stderr
        assert read_status(trial_dir) == TrialStatus("running")
        assert lock.read_bytes() == snapshot
    assert lock.read_bytes() == b""


def test_resume_after_crash_recovers_only_unfinished_trials(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(plan_module, "CHECKOUT_ROOT", tmp_path)
    argv = cli_inputs(tmp_path)
    assert main([*argv, "--run-id", "crashed"]) == ExitCode.ERROR
    capsys.readouterr()
    run_dir = tmp_path / "runs" / "crashed"
    plan = load_plan(run_dir)
    trial_dirs = [run_dir / TRIALS_DIR / trial.id for trial in plan.trials]
    running, done, interrupted, failed, pending, contaminated = trial_dirs
    store_results(done, [FINDING], USAGE)
    set_state(interrupted, "interrupted", "usage limit")
    set_state(failed, "failed", "timeout")
    set_state(contaminated, "contaminated", "audit failed")
    crash = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import signal
import sys
from pathlib import Path

from rbench.ledger import set_state
from rbench.ledger.resume import run_lock

with run_lock(Path(sys.argv[1])):
    set_state(Path(sys.argv[2]), "running")
    os.kill(os.getpid(), signal.SIGKILL)
""",
            str(run_dir),
            str(running),
        ],
        check=False,
        timeout=10,
    )
    assert crash.returncode == -signal.SIGKILL
    holder = json.loads((run_dir / LOCK_FILE).read_bytes())
    assert holder["host"] == socket.gethostname()
    assert read_status(running).state == "running"

    (tmp_path / "matrices" / "m.toml").write_text("unusable as a matrix")
    checkpoints = [done / FINDINGS_FILE, done / USAGE_FILE]
    checkpoints.extend(d / STATUS_FILE for d in trial_dirs[1:])
    before = {p: p.read_bytes() for p in checkpoints}
    assert main(["resume", str(run_dir)]) == ExitCode.OK
    out, err = capsys.readouterr()
    assert f"recovered stale run lock from pid {holder['pid']} on {holder['host']}" in err
    assert "continues with its frozen plan" in err
    assert "3 trials ready to rerun" in out
    assert load_plan(run_dir) == plan
    assert read_status(running).state == "interrupted"
    assert stat.S_IMODE((running / STATUS_FILE).stat().st_mode) == 0o600
    assert {p: p.read_bytes() for p in before} == before
    assert (run_dir / LOCK_FILE).read_bytes() == b""

    with run_lock(run_dir) as previous:
        assert previous is None
        assert [trial.id for trial in recover_trials(run_dir, plan)] == [
            running.name,
            interrupted.name,
            pending.name,
        ]


def test_resume_refuses_unreadable_trial_status_before_changing_trials(
    tmp_path: Path, capsys
) -> None:
    run_dir, _ = plan_json(tmp_path)
    running, broken = (run_dir / TRIALS_DIR / trial.id for trial in load_plan(run_dir).trials[:2])
    set_state(running, "running")
    (broken / STATUS_FILE).unlink()

    assert main(["resume", str(run_dir)]) == ExitCode.ERROR
    assert str(broken / STATUS_FILE) in capsys.readouterr().err
    assert read_status(running) == TrialStatus("running")
    assert (run_dir / LOCK_FILE).read_bytes() == b""


@pytest.mark.parametrize("format_version", [1, 0])
def test_resume_checks_previous_ledger_format(tmp_path: Path, capsys, format_version: int) -> None:
    run_dir, plan = plan_json(tmp_path)
    assert plan["format"] == 1  # The format shipped by the previous ledger writer.
    (run_dir / PLAN_FILE).write_text(json.dumps(plan | {"format": format_version}))
    trial_dir = run_dir / TRIALS_DIR / plan["trials"][0]["id"]
    set_state(trial_dir, "running")
    assert (run_dir / LOCK_FILE).read_bytes() == b""

    exit_code = main(["resume", str(run_dir)])
    _, err = capsys.readouterr()
    if format_version == 1:
        assert exit_code == ExitCode.OK
        assert read_status(trial_dir).state == "interrupted"
    else:
        assert exit_code == ExitCode.ERROR
        assert (
            f"{run_dir / PLAN_FILE}: format: plan format 0, but this rbench reads format 1" in err
        )
        assert "continue the run with the rbench version that started it" in err
        assert read_status(trial_dir) == TrialStatus("running")
    assert (run_dir / LOCK_FILE).read_bytes() == b""
