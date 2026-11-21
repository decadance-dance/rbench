"""Reuse earlier checkpoints without running an engine."""

import dataclasses
import json
import stat
from pathlib import Path

import pytest
import test_ledger
from test_ledger import (
    BUDGET,
    CLI,
    DIGEST,
    FINDING,
    IMAGES,
    MATRIX_TOML,
    PLAIN_TWO_MODELS,
    USAGE,
    assert_private_output,
    cli_inputs,
    freeze,
)
from test_manifest import edit, hashes, write_manifest
from test_matrix import engines, matrix

from rbench.cli import ExitCode, main
from rbench.ledger import (
    FINDINGS_FILE,
    LOCK_FILE,
    PLAN_FILE,
    RAW_DIR,
    STATUS_FILE,
    TRIALS_DIR,
    USAGE_FILE,
    Image,
    LedgerError,
    TrialStatus,
    create_run,
    load_plan,
    read_results,
    read_status,
    set_state,
    store_results,
)
from rbench.ledger.resume import recover_trials, run_lock
from rbench.tasks import GROUND_TRUTH_FILE, HIDDEN_DIR_ENV, MANIFEST_FILE

checkout = test_ledger.checkout
full_inputs = test_ledger.full_inputs


def test_reuse_copies_done_results_and_leaves_other_trials_pending(checkout, capsys) -> None:
    argv = cli_inputs(checkout)
    main([*argv, "--run-id", "earlier"])
    capsys.readouterr()
    source = checkout / "runs" / "earlier"
    plan = load_plan(source)
    done, running, failed, interrupted, contaminated, misconfigured = [
        source / TRIALS_DIR / trial.id for trial in plan.trials
    ]
    store_results(done, [FINDING], USAGE)
    (done / RAW_DIR / "engine.log").write_text("raw engine output")
    set_state(running, "running")
    set_state(failed, "failed", "timeout")
    set_state(interrupted, "interrupted", "usage limit")
    set_state(contaminated, "contaminated", "audit failed")
    set_state(misconfigured, "misconfigured", "wrong model")

    with run_lock(source):
        before = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}
        assert before[source / LOCK_FILE]
        assert main([*argv, "--run-id", "later", "--reuse", str(source)]) == ExitCode.ERROR
        assert {p: p.read_bytes() for p in source.rglob("*") if p.is_file()} == before
    out, err = capsys.readouterr()
    assert "runs/later: 5 trials pending" in out
    assert "trial executor is not implemented yet" in err

    target = checkout / "runs" / "later"
    reused = target / TRIALS_DIR / done.name
    assert load_plan(target) == plan
    for name in (FINDINGS_FILE, USAGE_FILE, STATUS_FILE):
        assert stat.S_IMODE((reused / name).stat().st_mode) == 0o600
    assert not list((reused / RAW_DIR).iterdir())
    assert read_status(reused) == dataclasses.replace(read_status(done), reused_from="earlier")
    (done / FINDINGS_FILE).unlink()
    assert read_results(reused) == ([FINDING], USAGE)
    with run_lock(target):
        assert recover_trials(target, plan) == plan.trials[1:]
    for trial in plan.trials[1:]:
        directory = target / TRIALS_DIR / trial.id
        assert read_status(directory) == TrialStatus("pending")
        assert not (directory / USAGE_FILE).exists()
        assert not (directory / FINDINGS_FILE).exists()


def test_reuse_only_unchanged_tasks_across_set_versions(tmp_path: Path) -> None:
    plan = freeze(tmp_path)
    runs = tmp_path / "runs"
    source = create_run(runs, "earlier", plan)
    for trial in plan.trials:
        store_results(source / TRIALS_DIR / trial.id, [FINDING], USAGE)

    task_set = tmp_path / "ours"
    changed_task = task_set / "o-1" / GROUND_TRUTH_FILE
    previous_manifest = json.loads((task_set / MANIFEST_FILE).read_text())
    edit(changed_task, lambda gt: gt["items"][0].update(message="Updated expectation."))
    write_manifest(
        task_set,
        version=2,
        changelog=[
            *previous_manifest["changelog"],
            {"version": 2, "note": "Update one task.", "changed": {"o-1": hashes(task_set)["o-1"]}},
        ],
    )

    new_plan = freeze(tmp_path)
    assert plan.task_sets["ours"] == 1 and new_plan.task_sets["ours"] == 2
    target = create_run(runs, "later", new_plan, reuse=source)
    for trial in new_plan.trials:
        status = read_status(target / TRIALS_DIR / trial.id)
        if trial.task == "o-1":
            assert status == TrialStatus("pending")
        else:
            assert status.state == "done" and status.reused_from == "earlier"


