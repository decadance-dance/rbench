"""Task format and loader (spec review-tasks: Task contents, Ground truth items, Recorded origin).

Every task here is synthetic and built in tmp_path.
"""

import json
from pathlib import Path

import pytest

from rbench.tasks import (
    GROUND_TRUTH_FILE,
    HIDDEN_CANARY_PREFIX,
    PUBLIC_CANARY_PREFIX,
    TASK_FILE,
    Producer,
    TaskError,
    load_tasks,
)

_canaries = iter(range(1, 1000))


def canary(prefix: str = PUBLIC_CANARY_PREFIX) -> str:
    return f"{prefix}{next(_canaries):032x}"


def item(**overrides) -> dict:
    data = {
        "id": "i1",
        "file": "src/app.py",
        "start_line": 3,
        "end_line": 5,
        "message": "Off-by-one in the loop bound.",
        "category": "correctness",
        "severity": "must",
        "producer": "human",
        "source": "https://example.test/pr/1#discussion_r1",
    }
    return {**data, **overrides}


def write_task(
    set_dir: Path, task_id: str = "t-1", task_set: str = "ours", items=None, gt=None, **meta
) -> Path:
    task_dir = set_dir / task_id
    task_dir.mkdir(parents=True)
    data = {
        "id": task_id,
        "set": task_set,
        "repo": "example/app",
        "pr": 1,
        "base": "a" * 40,
        "head": "b" * 40,
        "archive_sha256": "c" * 64,
        "language": "Python",
        "title": "Fix the loop",
        "description": "",
        **meta,
    }
    (task_dir / TASK_FILE).write_text(json.dumps(data))
    prefix = HIDDEN_CANARY_PREFIX if task_set == "hidden" else PUBLIC_CANARY_PREFIX
    if gt is None:
        gt = {"canary": canary(prefix), "items": [item()] if items is None else items}
    if gt is not False:
        (task_dir / GROUND_TRUTH_FILE).write_text(json.dumps(gt))
    return task_dir


def drop(path: Path, key: str) -> None:
    data = json.loads(path.read_text())
    del data[key]
    path.write_text(json.dumps(data))


def test_complete_task_loads(tmp_path: Path) -> None:
    write_task(tmp_path, "t-2")
    write_task(
        tmp_path,
        "t-1",
        task_set="reviewbench",
        items=[
            item(id="rb#0", producer="ccr:gpt-5.5[ReasoningEffort=medium]", source=None),
            item(id="rb#1", producer="unknown", severity="nit"),
        ],
    )
    (tmp_path / "manifest.json").write_text("{}")
    (tmp_path / ".cache").mkdir()

    tasks = load_tasks(tmp_path)

    assert [t.id for t in tasks] == ["t-1", "t-2"]
    rb = tasks[0]
    assert (rb.set, rb.pr, rb.description) == ("reviewbench", 1, "")
    assert rb.canary.startswith(PUBLIC_CANARY_PREFIX)
    assert [i.producer for i in rb.ground_truth] == [
        Producer("engine", "ccr", "gpt-5.5[ReasoningEffort=medium]"),
        Producer("unknown"),
    ]
    assert rb.ground_truth[0].source is None
    assert tasks[1].ground_truth[0].producer == Producer("human")


def test_task_without_ground_truth_names_task_and_file(tmp_path: Path) -> None:
    write_task(tmp_path, "t-1", gt=False)
    with pytest.raises(TaskError, match=rf"task .*t-1: missing file {GROUND_TRUTH_FILE}"):
        load_tasks(tmp_path)


def test_task_without_metadata_file_fails(tmp_path: Path) -> None:
    (tmp_path / "t-1").mkdir()
    with pytest.raises(TaskError, match=rf"t-1: missing file {TASK_FILE}"):
        load_tasks(tmp_path)


@pytest.mark.parametrize(
    "key",
    [
        "id",
        "set",
        "repo",
        "pr",
        "base",
        "head",
        "archive_sha256",
        "language",
        "title",
        "description",
    ],
)
def test_every_task_field_is_required(tmp_path: Path, key: str) -> None:
    task_dir = write_task(tmp_path)
    drop(task_dir / TASK_FILE, key)
    with pytest.raises(TaskError, match=rf"t-1: {TASK_FILE}: missing {key}"):
        load_tasks(tmp_path)


