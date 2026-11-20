"""Task-set manifests, content hashes and the core list (spec review-tasks: Task set versions,
Core task subset; design D11).

Every task here is synthetic and built in tmp_path.
"""

import json
from pathlib import Path

import pytest
from test_tasks import HID, HIDDEN, item, write_task

from rbench.tasks import (
    CORE_LIST_FILE,
    GROUND_TRUTH_FILE,
    MANIFEST_FILE,
    TASK_FILE,
    TaskError,
    load_core_list,
    load_task_set,
    load_tasks,
    select_core,
    task_content_sha256,
)

REPO_TASKS = Path(__file__).resolve().parent.parent / "tasks"


def hashes(set_dir: Path, hidden_canary_prefix: str | None = None) -> dict[str, str]:
    tasks = load_tasks(set_dir, hidden_canary_prefix=hidden_canary_prefix)
    return {task.id: task_content_sha256(task) for task in tasks}


def write_manifest(set_dir: Path, task_set: str = "ours", changelog=None, **overrides) -> Path:
    """A version 1 manifest of every task in `set_dir`, as an import would record it."""
    current = hashes(set_dir, HIDDEN if task_set == "hidden" else None)
    data = {
        "set": task_set,
        "version": 1,
        "tasks": {task_id: {"sha256": sha} for task_id, sha in current.items()},
        "changelog": changelog or [{"version": 1, "note": "Initial import.", "changed": current}],
        **overrides,
    }
    path = set_dir / MANIFEST_FILE
    path.write_text(json.dumps(data, indent=2))
    return path


def edit(path: Path, change) -> None:
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def two_task_set(tmp_path: Path) -> Path:
    set_dir = tmp_path / "ours"
    write_task(set_dir, "t-1")
    write_task(set_dir, "t-2")
    write_manifest(set_dir)
    return set_dir


@pytest.mark.parametrize(
    ("name", "change"),
    [
        ("repo", lambda d: d.update(repo="example/other")),
        ("pr", lambda d: d.update(pr=2)),
        ("head", lambda d: d.update(head="d" * 40)),
        ("language", lambda d: d.update(language="Go")),
        ("title", lambda d: d.update(title="Fix the loop bound")),
        ("description", lambda d: d.update(description="Fixes #1.")),
        ("archive_sha256", lambda d: d.update(archive_sha256="e" * 64)),
    ],
)
def test_hash_covers_metadata_description_and_archive_hash(tmp_path: Path, name, change) -> None:
    task_dir = write_task(tmp_path, "t-1")
    before = hashes(tmp_path)["t-1"]
    edit(task_dir / TASK_FILE, change)
    assert hashes(tmp_path)["t-1"] != before, name


@pytest.mark.parametrize(
    "change",
    [
        lambda gt: gt["items"][0].update(message="Off-by-one in the upper bound."),
        lambda gt: gt["items"][0].update(severity="should"),
        lambda gt: gt["items"][0].update(end_line=6),
        lambda gt: gt["items"].append(item(id="i2")),
        lambda gt: gt.update(canary=gt["canary"][:-1] + "f"),
    ],
)
def test_hash_covers_ground_truth(tmp_path: Path, change) -> None:
    task_dir = write_task(tmp_path, "t-1")
    before = hashes(tmp_path)["t-1"]
    edit(task_dir / GROUND_TRUTH_FILE, change)
    assert hashes(tmp_path)["t-1"] != before


def test_hash_ignores_formatting_and_key_order(tmp_path: Path) -> None:
    task_dir = write_task(tmp_path, "t-1")
    before = hashes(tmp_path)["t-1"]
    for name in (TASK_FILE, GROUND_TRUTH_FILE):
        data = json.loads((task_dir / name).read_text())
        reordered = dict(reversed(list(data.items())))
        (task_dir / name).write_text(json.dumps(reordered, indent=4) + "\n")
    assert hashes(tmp_path)["t-1"] == before


def test_set_matching_its_manifest_loads(tmp_path: Path) -> None:
    set_dir = two_task_set(tmp_path)
    edit(set_dir / MANIFEST_FILE, lambda m: m["tasks"]["t-1"].update(license="MIT"))
    manifest, tasks = load_task_set(set_dir)
    assert [t.id for t in tasks] == ["t-1", "t-2"]
    assert manifest.version == 1
    assert manifest.tasks["t-1"].license == "MIT"
    assert manifest.tasks["t-2"].license is None
    assert manifest.changelog[0].changed.keys() == {"t-1", "t-2"}


def test_ground_truth_fixed_without_new_version_fails_naming_task(tmp_path: Path) -> None:
    set_dir = two_task_set(tmp_path)
    edit(set_dir / "t-2" / GROUND_TRUTH_FILE, lambda gt: gt["items"][0].update(severity="nit"))
    with pytest.raises(TaskError, match=r"task .*t-2: content hash .* new set version"):
        load_task_set(set_dir)