@pytest.mark.parametrize("change", ["engine version", "configuration", "CLI version", "digest"])
def test_reuse_requires_all_frozen_execution_inputs_to_match(tmp_path: Path, change: str) -> None:
    old_plan = freeze(tmp_path)
    runs = tmp_path / "runs"
    source = create_run(runs, "earlier", old_plan)
    for trial in old_plan.trials:
        store_results(source / TRIALS_DIR / trial.id, [FINDING], USAGE)

    engines_ = engines(prompt="b" * 64) if change == "engine version" else engines()
    images = IMAGES
    entry = PLAIN_TWO_MODELS
    if change == "configuration":
        entry = {
            "engine": "plain",
            "roles": {
                "reviewer": {"model": ["claude-opus-5-5", "gpt-6.2-sol"], "effort": "medium"}
            },
        }
    elif change == "CLI version":
        clis = CLI | {"claude": "2.1.317"}
        engines_["plain"] = dataclasses.replace(engines_["plain"], clis=clis)
        images = IMAGES | {"plain": Image(IMAGES["plain"].ref, clis)}
    elif change == "digest":
        images = IMAGES | {"plain": Image(f"rbench/plain@sha256:{'d' * 64}", CLI)}
    new_plan = freeze(
        tmp_path, matrix(entry, budget=BUDGET, engines_=engines_), images=images, engines_=engines_
    )
    target = create_run(runs, "later", new_plan, reuse=source)
    for trial in new_plan.trials:
        assert read_status(target / TRIALS_DIR / trial.id) == TrialStatus("pending")


def test_reuse_compares_image_digest_across_repository_names(tmp_path: Path) -> None:
    old_plan = freeze(tmp_path)
    runs = tmp_path / "runs"
    source = create_run(runs, "earlier", old_plan)
    trial_dir = Path(TRIALS_DIR) / old_plan.trials[0].id
    store_results(source / trial_dir, [FINDING], USAGE)
    images = IMAGES | {"plain": Image(f"mirror/plain@{DIGEST}", CLI)}

    target = create_run(runs, "later", freeze(tmp_path, images=images), reuse=source)

    assert read_status(target / trial_dir).reused_from == "earlier"


def test_reuse_is_refused_for_full_stage(full_inputs, monkeypatch, capsys) -> None:
    checkout, argv, task = full_inputs
    source = checkout / task.name
    monkeypatch.delenv(HIDDEN_DIR_ENV)

    assert main([*argv, "--reuse", str(source)]) == ExitCode.ERROR

    out, err = capsys.readouterr()
    assert "full stage requires fresh trials and cannot use --reuse" in err
    assert_private_output(out + err, task)
    assert str(source) not in err
    assert not (checkout / "runs").exists()


@pytest.mark.parametrize("corrupt", ["plan", "result"])
def test_reuse_refuses_corrupt_source_without_leaking_hidden_details(
    full_inputs, capsys, corrupt: str
) -> None:
    checkout, argv, task = full_inputs
    main(argv)
    capsys.readouterr()
    source = checkout / "runs" / "fixture"
    trial = next(t for t in load_plan(source).trials if t.task == "o-1")
    trial_dir = source / TRIALS_DIR / trial.id
    store_results(trial_dir, [FINDING], USAGE)
    if corrupt == "plan":
        edit(source / PLAN_FILE, lambda p: p.update(format=str(task)))
        message = "cannot read or validate the earlier run's frozen plan.json"
    else:
        (trial_dir / FINDINGS_FILE).write_text(str(task))
        message = "cannot reuse trial 1 in a run with hidden tasks"
    private_parent = source.parent / task.name
    private_parent.mkdir()
    source = source.rename(private_parent / source.name)
    Path(argv[1]).write_text(MATRIX_TOML)

    assert main([*argv, "--run-id", "later", "--reuse", str(source)]) == ExitCode.ERROR

    out, err = capsys.readouterr()
    assert message in err
    assert_private_output(out + err, task)
    assert str(source) not in err
    assert list((checkout / "runs").iterdir()) == [private_parent]


@pytest.mark.parametrize("source_id", ["", "../earlier", "/run/earlier", 1])
def test_reuse_marker_must_be_a_run_id(tmp_path: Path, source_id) -> None:
    set_state(tmp_path, "pending")
    with pytest.raises(LedgerError, match="reused_from"):
        store_results(tmp_path, [FINDING], USAGE, reused_from=source_id)
    assert read_status(tmp_path) == TrialStatus("pending")

    store_results(tmp_path, [FINDING], USAGE)
    edit(tmp_path / STATUS_FILE, lambda status: status.update(reused_from=source_id))
    with pytest.raises(LedgerError, match="reused_from"):
        read_status(tmp_path)