@pytest.mark.parametrize(
    "key,value",
    [
        ("set", "public"),
        ("repo", "app"),
        ("pr", 0),
        ("pr", True),
        ("pr", "1"),
        ("base", "abc"),
        ("head", "B" * 40),
        ("archive_sha256", "c" * 63),
        ("language", ""),
        ("title", " "),
        ("description", None),
        ("id", "other"),
    ],
)
def test_malformed_task_field_fails(tmp_path: Path, key: str, value: object) -> None:
    write_task(tmp_path, **{key: value})
    with pytest.raises(TaskError, match=rf"t-1: {TASK_FILE}: '{key}'"):
        load_tasks(tmp_path)


def test_unknown_task_key_fails(tmp_path: Path) -> None:
    write_task(tmp_path, reviewers=["a"])
    with pytest.raises(TaskError, match=r"t-1: task.json: unknown reviewers"):
        load_tasks(tmp_path)


@pytest.mark.parametrize("key", ["canary", "items"])
def test_ground_truth_fields_are_required(tmp_path: Path, key: str) -> None:
    task_dir = write_task(tmp_path)
    drop(task_dir / GROUND_TRUTH_FILE, key)
    with pytest.raises(TaskError, match=rf"t-1: {GROUND_TRUTH_FILE}: missing {key}"):
        load_tasks(tmp_path)


def test_unreadable_ground_truth_names_the_file(tmp_path: Path) -> None:
    task_dir = write_task(tmp_path)
    (task_dir / GROUND_TRUTH_FILE).write_text("{not json")
    with pytest.raises(TaskError, match=rf"t-1: cannot read {GROUND_TRUTH_FILE}"):
        load_tasks(tmp_path)


@pytest.mark.parametrize("severity", ["high", "Must", "minor", None])
def test_invalid_severity_names_task_and_item(tmp_path: Path, severity: object) -> None:
    write_task(tmp_path, items=[item(id="i1"), item(id="i7", severity=severity)])
    with pytest.raises(TaskError, match=r"t-1: ground_truth.json: item 'i7': 'severity'"):
        load_tasks(tmp_path)


@pytest.mark.parametrize("severity", ["must", "should", "nit"])
def test_valid_severities_load(tmp_path: Path, severity: str) -> None:
    write_task(tmp_path, items=[item(severity=severity)])
    assert load_tasks(tmp_path)[0].ground_truth[0].severity == severity


@pytest.mark.parametrize("producer", ["", "claude", ":model", "engine:", "Human", None, 3])
def test_invalid_producer_fails(tmp_path: Path, producer: object) -> None:
    write_task(tmp_path, items=[item(producer=producer)])
    with pytest.raises(TaskError, match=r"item 'i1': 'producer'"):
        load_tasks(tmp_path)


@pytest.mark.parametrize(
    "key", ["id", "file", "start_line", "end_line", "message", "category", "severity", "producer"]
)
def test_every_item_field_is_required(tmp_path: Path, key: str) -> None:
    raw = item()
    del raw[key]
    write_task(tmp_path, items=[raw])
    label = "item 0" if key == "id" else "item 'i1'"
    with pytest.raises(TaskError, match=rf"t-1: {GROUND_TRUTH_FILE}: {label}: missing {key}"):
        load_tasks(tmp_path)


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_line": 0},
        {"start_line": 6},
        {"end_line": "5"},
        {"start_line": True, "end_line": True},
        {"file": "/etc/passwd"},
        {"file": "../outside.py"},
        {"message": ""},
        {"category": None},
        {"id": 7},
        {"extra": 1},
    ],
)
def test_malformed_item_fails(tmp_path: Path, overrides: dict) -> None:
    write_task(tmp_path, items=[item(**overrides)])
    with pytest.raises(TaskError, match=r"t-1: ground_truth.json: item "):
        load_tasks(tmp_path)


def test_repeated_item_id_fails(tmp_path: Path) -> None:
    write_task(tmp_path, items=[item(), item()])
    with pytest.raises(TaskError, match=r"item id 'i1' is repeated"):
        load_tasks(tmp_path)