def test_new_hash_without_changelog_entry_fails(tmp_path: Path) -> None:
    set_dir = two_task_set(tmp_path)
    edit(set_dir / "t-2" / GROUND_TRUTH_FILE, lambda gt: gt["items"][0].update(severity="nit"))
    new = hashes(set_dir)["t-2"]
    edit(set_dir / MANIFEST_FILE, lambda m: m["tasks"]["t-2"].update(sha256=new))
    with pytest.raises(TaskError, match=r"task 't-2': its hash differs from changelog version 1"):
        load_task_set(set_dir)


def test_new_version_with_one_changed_task(tmp_path: Path) -> None:
    set_dir = tmp_path / "ours"
    for task_id in ("t-1", "t-2", "t-3"):
        write_task(set_dir, task_id)
    v1 = hashes(set_dir)
    write_manifest(set_dir)

    edit(set_dir / "t-2" / GROUND_TRUTH_FILE, lambda gt: gt["items"][0].update(severity="nit"))
    v2 = hashes(set_dir)
    assert {k for k in v1 if v1[k] != v2[k]} == {"t-2"}

    def bump(m: dict) -> None:
        m["version"] = 2
        m["tasks"]["t-2"]["sha256"] = v2["t-2"]
        m["changelog"].append({"version": 2, "note": "Fix t-2.", "changed": {"t-2": v2["t-2"]}})

    edit(set_dir / MANIFEST_FILE, bump)
    manifest, _ = load_task_set(set_dir)
    assert manifest.version == 2
    assert list(manifest.changelog[-1].changed) == ["t-2"]
    assert {k: v.sha256 for k, v in manifest.tasks.items()} == v2


def test_removed_task_is_recorded_as_null(tmp_path: Path) -> None:
    set_dir = two_task_set(tmp_path)

    def remove(m: dict) -> None:
        m["version"] = 2
        del m["tasks"]["t-2"]
        m["changelog"].append({"version": 2, "note": "Drop t-2.", "changed": {"t-2": None}})

    edit(set_dir / MANIFEST_FILE, remove)
    (set_dir / "t-2" / TASK_FILE).unlink()
    (set_dir / "t-2" / GROUND_TRUTH_FILE).unlink()
    (set_dir / "t-2").rmdir()
    manifest, tasks = load_task_set(set_dir)
    assert [t.id for t in tasks] == ["t-1"] and list(manifest.tasks) == ["t-1"]


def test_hidden_set_is_checked_against_its_manifest(tmp_path: Path) -> None:
    set_dir = tmp_path / "hidden"
    write_task(set_dir, HID, task_set="hidden")
    write_manifest(set_dir, task_set="hidden")
    manifest, tasks = load_task_set(set_dir, hidden_canary_prefix=HIDDEN)
    assert manifest.set == "hidden" and [t.id for t in tasks] == [HID]
    edit(set_dir / HID / GROUND_TRUTH_FILE, lambda gt: gt["items"][0].update(message="Changed."))
    with pytest.raises(TaskError) as err:
        load_task_set(set_dir, hidden_canary_prefix=HIDDEN)
    assert HID in str(err.value) and HIDDEN not in str(err.value)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda m: m.update(version=2),
            r"'version' is 2, but the last changelog entry is version 1",
        ),
        (lambda m: m["changelog"][0].update(version=2), r"changelog entry 0: 'version' must be 1"),
        (lambda m: m["changelog"][0].update(changed={}), r"'changed' must name at least one task"),
        (lambda m: m["changelog"][0].update(note=" "), r"'note' must be a non-empty string"),
        (lambda m: m.update(changelog=[]), r"'changelog' must be a non-empty list"),
        (lambda m: m["changelog"][0]["changed"].pop("t-2"), r"'t-2': no changelog entry names it"),
        (lambda m: m["tasks"].pop("t-2"), r"'t-2': changelog version 1 records it, but 'tasks'"),
        (lambda m: m.update(set="nope"), r"'set' must be one of"),
        (lambda m: m.update(version="1"), r"'version' must be a positive integer"),
        (lambda m: m.update(extra=1), r"unknown extra"),
        (lambda m: m["tasks"]["t-1"].update(sha256="A" * 64), r"'sha256' must be 64 lowercase hex"),
        (lambda m: m["tasks"]["t-1"].update(license=""), r"'license' must be a non-empty string"),
        (lambda m: m["tasks"]["t-1"].update(owner="x"), r"unknown owner"),
        (lambda m: m["tasks"].update({"Bad id": m["tasks"]["t-1"]}), r"invalid task id 'Bad id'"),
    ],
)
def test_malformed_manifest_is_rejected(tmp_path: Path, change, message) -> None:
    set_dir = two_task_set(tmp_path)
    edit(set_dir / MANIFEST_FILE, change)
    with pytest.raises(TaskError, match=message):
        load_task_set(set_dir)


