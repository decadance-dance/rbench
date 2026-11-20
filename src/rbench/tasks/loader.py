"""The task directory format and the task-set loader (spec review-tasks).

A task set is a directory of task directories, one per task, named by the task id:

    <set-dir>/<task-id>/task.json          metadata, PR title and description
    <set-dir>/<task-id>/ground_truth.json  canary and ground-truth items

Ground truth and its canary live in their own file, so building a trial's inputs never has to
read it. Other files in a set directory (such as `manifest.json`) and dot entries are skipped.

`task.json`:
    {"id", "set", "repo", "pr", "base", "head", "archive_sha256", "language", "title",
     "description"}
`ground_truth.json`:
    {"canary", "items": [{"id", "file", "start_line", "end_line", "message", "category",
                          "severity", "producer", "source"?}]}

`producer` is `human`, `unknown` or `<engine>:<model>`. `source` (the review, comment or commit
the item was taken from) is required in the `ours` and `hidden` sets. A canary is a prefix
followed by 32 lowercase hex digits, e.g. `PUBLIC_CANARY_PREFIX + uuid.uuid4().hex`; hidden tasks
use the hidden canary prefix instead. That prefix is operator configuration kept with the hidden
task directory, never in this repository (spec review-tasks, Publication guard), so the caller
passes it in, and hidden tasks load only when it is given.
Every key is required and no other key is allowed; a malformed task fails the whole load.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

TASK_FILE = "task.json"
GROUND_TRUTH_FILE = "ground_truth.json"

TaskSet = Literal["reviewbench", "ours", "hidden"]
Severity = Literal["must", "should", "nit"]
TASK_SETS: tuple[TaskSet, ...] = ("reviewbench", "ours", "hidden")
SEVERITIES: tuple[Severity, ...] = ("must", "should", "nit")
SETS_WITH_SOURCE = ("ours", "hidden")

HIDDEN_ID_PREFIX = "h-"
PUBLIC_CANARY_PREFIX = "rbench-canary-"

TASK_ID = re.compile(r"[a-z0-9][a-z0-9_.-]*")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
CANARY_SUFFIX = re.compile(r"[0-9a-f]{32}")

TASK_KEYS = {
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
}
GROUND_TRUTH_KEYS = {"canary", "items"}
ITEM_KEYS = {"id", "file", "start_line", "end_line", "message", "category", "severity", "producer"}
ITEM_OPTIONAL_KEYS = {"source"}


class TaskError(ValueError):
    """A task set that cannot be loaded; the message names the task, the file and the item."""


@dataclass(frozen=True)
class Producer:
    """Who first produced a ground-truth item: an engine and model, a human, or unknown."""

    kind: Literal["engine", "human", "unknown"]
    engine: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class GroundTruthItem:
    id: str
    file: str
    start_line: int
    end_line: int
    message: str
    category: str
    severity: Severity
    producer: Producer
    source: str | None


@dataclass(frozen=True)
class Task:
    id: str
    set: TaskSet
    repo: str
    pr: int
    base: str
    head: str
    archive_sha256: str
    language: str
    title: str
    description: str
    canary: str
    ground_truth: tuple[GroundTruthItem, ...]
    path: Path


def load_tasks(*set_dirs: Path, hidden_canary_prefix: str | None = None) -> list[Task]:
    """Load every task under `set_dirs`, sorted by id; raise `TaskError` on the first bad one.

    Task ids and canaries must be unique across everything loaded in one call. Hidden tasks need
    `hidden_canary_prefix`, from the hidden directory's configuration.
    """
    check_hidden_canary_prefix(hidden_canary_prefix)
    tasks: list[Task] = []
    for set_dir in set_dirs:
        set_dir = Path(set_dir)
        if not set_dir.is_dir():
            raise TaskError(f"task set directory {set_dir} does not exist")
        for task_dir in sorted(set_dir.iterdir()):
            if task_dir.is_dir() and not task_dir.name.startswith("."):
                tasks.append(load_task(task_dir, hidden_canary_prefix))
    for attr in ("id", "canary"):
        seen: dict[str, Task] = {}
        for task in tasks:
            other = seen.setdefault(getattr(task, attr), task)
            if other is not task:
                raise TaskError(f"task {task.path} has the same {attr} as task {other.path}")
    return sorted(tasks, key=lambda t: t.id)


def check_hidden_canary_prefix(prefix: str | None) -> None:
    """Reject a hidden canary prefix that could be confused with the public one.

    With neither prefix starting the other, no canary can carry both, so a public task never
    uses the hidden prefix. The prefix itself stays out of the message.
    """
    if prefix is None:
        return
    if not isinstance(prefix, str) or not prefix.strip():
        raise TaskError("the hidden canary prefix must be a non-empty string")
    if prefix.startswith(PUBLIC_CANARY_PREFIX) or PUBLIC_CANARY_PREFIX.startswith(prefix):
        raise TaskError(
            f"the hidden canary prefix must differ from the public prefix {PUBLIC_CANARY_PREFIX!r},"
            " and neither may start the other"
        )


def load_task(task_dir: Path, hidden_canary_prefix: str | None = None) -> Task:
    """Load and validate one task directory; a hidden task needs `hidden_canary_prefix`."""
    check_hidden_canary_prefix(hidden_canary_prefix)
    meta_path = task_dir / TASK_FILE
    meta = _read_object(task_dir, meta_path)
    _check_keys(meta, TASK_KEYS, set(), f"task {task_dir}: {meta_path.name}")

    def where(key: str) -> str:
        return f"task {task_dir}: {meta_path.name}: {key!r}"

    task_id = _string(meta, "id", where, TASK_ID)
    if task_id != task_dir.name:
        raise TaskError(f"{where('id')} is {task_id!r}, but the directory is {task_dir.name!r}")
    task_set = meta["set"]
    if task_set not in TASK_SETS:
        raise TaskError(f"{where('set')} must be one of {', '.join(TASK_SETS)}, not {task_set!r}")
    if (task_set == "hidden") != task_id.startswith(HIDDEN_ID_PREFIX):
        raise TaskError(
            f"{where('id')}: the {HIDDEN_ID_PREFIX!r} id prefix is reserved for, and required in,"
            f" the hidden set; this task is in {task_set!r}"
        )
    pr = meta["pr"]
    if type(pr) is not int or pr < 1:
        raise TaskError(f"{where('pr')} must be a positive integer, not {pr!r}")

    gt_path = task_dir / GROUND_TRUTH_FILE
    gt = _read_object(task_dir, gt_path)
    _check_keys(gt, GROUND_TRUTH_KEYS, set(), f"task {task_dir}: {gt_path.name}")

    def gt_where(key: str) -> str:
        return f"task {task_dir}: {gt_path.name}: {key!r}"

    canary = _string(gt, "canary", gt_where)
    if task_set == "hidden":
        if hidden_canary_prefix is None:
            raise TaskError(
                f"task {task_dir} is hidden, and no hidden canary prefix is configured;"
                " it comes from the hidden directory's configuration"
            )
        prefix, prefix_name = hidden_canary_prefix, "the hidden canary prefix"
    else:
        prefix, prefix_name = PUBLIC_CANARY_PREFIX, repr(PUBLIC_CANARY_PREFIX)
    if not (canary.startswith(prefix) and CANARY_SUFFIX.fullmatch(canary.removeprefix(prefix))):
        raise TaskError(
            f"{gt_where('canary')} must be {prefix_name} and 32 lowercase hex digits"
            f" for a task in {task_set!r}"
        )
    items = gt["items"]
    if not isinstance(items, list):
        raise TaskError(f"{gt_where('items')} must be a list")
    loaded = tuple(_item(task_dir, gt_path, i, raw, task_set) for i, raw in enumerate(items))
    ids = [item.id for item in loaded]
    for item_id in ids:
        if ids.count(item_id) > 1:
            raise TaskError(f"task {task_dir}: {gt_path.name}: item id {item_id!r} is repeated")

    return Task(
        id=task_id,
        set=task_set,
        repo=_string(meta, "repo", where, REPO),
        pr=pr,
        base=_string(meta, "base", where, COMMIT),
        head=_string(meta, "head", where, COMMIT),
        archive_sha256=_string(meta, "archive_sha256", where, SHA256),
        language=_string(meta, "language", where),
        title=_string(meta, "title", where),
        description=_string(meta, "description", where, allow_empty=True),
        canary=canary,
        ground_truth=loaded,
        path=task_dir,
    )


def _item(task_dir: Path, path: Path, index: int, raw: object, task_set: str) -> GroundTruthItem:
    label = f"task {task_dir}: {path.name}: item {index}"
    if not isinstance(raw, dict):
        raise TaskError(f"{label} must be an object")
    if isinstance(raw.get("id"), str) and raw["id"]:
        label = f"task {task_dir}: {path.name}: item {raw['id']!r}"
    _check_keys(raw, ITEM_KEYS, ITEM_OPTIONAL_KEYS, label)

    def where(key: str) -> str:
        return f"{label}: {key!r}"

    file = _string(raw, "file", where)
    file_path = PurePosixPath(file)
    if file_path.is_absolute() or ".." in file_path.parts:
        raise TaskError(f"{where('file')} must be a path inside the repository, not {file!r}")
    start, end = raw["start_line"], raw["end_line"]
    if not (type(start) is int and type(end) is int and 1 <= start <= end):
        raise TaskError(
            f"{label}: line range must be integers with 1 <= start_line <= end_line,"
            f" not {start!r}-{end!r}"
        )
    severity = raw["severity"]
    if severity not in SEVERITIES:
        raise TaskError(
            f"{where('severity')} must be one of {', '.join(SEVERITIES)}, not {severity!r}"
        )
    source = raw.get("source")
    if source is None:
        if task_set in SETS_WITH_SOURCE:
            raise TaskError(f"{label} has no recorded source, required in the {task_set!r} set")
    else:
        source = _string(raw, "source", where)
    return GroundTruthItem(
        id=_string(raw, "id", where),
        file=file,
        start_line=start,
        end_line=end,
        message=_string(raw, "message", where),
        category=_string(raw, "category", where),
        severity=severity,
        producer=_producer(raw["producer"], where("producer")),
        source=source,
    )


def _producer(value: object, where: str) -> Producer:
    if value in ("human", "unknown"):
        return Producer(value)
    engine, sep, model = value.partition(":") if isinstance(value, str) else ("", "", "")
    if not (sep and engine.strip() and model.strip()):
        raise TaskError(f"{where} must be 'human', 'unknown' or '<engine>:<model>', not {value!r}")
    return Producer("engine", engine, model)


def _read_object(task_dir: Path, path: Path) -> dict:
    if not path.is_file():
        raise TaskError(f"task {task_dir}: missing file {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise TaskError(f"task {task_dir}: cannot read {path.name}: {e}") from e
    if not isinstance(data, dict):
        raise TaskError(f"task {task_dir}: {path.name} must hold a JSON object")
    return data


def _check_keys(data: dict, required: set[str], optional: set[str], label: str) -> None:
    if missing := sorted(required - data.keys()):
        raise TaskError(f"{label}: missing {', '.join(missing)}")
    if unknown := sorted(data.keys() - required - optional):
        raise TaskError(f"{label}: unknown {', '.join(unknown)}")


def _string(data: dict, key: str, where, pattern=None, allow_empty: bool = False) -> str:
    value = data[key]
    if not isinstance(value, str) or not (value.strip() or allow_empty):
        raise TaskError(f"{where(key)} must be a non-empty string, not {value!r}")
    if pattern is not None and not pattern.fullmatch(value):
        raise TaskError(f"{where(key)} has an invalid value {value!r}")
    return value