@pytest.mark.parametrize("task_set", ["ours", "hidden"])
@pytest.mark.parametrize("source", [None, "", "missing"])
def test_item_without_source_names_task_and_item(
    tmp_path: Path, task_set: str, source: str | None
) -> None:
    raw = item(id="i3", source=source)
    if source == "missing":
        del raw["source"]
    task_id = "h-1" if task_set == "hidden" else "t-1"
    write_task(tmp_path, task_id, task_set=task_set, items=[raw])
    with pytest.raises(TaskError, match=rf"{task_id}: ground_truth.json: item 'i3'"):
        load_tasks(tmp_path)


def test_reviewbench_item_may_omit_source(tmp_path: Path) -> None:
    raw = item()
    del raw["source"]
    write_task(tmp_path, task_set="reviewbench", items=[raw])
    assert load_tasks(tmp_path)[0].ground_truth[0].source is None


def test_canary_unique_across_sets(tmp_path: Path) -> None:
    shared = canary()
    write_task(tmp_path / "ours", "t-1", gt={"canary": shared, "items": []})
    write_task(tmp_path / "reviewbench", "t-2", "reviewbench", gt={"canary": shared, "items": []})
    with pytest.raises(TaskError, match=r"t-2 has the same canary as task .*t-1"):
        load_tasks(tmp_path / "ours", tmp_path / "reviewbench")


def test_task_id_unique_across_sets(tmp_path: Path) -> None:
    write_task(tmp_path / "ours", "t-1")
    write_task(tmp_path / "reviewbench", "t-1", "reviewbench")
    with pytest.raises(TaskError, match=r"has the same id as task"):
        load_tasks(tmp_path / "ours", tmp_path / "reviewbench")


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        PUBLIC_CANARY_PREFIX,
        PUBLIC_CANARY_PREFIX + "0" * 31,
        PUBLIC_CANARY_PREFIX + "G" * 32,
        "0" * 32,
        ["x"],
    ],
)
def test_malformed_canary_fails(tmp_path: Path, value: object) -> None:
    write_task(tmp_path, gt={"canary": value, "items": []})
    with pytest.raises(TaskError, match=r"t-1: ground_truth.json: 'canary'"):
        load_tasks(tmp_path)


def test_hidden_task_uses_hidden_canary_prefix(tmp_path: Path) -> None:
    write_task(tmp_path, "h-1", "hidden")
    task = load_tasks(tmp_path)[0]
    assert task.canary.startswith(HIDDEN_CANARY_PREFIX)
    assert not HIDDEN_CANARY_PREFIX.startswith(PUBLIC_CANARY_PREFIX)
    assert not PUBLIC_CANARY_PREFIX.startswith(HIDDEN_CANARY_PREFIX)


@pytest.mark.parametrize("task_set", ["ours", "reviewbench"])
def test_public_task_cannot_use_hidden_canary_prefix(tmp_path: Path, task_set: str) -> None:
    write_task(
        tmp_path, task_set=task_set, gt={"canary": canary(HIDDEN_CANARY_PREFIX), "items": []}
    )
    with pytest.raises(TaskError, match=r"'canary' must be 'rbench-canary-'"):
        load_tasks(tmp_path)


def test_hidden_task_cannot_use_public_canary_prefix(tmp_path: Path) -> None:
    write_task(tmp_path, "h-1", "hidden", gt={"canary": canary(), "items": []})
    with pytest.raises(TaskError, match=r"h-1: ground_truth.json: 'canary'"):
        load_tasks(tmp_path)


@pytest.mark.parametrize("task_set", ["ours", "reviewbench"])
def test_hidden_id_prefix_reserved_for_hidden_tasks(tmp_path: Path, task_set: str) -> None:
    write_task(tmp_path, "h-1", task_set)
    with pytest.raises(TaskError, match=r"h-1: task.json: 'id': the 'h-' id prefix is reserved"):
        load_tasks(tmp_path)


def test_hidden_task_needs_hidden_id_prefix(tmp_path: Path) -> None:
    write_task(tmp_path, "t-1", "hidden")
    with pytest.raises(TaskError, match=r"t-1: task.json: 'id': the 'h-' id prefix"):
        load_tasks(tmp_path)


def test_missing_set_directory_fails(tmp_path: Path) -> None:
    with pytest.raises(TaskError, match=r"does not exist"):
        load_tasks(tmp_path / "nope")
