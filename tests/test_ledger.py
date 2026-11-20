"""The run ledger (spec run-ledger: Frozen plan, Trial states are checkpointed; design D8).

Engines, tasks, images and matrices are synthetic and built in tmp_path.
"""

import hashlib
import json
import stat
import subprocess
from pathlib import Path

import pytest
from test_manifest import write_manifest
from test_matrix import engines, matrix
from test_tasks import item, write_task

import rbench.ledger.plan as plan_module
import rbench.ledger.trials as trials_module
from rbench.cli import ExitCode, main
from rbench.engines.declaration import parse_engine
from rbench.engines.pins import FloatingRefError
from rbench.ledger import (
    FINDINGS_FILE,
    LOCK_FILE,
    PLAN_FILE,
    RAW_DIR,
    STATES,
    STATUS_FILE,
    TRIALS_DIR,
    USAGE_FILE,
    Image,
    LedgerError,
    TrialStatus,
    create_run,
    freeze_plan,
    load_images,
    load_plan,
    matrix_warning,
    read_results,
    read_status,
    set_state,
    store_results,
)
from rbench.matrix.expansion import MatrixError
from rbench.tasks import CORE_LIST_FILE, load_core_list, load_task_set

ROOT = Path(__file__).resolve().parent.parent
CLI = {"claude": "2.1.316", "codex": "0.167.0"}
DIGEST = "sha256:" + "e" * 64
IMAGES = {name: Image(f"rbench/{name}@{DIGEST}", dict(CLI)) for name in ("plain", "panel")}
PLAIN_TWO_MODELS = {
    "engine": "plain",
    "roles": {"reviewer": {"model": ["claude-opus-5-5", "gpt-6.2-sol"]}},
}
BUDGET = {"claude": 1000, "codex": 2000}


def task_sets(root: Path) -> list:
    """Two public sets with two tasks each, their manifests and a core list of three tasks."""
    for task_id in ("rb-1", "rb-2"):
        write_task(root / "reviewbench", task_id, task_set="reviewbench", items=[item(source=None)])
    for task_id in ("o-1", "o-2"):
        write_task(root / "ours", task_id)
    write_manifest(root / "reviewbench", "reviewbench")
    write_manifest(root / "ours", "ours")
    (root / CORE_LIST_FILE).write_text(json.dumps({"version": 1, "tasks": ["rb-2", "o-1", "o-2"]}))
    return [load_task_set(root / s, s) for s in ("reviewbench", "ours")]


def freeze(root: Path, m=None, round_name="r1", images=IMAGES, engines_=None) -> dict:
    if not (root / CORE_LIST_FILE).exists():
        task_sets(root)
    return freeze_plan(
        m or matrix(PLAIN_TWO_MODELS, budget=BUDGET),
        round_name,
        engines_ or engines(),
        [load_task_set(root / s, s) for s in ("reviewbench", "ours")],
        load_core_list(root / CORE_LIST_FILE),
        images,
        "m.toml",
        "f" * 64,
    )


# Frozen plan


def test_plan_holds_what_the_spec_freezes(tmp_path: Path) -> None:
    plan = freeze(tmp_path)

    assert plan["round"] == {"name": "r1", "budget": BUDGET}
    assert [c["settings"]["roles"]["reviewer"]["models"] for c in plan["configurations"]] == [
        ["claude-opus-5-5"],
        ["gpt-6.2-sol"],
    ]
    assert plan["task_sets"] == {"ours": 1, "reviewbench": 1}
    manifest = json.loads((tmp_path / "ours" / "manifest.json").read_text())
    assert [t["id"] for t in plan["tasks"]] == ["o-1", "o-2", "rb-2"]
    assert plan["tasks"][0] == {
        "id": "o-1",
        "set": "ours",
        "sha256": manifest["tasks"]["o-1"]["sha256"],
    }
    assert plan["engines"] == {
        "plain": {
            "pin": {"kind": "sha256", "ref": "ab" * 32},
            "version": "ab" * 32,
            "clis": CLI,
            "image": f"rbench/plain@{DIGEST}",
        }
    }
    assert plan["matrix"] == {"path": "m.toml", "sha256": "f" * 64}


