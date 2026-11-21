"""The run ledger (spec run-ledger: Frozen plan, Trial states are checkpointed; design D8).

Engines, tasks, images and matrices are synthetic and built in tmp_path.
"""

import dataclasses
import errno
import hashlib
import json
import re
import stat
import subprocess
from pathlib import Path

import pytest
from conftest import PLAIN_PROMPT_SHA256, write_plain_files
from test_hidden import git_repo, hidden_dir
from test_manifest import edit, write_manifest
from test_matrix import engines, matrix
from test_tasks import HID, HIDDEN, item, write_task

import rbench.ledger.plan as plan_module
import rbench.ledger.trials as trials_module
from rbench.cli import ExitCode, main
from rbench.engines.declaration import parse_engine
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
from rbench.ledger.plan import checkout_path
from rbench.matrix.expansion import Configuration, MatrixError, RoleSetting
from rbench.tasks import (
    CORE_LIST_FILE,
    GROUND_TRUTH_FILE,
    HIDDEN_DIR_ENV,
    MANIFEST_FILE,
    load_core_list,
    load_task_set,
)

ROOT = Path(__file__).resolve().parent.parent
CLI = {"claude": "2.1.316", "codex": "0.167.0"}
DIGEST = "sha256:" + "e" * 64
IMAGES = {name: Image(f"rbench/{name}@{DIGEST}", dict(CLI)) for name in ("plain", "panel")}
PLAIN_TWO_MODELS = {
    "engine": "plain",
    "roles": {"reviewer": {"model": ["claude-opus-5-5", "gpt-6.2-sol"]}},
}
BUDGET = {"claude": 1000, "codex": 2000}
FINDING = {"file": "a.py", "start_line": 1, "end_line": 2, "message": "Off by one."}
COUNTS = {"input": 3, "output": 2, "cache_read": 0, "cache_creation": 0}
USAGE = {"engine": {"claude-opus-5-5": COUNTS}, "normalization": {}}


def usage(**models) -> dict:
    """Engine usage of `models` (model id -> counts), no normalization step."""
    return {"engine": models, "normalization": {}}


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


def freeze(
    root: Path,
    m=None,
    round_name="r1",
    images=IMAGES,
    engines_=None,
    matrix_path="m.toml",
    matrix_sha256="f" * 64,
):
    if not (root / CORE_LIST_FILE).exists():
        task_sets(root)
    return freeze_plan(
        m or matrix(PLAIN_TWO_MODELS, budget=BUDGET),
        round_name,
        engines_ or engines(),
        [load_task_set(root / s, s) for s in ("reviewbench", "ours")],
        load_core_list(root / CORE_LIST_FILE),
        images,
        matrix_path,
        matrix_sha256,
    )


def plan_json(tmp_path: Path) -> tuple[Path, dict]:
    run_dir = create_run(tmp_path / "runs", "run-1", freeze(tmp_path))
    return run_dir, json.loads((run_dir / PLAN_FILE).read_text())


# Frozen plan


def test_plan_holds_what_the_spec_freezes(tmp_path: Path) -> None:
    plan = dataclasses.asdict(freeze(tmp_path))

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
            "roles": {
                "reviewer": {
                    "cli": None,
                    "delivery": "flag",
                    "default": {"model": "claude-opus-5-5", "effort": "high"},
                }
            },
        }
    }
    assert plan["matrix"] == {"path": "m.toml", "sha256": "f" * 64}


def test_plan_records_how_each_role_gets_its_settings(tmp_path: Path) -> None:
    def panel_plan(delivery: str):
        engines_ = engines()
        panel = engines_["panel"]
        roles = dict(panel.roles)
        roles["reviewers"] = dataclasses.replace(roles["reviewers"], delivery=delivery)
        engines_["panel"] = dataclasses.replace(panel, roles=roles)
        m = matrix({"engine": "panel", "default": True}, budget=BUDGET, engines_=engines_)
        return freeze(tmp_path, m, engines_=engines_)

    flag, config = panel_plan("flag"), panel_plan("config")

    assert flag.engines["panel"].roles["reviewers"].delivery == "flag"
    assert config.engines["panel"].roles["reviewers"].delivery == "config"
    assert flag.configurations == config.configurations  # same ids, different launch
    assert flag != config


