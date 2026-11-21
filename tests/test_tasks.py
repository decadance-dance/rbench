"""Task format and loader (spec review-tasks: Task contents, Ground truth items, Recorded origin).

Every task here is synthetic and built in tmp_path.
"""

import itertools
import json
import re
import secrets
from pathlib import Path

import pytest

from rbench.tasks import (
    GROUND_TRUTH_FILE,
    HIDDEN_ID_PREFIX,
    PUBLIC_CANARY_PREFIX,
    TASK_FILE,
    Producer,
    TaskError,
    load_task,
    load_tasks,
)
from rbench.tasks.loader import HIDDEN_DIR_ENV, _load_tasks

_canaries = itertools.count(1)  # shared by every test module that builds tasks
# The real hidden canary prefix is operator configuration and never appears in this repository.
HIDDEN = f"{secrets.token_hex(8)}-"
# Built at runtime: the publication guard rejects tracked files holding hidden-prefixed task ids.
HID = f"{HIDDEN_ID_PREFIX}1"
ID_PREFIX = re.escape(repr(HIDDEN_ID_PREFIX))
# The neutral label errors give a hidden task instead of its path or id.
ENTRY0 = f"{HIDDEN_DIR_ENV} entry 0"


def load_hidden(set_dir: Path, prefix: str = HIDDEN):
    """The loader's hidden mode, which `rbench.tasks.hidden` enters after validating the directory."""
    return _load_tasks([set_dir], prefix)


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
    prefix = HIDDEN if task_set == "hidden" else PUBLIC_CANARY_PREFIX
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
    hidden = task_set == "hidden"
    write_task(tmp_path, HID if hidden else "t-1", task_set=task_set, items=[raw])
    # A hidden item is named by its position, not its id.
    where = (
        f"{ENTRY0}: ground_truth.json: item 0" if hidden else "t-1: ground_truth.json: item 'i3'"
    )
    with pytest.raises(TaskError, match=re.escape(where)):
        load_hidden(tmp_path) if hidden else load_tasks(tmp_path)


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


def test_set_directory_listed_twice_fails(tmp_path: Path) -> None:
    write_task(tmp_path, "t-1")
    with pytest.raises(TaskError, match=r"t-1 has the same id as task .*t-1"):
        load_tasks(tmp_path, tmp_path)


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
    write_task(tmp_path, HID, "hidden")
    task = load_hidden(tmp_path)[0]
    assert task.canary.startswith(HIDDEN)


REFUSED = f"is a hidden task; hidden tasks load only from the directory {HIDDEN_DIR_ENV} names"


def assert_names_neither(error: pytest.ExceptionInfo, *secrets: str) -> None:
    for exc in (error.value, error.value.__cause__, error.value.__context__):
        assert not any(secret in str(exc) for secret in secrets)


def test_hidden_task_fails_the_public_loaders(tmp_path: Path) -> None:
    write_task(tmp_path, HID, "hidden")
    with pytest.raises(TaskError, match=f"argument 0, entry 0, {REFUSED}") as error:
        load_tasks(tmp_path)
    assert_names_neither(error, HID, str(tmp_path))
    with pytest.raises(TaskError, match=f"the task directory given {REFUSED}") as error:
        load_task(tmp_path / HID)
    assert_names_neither(error, HID, str(tmp_path))


@pytest.mark.parametrize(
    "meta",
    [{"id": HID}, {"id": HID, "pr": 0, "set": "unknown"}, {"id": HID, "set": "ours"}, {"id": "x"}],
)
def test_renamed_hidden_task_is_refused_before_any_value_is_quoted(tmp_path: Path, meta) -> None:
    # A hidden task's directory renamed away from its id, or a hidden set under a plain id.
    task_dir = write_task(tmp_path, "x", "hidden", **meta)
    with pytest.raises(TaskError, match=REFUSED) as error:
        load_tasks(tmp_path)
    assert_names_neither(error, HID, str(tmp_path))
    with pytest.raises(TaskError, match=REFUSED) as error:
        load_task(task_dir)
    assert_names_neither(error, HID, str(tmp_path))


def test_task_repr_hides_the_canary_and_a_hidden_task(tmp_path: Path) -> None:
    write_task(tmp_path / "ours")
    write_task(tmp_path / "hidden", HID, "hidden")
    public, hidden = load_tasks(tmp_path / "ours")[0], load_hidden(tmp_path / "hidden")[0]
    assert repr(public).startswith("Task(id='t-1'") and public.canary not in repr(public)
    assert repr(hidden) == "Task(set='hidden')"


@pytest.mark.parametrize("task_set", ["ours", "reviewbench"])
def test_public_task_cannot_use_hidden_canary_prefix(tmp_path: Path, task_set: str) -> None:
    write_task(tmp_path, task_set=task_set, gt={"canary": canary(HIDDEN), "items": []})
    with pytest.raises(TaskError, match=r"'canary' must be 'rbench-canary-'"):
        load_tasks(tmp_path)


def test_hidden_task_cannot_use_public_canary_prefix(tmp_path: Path) -> None:
    write_task(tmp_path, HID, "hidden", gt={"canary": canary(), "items": []})
    with pytest.raises(
        TaskError, match=rf"{ENTRY0}: ground_truth.json: 'canary' must be the hidden"
    ):
        load_hidden(tmp_path)