def test_trials_are_queued_by_attempt_then_task_then_configuration(tmp_path: Path) -> None:
    m = matrix(PLAIN_TWO_MODELS, budget=BUDGET, stage="full", tasks="all", attempts=3)
    plan = freeze(tmp_path, m)

    configs = [c["id"] for c in plan["configurations"]]
    tasks = ["o-1", "o-2", "rb-1", "rb-2"]
    expected = [(a, t, c) for a in (1, 2, 3) for t in tasks for c in configs]
    assert [(t["attempt"], t["task"], t["configuration"]) for t in plan["trials"]] == expected
    assert len({t["id"] for t in plan["trials"]}) == 3 * 4 * 2


def test_same_inputs_give_the_same_plan_bytes(tmp_path: Path) -> None:
    first = create_run(tmp_path / "runs", "a", freeze(tmp_path))
    second = create_run(tmp_path / "runs", "b", freeze(tmp_path))

    assert (first / PLAN_FILE).read_bytes() == (second / PLAN_FILE).read_bytes()
    assert sorted(p.name for p in (first / TRIALS_DIR).iterdir()) == sorted(
        p.name for p in (second / TRIALS_DIR).iterdir()
    )


def test_run_directory_layout(tmp_path: Path) -> None:
    plan = freeze(tmp_path)
    run_dir = create_run(tmp_path / "runs", "run-1", plan)

    assert load_plan(run_dir) == plan
    assert (run_dir / LOCK_FILE).read_bytes() == b""
    trial_dirs = sorted((run_dir / TRIALS_DIR).iterdir())
    assert [d.name for d in trial_dirs] == sorted(t["id"] for t in plan["trials"])
    for d in trial_dirs:
        assert read_status(d) == TrialStatus("pending")
        assert (d / RAW_DIR).is_dir()
    for path in (run_dir / PLAN_FILE, run_dir / LOCK_FILE, trial_dirs[0] / STATUS_FILE):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for path in (run_dir, trial_dirs[0], trial_dirs[0] / RAW_DIR):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_existing_run_is_never_overwritten(tmp_path: Path) -> None:
    run_dir = create_run(tmp_path / "runs", "run-1", freeze(tmp_path))
    before = (run_dir / PLAN_FILE).read_bytes()
    m = matrix(PLAIN_TWO_MODELS, budget=BUDGET, stage="full", tasks="all", attempts=3)

    with pytest.raises(LedgerError, match=r"already exists; resume it"):
        create_run(tmp_path / "runs", "run-1", freeze(tmp_path, m))
    assert (run_dir / PLAN_FILE).read_bytes() == before