def test_trials_are_queued_by_attempt_then_task_then_configuration(tmp_path: Path) -> None:
    m = matrix(PLAIN_TWO_MODELS, budget=BUDGET, stage="full", tasks="all", attempts=3)
    plan = freeze(tmp_path, m)

    configs = [c.id for c in plan.configurations]
    tasks = ["o-1", "o-2", "rb-1", "rb-2"]
    expected = [(a, t, c) for a in (1, 2, 3) for t in tasks for c in configs]
    assert [(t.attempt, t.task, t.configuration) for t in plan.trials] == expected
    assert len({t.id for t in plan.trials}) == 3 * 4 * 2


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
    assert [d.name for d in trial_dirs] == sorted(t.id for t in plan.trials)
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


def test_edited_matrix_warns_from_any_directory(tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "matrices").mkdir(parents=True)
    original = checkout / "matrices" / "m.toml"
    original.write_bytes(b"stage = 'screening'\n")
    path = checkout_path(original, checkout)
    assert path == "matrices/m.toml"
    sha = hashlib.sha256(original.read_bytes()).hexdigest()
    run_dir = create_run(
        tmp_path / "runs", "run-1", freeze(tmp_path, matrix_path=path, matrix_sha256=sha)
    )
    assert matrix_warning(load_plan(run_dir), checkout) is None

    # Resumed from another directory that holds an unedited copy at the same relative path.
    other = tmp_path / "elsewhere"
    (other / "matrices").mkdir(parents=True)
    (other / "matrices" / "m.toml").write_bytes(b"stage = 'screening'\n")
    monkeypatch.chdir(other)
    original.write_text("stage = 'full'\n")

    plan = load_plan(run_dir)
    assert plan.matrix.sha256 == sha
    warning = matrix_warning(plan, checkout)
    assert "matrices/m.toml changed or is gone" in warning
    assert "continues with its frozen plan" in warning


def test_matrix_outside_the_checkout_is_refused(tmp_path: Path) -> None:
    (tmp_path / "checkout").mkdir()
    with pytest.raises(LedgerError, match=r"outside the rbench checkout"):
        checkout_path(tmp_path / "m.toml", tmp_path / "checkout")


def test_plan_of_another_format_is_refused(tmp_path: Path) -> None:
    run_dir, plan = plan_json(tmp_path)
    for bad in (0, True, 1.0, "1"):
        (run_dir / PLAN_FILE).write_text(json.dumps(plan | {"format": bad}))
        with pytest.raises(LedgerError, match=r"format: plan format .*this rbench reads format 1"):
            load_plan(run_dir)


def _set(path: list, value):
    def change(plan: dict) -> None:
        target = plan
        for key in path[:-1]:
            target = target[key]
        if value is DELETE:
            del target[path[-1]]
        else:
            target[path[-1]] = value

    return change


DELETE = object()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (_set(["matrix"], DELETE), r"expected exactly the fields"),
        (_set(["trials", 0, "task"], "missing"), r"trials\[0\]\.task: does not match"),
        (_set(["trials", 1, "attempt"], True), r"trials\[1\]\.attempt"),
        (_set(["trials", 0, "id"], "other"), r"trials\[0\]\.id: does not match"),
        (lambda p: p["trials"].pop(), r"trials: 5 trials, but .* gives 6"),
        (lambda p: p["trials"].reverse(), r"trials\[0\]\.\w+: does not match"),
        (_set(["configurations", 0, "id"], "x"), r"configurations\[0\]\.id"),
        (_set(["configurations", 0, "providers"], []), r"configurations\[0\]\.providers"),
        (_set(["tasks", 0, "sha256"], "zz"), r"tasks\[0\]\.sha256"),
        (_set(["tasks", 0, "set"], "hidden"), r"tasks\[0\]\.set"),
        (_set(["engines", "plain", "image"], "x:latest"), r"engines\.plain: image"),
        (_set(["engines", "plain", "version"], "other"), r"engines\.plain\.version"),
        (_set(["engines", "plain", "roles", "reviewer", "delivery"], "x"), r"delivery"),
        (_set(["matrix", "path"], "/abs/m.toml"), r"matrix\.path"),
        (_set(["matrix", "path"], "../m.toml"), r"matrix\.path"),
        (_set(["round", "budget", "claude"], 0), r"round\.budget\.claude"),
        (_set(["round", "budget", "codex"], DELETE), r"round\.budget: expected a budget"),
        (_set(["attempts"], 2), r"attempts: the screening stage runs 1"),
        (_set(["stage"], "full"), r"attempts: the full stage runs 3"),
        (_set(["stage"], "smoke"), r"tasks: the smoke stage plans at most 1"),
        (_set(["stage"], "huge"), r"stage: expected one of"),
    ],
)
def test_malformed_plan_is_refused_naming_path_and_field(tmp_path: Path, change, message):
    run_dir, plan = plan_json(tmp_path)
    change(plan)
    (run_dir / PLAN_FILE).write_text(json.dumps(plan))

    with pytest.raises(LedgerError, match=message) as error:
        load_plan(run_dir)
    assert str(run_dir / PLAN_FILE) in str(error.value)