@pytest.mark.parametrize("wrap", [lambda c: c + "x", lambda c: [c], lambda c: {"value": c}])
def test_canary_errors_do_not_echo_the_canary(tmp_path: Path, wrap) -> None:
    write_task(tmp_path, HID, "hidden", gt={"canary": wrap(canary(HIDDEN)), "items": []})
    with pytest.raises(TaskError, match=r"'canary' must be the hidden canary prefix") as error:
        load_hidden(tmp_path)
    assert HIDDEN not in str(error.value)


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        " ",
        PUBLIC_CANARY_PREFIX,
        PUBLIC_CANARY_PREFIX + "x-",
        PUBLIC_CANARY_PREFIX[:-1],
        PUBLIC_CANARY_PREFIX[:1],
    ],
)
def test_hidden_prefix_must_be_distinct_from_the_public_one(tmp_path: Path, prefix: str) -> None:
    write_task(tmp_path)
    with pytest.raises(TaskError, match=r"the hidden canary prefix must"):
        load_hidden(tmp_path, prefix)


@pytest.mark.parametrize("task_set", ["ours", "reviewbench"])
def test_hidden_id_prefix_reserved_for_hidden_tasks(tmp_path: Path, task_set: str) -> None:
    write_task(tmp_path, HID, task_set)
    with pytest.raises(TaskError, match=REFUSED):
        load_tasks(tmp_path)
    with pytest.raises(
        TaskError, match=rf"{ENTRY0}: task.json: 'id': the {ID_PREFIX} id prefix is reserved"
    ):
        load_hidden(tmp_path)


def test_hidden_task_needs_hidden_id_prefix(tmp_path: Path) -> None:
    write_task(tmp_path, "t-1", "hidden")
    with pytest.raises(TaskError, match=REFUSED):
        load_tasks(tmp_path)
    with pytest.raises(TaskError, match=rf"{ENTRY0}: task.json: 'id': the {ID_PREFIX} id prefix"):
        load_hidden(tmp_path)


def test_hidden_task_id_is_more_than_the_prefix(tmp_path: Path) -> None:
    write_task(tmp_path, HIDDEN_ID_PREFIX, "hidden")
    with pytest.raises(
        TaskError, match=rf"{ENTRY0}: task.json: 'id': a hidden task id needs characters after"
    ):
        load_hidden(tmp_path)


def test_missing_set_directory_fails(tmp_path: Path) -> None:
    with pytest.raises(TaskError, match=r"does not exist"):
        load_tasks(tmp_path / "nope")


@pytest.mark.parametrize(
    "file,text,match",
    [
        (TASK_FILE, '{"id": "t-1", "id": "t-1"}', r"task.json: top level: 'id' is repeated"),
        (
            GROUND_TRUTH_FILE,
            '{"canary": "a", "canary": "b", "items": []}',
            r"ground_truth.json: top level: 'canary' is repeated",
        ),
        (
            GROUND_TRUTH_FILE,
            '{"canary": "a", "items": [{"id": "i9", "severity": "high", "severity": "must"}]}',
            r"ground_truth.json: item 0: 'severity' is repeated",
        ),
        (
            GROUND_TRUTH_FILE,
            '{"canary": "a", "items": [{"id": "i1"}, {"id": "", "severity": "a", "severity": "b"}]}',
            r"ground_truth.json: item 1: 'severity' is repeated",
        ),
        (
            GROUND_TRUTH_FILE,
            '{"canary": "a", "items": [{"producer": {"x": 1, "x": 2}}]}',
            r"ground_truth.json: item 0: 'producer': a key is repeated",
        ),
    ],
)
def test_repeated_json_key_fails(tmp_path: Path, file: str, text: str, match: str) -> None:
    task_dir = write_task(tmp_path)
    (task_dir / file).write_text(text)
    with pytest.raises(TaskError, match=rf"t-1: {match}"):
        load_tasks(tmp_path)


@pytest.mark.parametrize(
    "content",
    [
        b'{"canary": ' + b"1" * 5000 + b', "items": []}',
        b'{"canary": "\xff"}',
        b"[" * 100_000,
    ],
)
def test_undecodable_ground_truth_names_task_and_file(tmp_path: Path, content: bytes) -> None:
    task_dir = write_task(tmp_path)
    (task_dir / GROUND_TRUTH_FILE).write_bytes(content)
    with pytest.raises(TaskError, match=rf"t-1: cannot read {GROUND_TRUTH_FILE}"):
        load_tasks(tmp_path)


@pytest.mark.parametrize(
    "shape",
    [
        '{{"id": {m}, "value": 1, "value": 2}}',
        "{{{m}: 1, {m}: 2}}",
        '{{"x": {{{m}: {{"a": 1, "a": 2}}}}}}',
        '[{m}, {{"a": 1, "a": 2}}]',
    ],
)
def test_repeated_key_errors_do_not_echo_contents(tmp_path: Path, shape: str) -> None:
    marker = json.dumps(canary(HIDDEN))
    text = f'{{"canary": {shape.format(m=marker)}, "items": []}}'
    write_task(tmp_path, HID, "hidden", gt=False)
    (tmp_path / HID / GROUND_TRUTH_FILE).write_text(text)
    with pytest.raises(TaskError, match=r"ground_truth.json: 'canary'.* is repeated") as error:
        load_hidden(tmp_path)
    for exc in (error.value, error.value.__cause__, error.value.__context__):
        assert HIDDEN not in str(exc)