def test_edited_matrix_warns_and_the_plan_stays(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    Path("m.toml").write_text("stage = 'screening'\n")
    plan = freeze(tmp_path)
    plan["matrix"]["sha256"] = hashlib.sha256(b"stage = 'screening'\n").hexdigest()
    run_dir = create_run(Path("runs"), "run-1", plan)
    assert matrix_warning(load_plan(run_dir)) is None

    Path("m.toml").write_text("stage = 'full'\n")

    assert load_plan(run_dir) == plan
    assert "m.toml changed or is gone" in matrix_warning(load_plan(run_dir))
    assert "continues with its frozen plan" in matrix_warning(load_plan(run_dir))


def test_plan_of_another_format_is_refused(tmp_path: Path) -> None:
    run_dir = create_run(tmp_path / "runs", "run-1", freeze(tmp_path))
    plan = json.loads((run_dir / PLAN_FILE).read_text())
    (run_dir / PLAN_FILE).write_text(json.dumps(plan | {"format": 0}))

    with pytest.raises(LedgerError, match=r"plan format 0, but this rbench reads format 1"):
        load_plan(run_dir)


def test_crash_while_creating_leaves_no_run(tmp_path: Path, monkeypatch) -> None:
    plan = freeze(tmp_path)
    calls = []

    def crash(trial_dir, state, reason=None):
        calls.append(trial_dir)
        if len(calls) == 2:
            raise KeyboardInterrupt
        trials_module.set_state(trial_dir, state, reason)

    monkeypatch.setattr(plan_module, "set_state", crash)
    with pytest.raises(KeyboardInterrupt):
        create_run(tmp_path / "runs", "run-1", plan)
    assert list((tmp_path / "runs").iterdir()) == []


@pytest.mark.parametrize("run_id", ["", "../x", "a/b", ".hidden"])
def test_bad_run_id_is_refused(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(LedgerError, match=r"bad run id"):
        create_run(tmp_path / "runs", run_id, {"trials": []})


@pytest.mark.parametrize(
    ("images", "error", "message"),
    [
        ({}, LedgerError, r"engine 'plain': no image given"),
        (
            {"plain": Image("rbench/plain:latest", dict(CLI))},
            FloatingRefError,
            r"image 'rbench/plain:latest' is a floating reference",
        ),
        (
            {"plain": Image(f"rbench/plain@{DIGEST}", CLI | {"claude": "2.1.317"})},
            LedgerError,
            r"has CLIs .*2\.1\.317.*, but the declaration pins",
        ),
    ],
)
def test_images_are_checked(tmp_path: Path, images, error, message) -> None:
    with pytest.raises(error, match=message):
        freeze(tmp_path, images=images)


def test_unresolved_tag_pin_is_refused(tmp_path: Path) -> None:
    tagged = {
        "name": "plain",
        "pin": {"kind": "git", "ref": "refs/tags/v1.0.0"},
        "cli": CLI,
        "roles": {
            "reviewer": {
                "delivery": "flag",
                "default": {"model": "claude-opus-5-5", "effort": "high"},
            }
        },
    }
    engines_ = {"plain": parse_engine(tagged, "plain.toml")}
    m = matrix({"engine": "plain", "default": True}, budget=BUDGET, engines_=engines_)

    with pytest.raises(LedgerError, match=r"resolve tag refs/tags/v1\.0\.0 to its commit"):
        freeze(tmp_path, m, engines_=engines_)


def test_undecided_budget_and_unknown_round_are_refused(tmp_path: Path) -> None:
    undecided = matrix(PLAIN_TWO_MODELS)  # codex budget "undecided"
    with pytest.raises(MatrixError, match=r"the codex budget is still 'undecided'"):
        freeze(tmp_path, undecided)
    with pytest.raises(LedgerError, match=r"no round 'r9'; rounds: r1"):
        freeze(tmp_path, round_name="r9")


def test_load_images_validates_its_file(tmp_path: Path) -> None:
    path = tmp_path / "images.json"
    path.write_text(json.dumps({"plain": {"image": f"x@{DIGEST}", "clis": CLI}}))
    assert load_images(path) == {"plain": Image(f"x@{DIGEST}", CLI)}

    path.write_text(json.dumps({"plain": {"image": f"x@{DIGEST}"}}))
    with pytest.raises(LedgerError, match=r"engine 'plain': expected exactly the fields"):
        load_images(path)


# Trial states


@pytest.mark.parametrize("state", [s for s in STATES if s != "done"])
def test_every_state_but_done_is_set_atomically(tmp_path: Path, state: str) -> None:
    reason = "timeout" if state in trials_module.NEEDS_REASON else None
    set_state(tmp_path, state, reason)

    assert read_status(tmp_path) == TrialStatus(state, reason)
    assert [p.name for p in tmp_path.iterdir()] == [STATUS_FILE]  # no temporary file left


def test_states_are_exactly_the_spec_list() -> None:
    assert STATES == (
        "pending",
        "running",
        "done",
        "failed",
        "interrupted",
        "contaminated",
        "misconfigured",
    )


@pytest.mark.parametrize(
    ("state", "reason", "message"),
    [
        ("done", None, r"done only by storing its results"),
        ("finished", None, r"unknown state 'finished'"),
        ("failed", None, r"'failed' needs a reason"),
        ("failed", " ", r"'failed' needs a reason"),
        ("running", "why", r"'running' takes no reason"),
    ],
)
def test_bad_state_changes_are_refused(tmp_path: Path, state, reason, message) -> None:
    with pytest.raises(LedgerError, match=message):
        set_state(tmp_path, state, reason)
    assert not (tmp_path / STATUS_FILE).exists()


def test_done_holds_findings_and_usage(tmp_path: Path) -> None:
    set_state(tmp_path, "running")
    store_results(tmp_path, [{"file": "a.py"}], {"claude-opus-5-5": {"input": 3}})

    assert read_status(tmp_path).state == "done"
    assert read_results(tmp_path) == ([{"file": "a.py"}], {"claude-opus-5-5": {"input": 3}})


@pytest.mark.parametrize("killed_at", [FINDINGS_FILE, USAGE_FILE, STATUS_FILE])
def test_kill_while_storing_leaves_the_trial_not_done(tmp_path: Path, monkeypatch, killed_at):
    set_state(tmp_path, "running")
    real = trials_module.atomic_write

    def killed(path, data):
        if Path(path).name == killed_at:
            raise KeyboardInterrupt  # the process dies before this write lands
        real(path, data)

    monkeypatch.setattr(trials_module, "atomic_write", killed)
    with pytest.raises(KeyboardInterrupt):
        store_results(tmp_path, [{"file": "a.py"}], {"m": {"input": 1}})

    assert read_status(tmp_path) == TrialStatus("running")
    with pytest.raises(LedgerError, match=r"the trial is running, not done"):
        read_results(tmp_path)


def test_results_of_an_earlier_attempt_are_not_read_as_complete(tmp_path: Path, monkeypatch):
    store_results(tmp_path, [{"file": "old.py"}], {"m": {"input": 1}})
    set_state(tmp_path, "interrupted", "usage limit")
    set_state(tmp_path, "running")  # rerun from the start, killed before done

    with pytest.raises(LedgerError, match=r"running, not done"):
        read_results(tmp_path)


def test_result_file_that_does_not_match_done_is_rejected(tmp_path: Path) -> None:
    store_results(tmp_path, [{"file": "a.py"}], {"m": {"input": 1}})
    (tmp_path / USAGE_FILE).write_text("{}")

    with pytest.raises(LedgerError, match=r"usage.json: does not match the hash"):
        read_results(tmp_path)


@pytest.mark.parametrize(
    "status",
    [
        "not json",
        {"state": "done"},
        {"state": "failed"},
        {"state": "pending", "reason": "x"},
        {"state": "paused"},
    ],
)
def test_malformed_status_is_rejected(tmp_path: Path, status) -> None:
    (tmp_path / STATUS_FILE).write_text(status if isinstance(status, str) else json.dumps(status))
    with pytest.raises(LedgerError, match=r"status.json"):
        read_status(tmp_path)


def test_runs_directory_is_git_ignored() -> None:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "-q", "runs/any-run/plan.json"], check=False
    )
    assert result.returncode == 0