def _write(run_dir: Path, plan: dict) -> None:
    (run_dir / PLAN_FILE).write_text(json.dumps(plan))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            _set(["engines", "plain", "roles", "reviewer", "delivery"], "config"),
            r"engines\.plain: not a valid engine declaration: .*config delivery needs cli",
        ),
        (
            _set(["engines", "plain", "clis", "claude"], DELETE),
            r"engines\.plain: not a valid engine declaration: .*needs a pinned claude CLI",
        ),
        (
            _set(["engines", "plain", "roles", "reviewer", "default", "effort"], "huge"),
            r"engines\.plain: not a valid engine declaration: .*effort 'huge'",
        ),
        (
            _set(["engines", "plain", "pin"], {"kind": "git", "ref": "refs/tags/v1.0.0"}),
            r"engines\.plain\.pin\.ref: a tag",
        ),
    ],
)
def test_frozen_declaration_follows_the_declaration_rules(tmp_path: Path, change, message):
    run_dir, plan = plan_json(tmp_path)
    change(plan)
    _write(run_dir, plan)

    with pytest.raises(LedgerError, match=message):
        load_plan(run_dir)


def test_role_settings_with_consistent_ids_must_still_be_launchable(tmp_path: Path) -> None:
    run_dir, plan = plan_json(tmp_path)
    old = plan["configurations"][0]
    reviewer = old["settings"]["roles"]["reviewer"] | {"effort": "invalid-effort"}
    config = Configuration(
        "plain",
        old["settings"]["version"],
        (("reviewer", RoleSetting(tuple(reviewer["models"]), reviewer["effort"])),),
    )
    old_hash = old["id"].rsplit("-", 1)[1][:12]
    plan["configurations"][0] = old | {"id": config.id, "settings": config.settings}
    for trial in plan["trials"]:
        if trial["configuration"] == old["id"]:
            trial["configuration"] = config.id
            trial["id"] = trial["id"].replace(old_hash, config.hash[:12])
    _write(run_dir, plan)

    with pytest.raises(
        LedgerError, match=r"configurations\[0\]\.settings\.roles\.reviewer: effort"
    ):
        load_plan(run_dir)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (_set(["attempts"], 1000), r"attempts: the screening stage runs 1"),
        (lambda p: p["trials"].pop(), r"trials: 5 trials, but .* gives 6"),
        (lambda p: p["trials"].extend(p["trials"] * 1000), r"trials: 6006 trials, but"),
    ],
)
def test_trial_count_is_checked_before_any_queue_entry(tmp_path, monkeypatch, change, message):
    run_dir, plan = plan_json(tmp_path)
    change(plan)
    _write(run_dir, plan)
    calls = []
    real = plan_module.trial_id
    monkeypatch.setattr(plan_module, "trial_id", lambda *a: calls.append(a) or real(*a))

    with pytest.raises(LedgerError, match=message):
        load_plan(run_dir)
    assert calls == []


def test_nul_in_the_matrix_path_is_refused(tmp_path: Path) -> None:
    run_dir, plan = plan_json(tmp_path)
    plan["matrix"]["path"] = "matrices/m\x00.toml"
    _write(run_dir, plan)

    with pytest.raises(LedgerError, match=r"matrix\.path: expected a plain path"):
        load_plan(run_dir)


DEEP = b"[" * 20000 + b"0" + b"]" * 20000