def test_manifest_and_directories_must_agree(tmp_path: Path) -> None:
    set_dir = two_task_set(tmp_path)
    write_task(set_dir, "t-3")
    with pytest.raises(TaskError, match=r"does not list tasks: t-3"):
        load_task_set(set_dir)

    set_dir = tmp_path / "other"
    write_task(set_dir, "t-1")
    write_manifest(set_dir)
    write_task(tmp_path / "scratch", "t-9")
    sha = hashes(tmp_path / "scratch")["t-9"]

    def add(m: dict) -> None:
        m["tasks"]["t-9"] = {"sha256": sha}
        m["changelog"][0]["changed"]["t-9"] = sha

    edit(set_dir / MANIFEST_FILE, add)
    with pytest.raises(TaskError, match=r"lists tasks with no directory: t-9"):
        load_task_set(set_dir)


def test_manifest_set_must_match_its_tasks(tmp_path: Path) -> None:
    set_dir = tmp_path / "rb"
    write_task(set_dir, "t-1", task_set="reviewbench", items=[item(source=None)])
    write_manifest(set_dir, task_set="ours")
    with pytest.raises(TaskError, match=r"t-1 is in set 'reviewbench', but .* is for 'ours'"):
        load_task_set(set_dir)


def test_missing_or_repeated_manifest_keys(tmp_path: Path) -> None:
    set_dir = tmp_path / "ours"
    write_task(set_dir, "t-1")
    with pytest.raises(TaskError, match=r"missing file manifest.json"):
        load_task_set(set_dir)
    path = write_manifest(set_dir)
    path.write_text(path.read_text().replace('"version": 1,', '"version": 1, "version": 1,'))
    with pytest.raises(TaskError, match=r"a key is repeated"):
        load_task_set(set_dir)


def core_setup(tmp_path: Path, ids: list[str]) -> tuple[Path, list]:
    write_task(tmp_path / "reviewbench", "rb-1", task_set="reviewbench", items=[item(source=None)])
    write_task(tmp_path / "reviewbench", "rb-2", task_set="reviewbench", items=[item(source=None)])
    write_task(tmp_path / "ours", "o-1")
    write_task(tmp_path / "ours", "o-2")
    tasks = load_tasks(tmp_path / "reviewbench", tmp_path / "ours")
    path = tmp_path / CORE_LIST_FILE
    path.write_text(json.dumps({"version": 3, "tasks": ids}))
    return path, tasks


def test_selecting_core_tasks_plans_exactly_those(tmp_path: Path) -> None:
    path, tasks = core_setup(tmp_path, ["rb-2", "o-1"])
    core = load_core_list(path)
    assert core.version == 3
    assert [t.id for t in select_core(core, tasks)] == ["o-1", "rb-2"]


@pytest.mark.parametrize(
    ("ids", "message"),
    [
        (["rb-1", "o-1", "o-9"], r"tasks not loaded: o-9"),
        (["rb-1", "rb-2"], r"must include tasks of both .*; none from ours"),
        (["o-1"], r"none from reviewbench"),
    ],
)
def test_core_list_against_loaded_tasks(tmp_path: Path, ids, message) -> None:
    path, tasks = core_setup(tmp_path, ids)
    with pytest.raises(TaskError, match=message):
        select_core(load_core_list(path), tasks)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"version": 1, "tasks": []}, r"'tasks' must be a non-empty list"),
        ({"version": 0, "tasks": ["o-1"]}, r"'version' must be a positive integer"),
        ({"tasks": ["o-1"]}, r"missing version"),
        ({"version": 1, "tasks": ["o-1", "o-1"]}, r"'o-1' is listed twice"),
        ({"version": 1, "tasks": [HID]}, r"hidden tasks cannot be core tasks"),
    ],
)
def test_malformed_core_list_is_rejected(tmp_path: Path, data, message) -> None:
    path = tmp_path / CORE_LIST_FILE
    path.write_text(json.dumps(data))
    with pytest.raises(TaskError, match=message):
        load_core_list(path)


def check_tasks_root(root: Path) -> None:
    """The repository check: recompute every task's hash in every set under `root` (design D11)."""
    tasks = []
    for set_dir in sorted(root.iterdir()):
        if set_dir.is_dir() and not set_dir.name.startswith("."):
            tasks += load_task_set(set_dir)[1]
    if (root / CORE_LIST_FILE).exists():
        select_core(load_core_list(root / CORE_LIST_FILE), tasks)


def test_committed_task_sets_match_their_manifests() -> None:
    if REPO_TASKS.is_dir():  # no task set is committed yet
        check_tasks_root(REPO_TASKS)


def test_repository_check_names_an_edited_task(tmp_path: Path) -> None:
    core_setup(tmp_path, ["rb-1", "o-1"])
    write_manifest(tmp_path / "reviewbench", task_set="reviewbench")
    write_manifest(tmp_path / "ours")
    check_tasks_root(tmp_path)
    edit(tmp_path / "ours" / "o-2" / TASK_FILE, lambda d: d.update(title="Retitled"))
    with pytest.raises(TaskError, match=r"task .*o-2: content hash"):
        check_tasks_root(tmp_path)
    (tmp_path / "ours" / MANIFEST_FILE).unlink()
    with pytest.raises(TaskError, match=r"missing file manifest.json"):
        check_tasks_root(tmp_path)