# rbench run


ENGINE_TOML = f"""
name = "plain"
pin = {{ kind = "sha256", ref = "{"ab" * 32}" }}

[cli]
claude = "{CLI["claude"]}"
codex = "{CLI["codex"]}"

[roles.reviewer]
delivery = "flag"
default = {{ model = "claude-opus-5-5", effort = "high" }}
"""
MATRIX_TOML = """
stage = "screening"
tasks = "core"
attempts = 1

[[rounds]]
name = "r1"
budget = { claude = 1000 }

[[rounds.entries]]
engine = "plain"
roles.reviewer = { model = ["claude-opus-5-5", "claude-sonnet-5-5"] }
"""


def cli_inputs(root: Path) -> list[str]:
    (root / "engines").mkdir()
    (root / "engines" / "plain.toml").write_text(ENGINE_TOML)
    task_sets(root / "tasks")
    (root / "m.toml").write_text(MATRIX_TOML)
    (root / "images.json").write_text(
        json.dumps({"plain": {"image": f"rbench/plain@{DIGEST}", "clis": CLI}})
    )
    return [
        "run",
        str(root / "m.toml"),
        "--round",
        "r1",
        "--images",
        str(root / "images.json"),
        "--engines",
        str(root / "engines"),
        "--tasks",
        str(root / "tasks"),
        "--runs",
        str(root / "runs"),
    ]


def test_run_creates_the_run_and_stops_before_executing(tmp_path: Path, capsys) -> None:
    argv = cli_inputs(tmp_path)

    assert main([*argv, "--run-id", "smoke-1"]) == ExitCode.ERROR
    out, err = capsys.readouterr()
    assert "runs/smoke-1: 6 trials pending" in out
    assert "the trial executor is not implemented yet" in err
    plan = load_plan(tmp_path / "runs" / "smoke-1")
    assert len(plan["trials"]) == 2 * 3
    assert plan["matrix"]["sha256"] == hashlib.sha256(MATRIX_TOML.encode()).hexdigest()

    assert main(argv) == ExitCode.ERROR  # default run id: UTC time, stage and round
    names = sorted(p.name for p in (tmp_path / "runs").iterdir())
    assert len(names) == 2 and names[0].endswith("Z-screening-r1") and names[1] == "smoke-1"


def test_run_with_bad_inputs_creates_nothing(tmp_path: Path, capsys) -> None:
    argv = cli_inputs(tmp_path)
    (tmp_path / "images.json").write_text(json.dumps({}))

    assert main(argv) == ExitCode.ERROR
    assert "rbench run: engine 'plain': no image given" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()