def test_deeply_nested_json_is_refused_with_its_file(tmp_path: Path) -> None:
    run_dir, _ = plan_json(tmp_path)
    (run_dir / PLAN_FILE).write_bytes(DEEP)
    with pytest.raises(LedgerError, match=r"plan\.json: cannot read the plan"):
        load_plan(run_dir)

    images = tmp_path / "images.json"
    images.write_bytes(DEEP)
    with pytest.raises(LedgerError, match=r"images file .*images\.json: cannot read it"):
        load_images(images)

    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / STATUS_FILE).write_bytes(DEEP)
    with pytest.raises(LedgerError, match=r"status\.json: cannot read the trial status"):
        read_status(trial)

    (trial / FINDINGS_FILE).write_bytes(DEEP)
    (trial / USAGE_FILE).write_text(json.dumps(USAGE))
    status = {
        "state": "done",
        "findings_sha256": hashlib.sha256(DEEP).hexdigest(),
        "usage_sha256": hashlib.sha256((trial / USAGE_FILE).read_bytes()).hexdigest(),
    }
    (trial / STATUS_FILE).write_text(json.dumps(status))
    with pytest.raises(LedgerError, match=r"findings\.json: not valid JSON"):
        read_results(trial)


def test_symlinked_matrix_is_refused(tmp_path: Path) -> None:
    (tmp_path / "m.toml").write_text("stage = 'screening'\n")
    (tmp_path / "active.toml").symlink_to("m.toml")
    with pytest.raises(LedgerError, match=r"active\.toml is a symlink"):
        checkout_path(tmp_path / "active.toml", tmp_path)
    assert checkout_path(tmp_path / "m.toml", tmp_path) == "m.toml"


def test_plan_with_a_repeated_key_is_refused(tmp_path: Path) -> None:
    run_dir, plan = plan_json(tmp_path)
    text = json.dumps(plan).replace('"stage": ', '"stage": "full", "stage": ', 1)
    (run_dir / PLAN_FILE).write_text(text)

    with pytest.raises(LedgerError, match=r"cannot read the plan: a key is repeated"):
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
        create_run(tmp_path / "runs", run_id, freeze(tmp_path))


@pytest.mark.parametrize(
    ("images", "message"),
    [
        ({}, r"engine 'plain': no image given"),
        (
            {"plain": Image("rbench/plain:latest", dict(CLI))},
            r"image 'rbench/plain:latest' is a floating reference",
        ),
        (
            {"plain": Image(f"rbench/plain@{DIGEST}", CLI | {"claude": "2.1.317"})},
            r"has CLIs .*2\.1\.317.*, but the declaration pins",
        ),
        (
            IMAGES | {"unused": Image("latest", {"invalid": "latest"})},
            r"engine 'unused': image 'latest' is a floating reference",
        ),
        (
            IMAGES | {"unused": Image(f"x@{DIGEST}", {"invalid": "1.0.0"})},
            r"engine 'unused': clis: unknown CLI 'invalid'",
        ),
        (
            IMAGES | {"unused": Image(f"x@{DIGEST}", {"codex": "^1.0"})},
            r"engine 'unused': clis\.codex '\^1\.0' is a floating reference",
        ),
    ],
)
def test_images_are_checked(tmp_path: Path, images, message) -> None:
    with pytest.raises(LedgerError, match=message):
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


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            f'{{"plain": null, "plain": {{"image": "x@{DIGEST}", "clis": {{"claude": "2.1.316"}}}}}}',
            r"cannot read it: a key is repeated",
        ),
        (json.dumps({"plain": {"image": f"x@{DIGEST}"}}), r"'plain': expected exactly the fields"),
        (
            json.dumps({"unused": {"image": "latest", "clis": {"invalid": "latest"}}}),
            r"'unused': image 'latest' is a floating reference",
        ),
        (json.dumps({"plain": {"image": f"x@{DIGEST}", "clis": []}}), r"'plain': clis"),
        (json.dumps([]), r"expected an object of engines"),
    ],
)
def test_load_images_validates_every_entry(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "images.json"
    path.write_text(text)
    with pytest.raises(LedgerError, match=message):
        load_images(path)


def test_load_images_reads_a_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "images.json"
    path.write_text(json.dumps({"plain": {"image": f"x@{DIGEST}", "clis": CLI}}))
    assert load_images(path) == {"plain": Image(f"x@{DIGEST}", CLI)}


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
    store_results(tmp_path, [FINDING, FINDING | {"severity": "major"}], USAGE)

    assert read_status(tmp_path).state == "done"
    assert read_results(tmp_path) == ([FINDING, FINDING | {"severity": "major"}], USAGE)


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
        store_results(tmp_path, [FINDING], USAGE)

    assert read_status(tmp_path) == TrialStatus("running")
    with pytest.raises(LedgerError, match=r"the trial is running, not done"):
        read_results(tmp_path)


def test_done_trial_refuses_a_second_store(tmp_path: Path, monkeypatch) -> None:
    set_state(tmp_path, "running")
    store_results(tmp_path, [FINDING], USAGE)
    real = trials_module.atomic_write
    writes = []

    def interrupted(path, data):
        writes.append(Path(path).name)
        if Path(path).name == USAGE_FILE:
            raise InterruptedError
        real(path, data)

    monkeypatch.setattr(trials_module, "atomic_write", interrupted)
    with pytest.raises(LedgerError, match=r"already done; its results stay as stored"):
        store_results(tmp_path, [FINDING | {"file": "new.py"}], usage(m=COUNTS | {"input": 9}))

    assert writes == []
    assert read_results(tmp_path) == ([FINDING], USAGE)


def test_store_needs_a_readable_status(tmp_path: Path) -> None:
    with pytest.raises(LedgerError, match=r"cannot read the trial status"):
        store_results(tmp_path, [FINDING], USAGE)
    assert list(tmp_path.iterdir()) == []


def test_results_of_an_earlier_attempt_are_not_read_as_complete(tmp_path: Path) -> None:
    set_state(tmp_path, "running")
    store_results(tmp_path, [FINDING], USAGE)
    set_state(tmp_path, "interrupted", "usage limit")
    set_state(tmp_path, "running")  # rerun from the start, killed before done

    with pytest.raises(LedgerError, match=r"running, not done"):
        read_results(tmp_path)


def test_result_file_that_does_not_match_done_is_rejected(tmp_path: Path) -> None:
    set_state(tmp_path, "running")
    store_results(tmp_path, [FINDING], USAGE)
    (tmp_path / USAGE_FILE).write_text("{}")

    with pytest.raises(LedgerError, match=r"usage.json: does not match the hash"):
        read_results(tmp_path)


@pytest.mark.parametrize(
    ("findings", "usage", "message"),
    [
        ([42], USAGE, r"finding 0: expected an object"),
        ([{"file": "a.py"}], USAGE, r"finding 0: expected the fields"),
        ([FINDING | {"extra": 1}], USAGE, r"finding 0: expected the fields"),
        ([FINDING, FINDING | {"file": " "}], USAGE, r"finding 1: file"),
        ([FINDING | {"start_line": 0}], USAGE, r"finding 0: start_line"),
        ([FINDING | {"start_line": True}], USAGE, r"finding 0: start_line"),
        ([FINDING | {"start_line": 3, "end_line": 2}], USAGE, r"finding 0: end_line"),
        ([FINDING | {"severity": 1}], USAGE, r"finding 0: severity"),
        ({"file": "a.py"}, USAGE, r"findings must be a list"),
        ([FINDING], {"input": -1}, r"usage must be an object of exactly engine, normalization"),
        ([FINDING], [], r"usage must be an object"),
        ([FINDING], {"engine": {"m": COUNTS}}, r"usage must be an object of exactly"),
        ([FINDING], usage(input=-1), r"engine usage of model 'input': expected exactly"),
        ([FINDING], usage(m=COUNTS | {"output": -1}), r"'m': output: expected a token count"),
        ([FINDING], usage(m=COUNTS | {"output": 1.5}), r"'m': output: expected a token count"),
        ([FINDING], {"engine": {"": COUNTS}, "normalization": {}}, r"model '': expected a model"),
        ([FINDING], USAGE | {"normalization": []}, r"usage\.normalization must be an object"),
        (
            [FINDING],
            USAGE | {"normalization": {"m": {"input": 1}}},
            r"normalization usage of model 'm': expected exactly",
        ),
    ],
)
def test_invalid_results_are_rejected_whole(tmp_path: Path, findings, usage, message) -> None:
    set_state(tmp_path, "running")
    with pytest.raises(LedgerError, match=message):
        store_results(tmp_path, findings, usage)
    assert read_status(tmp_path) == TrialStatus("running")
    assert sorted(p.name for p in tmp_path.iterdir()) == [STATUS_FILE]


@pytest.mark.parametrize(
    ("findings", "usage", "message"),
    [
        (b"42", b"{}", r"findings must be a list"),
        (b"[]", b"[]", r"usage must be an object"),
        (b"[{}]", b"{}", r"finding 0: expected the fields"),
        (
            b"[]",
            b'{"engine": {"m": {"input": -1}}, "normalization": {}}',
            r"engine usage of model 'm': expected exactly",
        ),
        (b'[{"file": "a", "file": "b"}]', b"{}", r"findings.json: not valid JSON"),
    ],
)
def test_done_with_matching_hashes_but_invalid_contents_is_rejected(
    tmp_path: Path, findings: bytes, usage: bytes, message: str
) -> None:
    (tmp_path / FINDINGS_FILE).write_bytes(findings)
    (tmp_path / USAGE_FILE).write_bytes(usage)
    status = {
        "state": "done",
        "findings_sha256": hashlib.sha256(findings).hexdigest(),
        "usage_sha256": hashlib.sha256(usage).hexdigest(),
    }
    (tmp_path / STATUS_FILE).write_text(json.dumps(status))

    with pytest.raises(LedgerError, match=message):
        read_results(tmp_path)


@pytest.mark.parametrize(
    "status",
    [
        "not json",
        {"state": "done"},
        {"state": "done", "findings_sha256": "x", "usage_sha256": "f" * 64},
        {"state": "done", "findings_sha256": "F" * 64, "usage_sha256": "f" * 64},
        {"state": "failed"},
        {"state": "failed", "reason": " "},
        {"state": "failed", "reason": 7},
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
pin = {{ kind = "sha256", ref = "{PLAIN_PROMPT_SHA256}" }}

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


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch) -> Path:
    """A synthetic checkout in tmp_path, standing in for the rbench checkout."""
    monkeypatch.setattr(plan_module, "CHECKOUT_ROOT", tmp_path)
    return tmp_path


def cli_inputs(root: Path, images: dict | None = None) -> list[str]:
    (root / "engines").mkdir()
    (root / "engines" / "plain.toml").write_text(ENGINE_TOML)
    write_plain_files(root / "engines", CLI["codex"])
    task_sets(root / "tasks")
    (root / "matrices").mkdir()
    (root / "matrices" / "m.toml").write_text(MATRIX_TOML)
    (root / "images.json").write_text(
        json.dumps(images or {"plain": {"image": f"rbench/plain@{DIGEST}", "clis": CLI}})
    )
    return [
        "run",
        str(root / "matrices" / "m.toml"),
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


def test_run_creates_the_run_and_stops_before_executing(
    checkout: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(HIDDEN_DIR_ENV, str(checkout / "nonexistent"))
    argv = cli_inputs(checkout)

    assert main([*argv, "--run-id", "smoke-1"]) == ExitCode.ERROR
    out, err = capsys.readouterr()
    assert "runs/smoke-1: 6 trials pending" in out
    assert "the trial executor is not implemented yet" in err
    plan = load_plan(checkout / "runs" / "smoke-1")
    assert len(plan.trials) == 2 * 3
    assert plan.matrix.path == "matrices/m.toml"
    assert plan.matrix.sha256 == hashlib.sha256(MATRIX_TOML.encode()).hexdigest()
    assert str(checkout) not in (checkout / "runs" / "smoke-1" / PLAN_FILE).read_text()

    assert main(argv) == ExitCode.ERROR  # default run id: UTC time, stage and round
    names = sorted(p.name for p in (checkout / "runs").iterdir())
    assert len(names) == 2 and names[0].endswith("Z-screening-r1") and names[1] == "smoke-1"


def test_run_resolves_a_relative_matrix_path_from_any_directory(checkout: Path, monkeypatch):
    argv = cli_inputs(checkout)
    monkeypatch.chdir(checkout / "matrices")
    argv[1] = "m.toml"

    main([*argv, "--run-id", "r"])
    plan = load_plan(checkout / "runs" / "r")
    assert plan.matrix.path == "matrices/m.toml"
    monkeypatch.chdir(checkout / "tasks")
    assert matrix_warning(plan) is None


@pytest.mark.parametrize(
    ("images", "message"),
    [
        ({"other": {"image": f"x@{DIGEST}", "clis": CLI}}, r"engine 'plain': no image given"),
        (
            {
                "plain": {"image": f"rbench/plain@{DIGEST}", "clis": CLI},
                "unused": {"image": "latest", "clis": {"invalid": "latest"}},
            },
            r"engine 'unused': image 'latest' is a floating reference",
        ),
    ],
)
def test_run_with_bad_inputs_creates_nothing(checkout: Path, capsys, images, message) -> None:
    argv = cli_inputs(checkout, images)

    assert main([*argv, "--run-id", "fixture"]) == ExitCode.ERROR
    err = capsys.readouterr().err
    assert err.startswith("rbench run: ")
    assert re.search(message, err)
    assert not (checkout / "runs").exists()


def test_run_refuses_a_matrix_outside_the_checkout(checkout: Path, tmp_path_factory, capsys):
    argv = cli_inputs(checkout)
    outside = tmp_path_factory.mktemp("outside") / "m.toml"
    outside.write_text(MATRIX_TOML)
    argv[1] = str(outside)

    assert main(argv) == ExitCode.ERROR
    assert "outside the rbench checkout" in capsys.readouterr().err
    assert not (checkout / "runs").exists()


def test_run_refuses_a_symlinked_matrix(checkout: Path, capsys) -> None:
    argv = cli_inputs(checkout)
    (checkout / "matrices" / "active.toml").symlink_to("m.toml")
    argv[1] = str(checkout / "matrices" / "active.toml")

    assert main(argv) == ExitCode.ERROR
    assert "is a symlink" in capsys.readouterr().err
    assert not (checkout / "runs").exists()


@pytest.fixture
def full_inputs(tmp_path: Path, monkeypatch) -> tuple[Path, list[str], Path]:
    checkout = git_repo(tmp_path / "public")
    monkeypatch.setattr(plan_module, "CHECKOUT_ROOT", checkout)
    monkeypatch.chdir(checkout)
    argv = cli_inputs(checkout)
    Path(argv[1]).write_text(
        MATRIX_TOML.replace('"screening"', '"full"')
        .replace('"core"', '"all"')
        .replace("attempts = 1", "attempts = 3")
    )
    hidden = hidden_dir(tmp_path)
    task = write_task(hidden, HID, task_set="hidden")
    write_manifest(hidden, task_set="hidden")
    monkeypatch.setenv(HIDDEN_DIR_ENV, str(hidden))
    return checkout, [*argv, "--run-id", "fixture"], task


def assert_private_output(output: str, task: Path, *, includes_id: bool = False) -> None:
    gt = json.loads((task / GROUND_TRUTH_FILE).read_text())
    for secret in (str(task.parent), gt["canary"], HIDDEN, gt["items"][0]["message"]):
        assert secret not in output
    if not includes_id:
        assert task.name not in output


def test_full_run_freezes_hidden_task_in_ignored_results(full_inputs, capsys) -> None:
    checkout, argv, task = full_inputs

    assert main(argv) == ExitCode.ERROR  # The executor is still pending.
    out, err = capsys.readouterr()
    assert "30 trials pending" in out
    assert "the trial executor is not implemented yet" in err
    assert_private_output(out + err, task)

    run_dir = checkout / "runs" / "fixture"
    plan = load_plan(run_dir)
    manifest = json.loads((task.parent / MANIFEST_FILE).read_text())
    assert plan.task_sets == {"hidden": manifest["version"], "ours": 1, "reviewbench": 1}
    assert [t.id for t in plan.tasks] == sorted([task.name, "o-1", "o-2", "rb-1", "rb-2"])
    (planned,) = (t for t in plan.tasks if t.set == "hidden")
    assert dataclasses.asdict(planned) == {
        "id": task.name,
        "set": "hidden",
        "sha256": manifest["tasks"][task.name]["sha256"],
    }
    hidden_trials = [t for t in plan.trials if t.task == task.name]
    assert len(hidden_trials) == 6
    for trial in hidden_trials:
        assert read_status(run_dir / TRIALS_DIR / trial.id) == TrialStatus("pending")
    for path in run_dir.rglob("*"):
        assert not path.is_symlink()
        if path.is_file():
            assert_private_output(path.read_text(), task, includes_id=(path.name == PLAN_FILE))


def test_full_run_requires_a_configured_hidden_directory(full_inputs, monkeypatch, capsys) -> None:
    checkout, argv, task = full_inputs
    monkeypatch.delenv(HIDDEN_DIR_ENV)

    assert main(argv) == ExitCode.ERROR
    err = capsys.readouterr().err
    assert f"set {HIDDEN_DIR_ENV} to the hidden task directory" in err
    assert_private_output(err, task)
    assert not (checkout / "runs").exists()


def test_full_run_rejects_a_changed_hidden_task(full_inputs, capsys) -> None:
    checkout, argv, task = full_inputs
    edit(task / GROUND_TRUTH_FILE, lambda gt: gt["items"][0].update(message="Changed finding."))

    assert main(argv) == ExitCode.ERROR
    err = capsys.readouterr().err
    assert "content hash does not match" in err
    assert_private_output(err, task)
    assert not (checkout / "runs").exists()


def test_full_run_hidden_input_io_error_keeps_its_details_private(
    full_inputs, monkeypatch, capsys
) -> None:
    checkout, argv, task = full_inputs

    def fail_load(config):
        raise OSError(errno.EACCES, "Permission denied", str(task))

    monkeypatch.setattr(plan_module, "load_hidden_task_set", fail_load)
    assert main(argv) == ExitCode.ERROR
    err = capsys.readouterr().err
    assert "cannot read the hidden task set: Permission denied" in err
    assert_private_output(err, task)
    assert not (checkout / "runs").exists()


@pytest.mark.parametrize(
    "destination", ["unignored", "symlink", "unignored staging", "unignored final"]
)
def test_full_run_rejects_unignored_results(full_inputs, monkeypatch, capsys, destination) -> None:
    checkout, argv, task = full_inputs
    output = checkout / "output"
    if destination == "symlink":
        output = checkout / "runs"
        output.symlink_to(task.parent, target_is_directory=True)
    elif destination == "unignored staging":
        (checkout / ".gitignore").write_text("/output/fixture\n")
        output.mkdir()
    elif destination == "unignored final":
        (checkout / ".gitignore").write_text("/output/.rbench.*\n")
    else:
        output /= task.name

    def unexpected_write(*args):
        pytest.fail("attempted to write a hidden run at an unignored destination")

    monkeypatch.setattr(plan_module, "atomic_write", unexpected_write)
    assert main([*argv, "--runs", str(output)]) == ExitCode.ERROR
    err = capsys.readouterr().err
    assert "path git ignores" in err
    assert_private_output(err, task)
    assert not (output / "fixture").exists()
    if destination == "unignored staging":
        assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    ("suffix", "message"), [("/invalid", "bad run id"), ("", "run already exists")]
)
def test_full_run_id_errors_omit_private_values(full_inputs, capsys, suffix, message) -> None:
    checkout, argv, task = full_inputs
    if not suffix:
        (checkout / "runs" / task.name).mkdir(parents=True)

    assert main([*argv, "--run-id", task.name + suffix]) == ExitCode.ERROR
    err = capsys.readouterr().err
    assert message in err
    assert_private_output(err, task)


def test_full_run_creation_error_keeps_the_hidden_trial_private(
    full_inputs, monkeypatch, capsys
) -> None:
    checkout, argv, task = full_inputs

    def fail_hidden_trial(trial_dir, state):
        if trial_dir.name.startswith(task.name + "--"):
            assert task.name not in trial_dir.parent.parent.name
            raise OSError(errno.ENOSPC, "No space left on device", str(trial_dir))
        set_state(trial_dir, state)

    monkeypatch.setattr(plan_module, "set_state", fail_hidden_trial)
    assert main([*argv, "--run-id", task.name]) == ExitCode.ERROR
    err = capsys.readouterr().err
    assert "cannot create the run with hidden tasks: No space left on device" in err
    assert_private_output(err, task)
    assert list((checkout / "runs").iterdir()) == []


def test_full_run_plan_read_error_keeps_its_details_private(
    full_inputs, monkeypatch, capsys
) -> None:
    checkout, argv, task = full_inputs

    def fail_read(run_dir):
        raise LedgerError(f"cannot read plan for {task}")

    monkeypatch.setattr("rbench.cli.load_plan", fail_read)
    assert main(argv) == ExitCode.ERROR
    err = capsys.readouterr().err
    assert "cannot read the newly created plan.json" in err
    assert_private_output(err, task)
    assert (checkout / "runs" / "fixture" / PLAN_FILE).is_file()
