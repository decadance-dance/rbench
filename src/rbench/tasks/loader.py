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
task directory, never in this repository (spec review-tasks, Publication guard). `load_tasks` and
`load_task` refuse hidden tasks: those load only through `rbench.tasks.hidden`, from the directory
`RBENCH_HIDDEN_DIR` names, and their errors quote no path, id or value.
Every key is required and no other key is allowed; a malformed task fails the whole load.
"""

import json
import os
import re
from contextvars import ContextVar
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
HIDDEN_DIR_ENV = "RBENCH_HIDDEN_DIR"
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

    def __repr__(self) -> str:
        # Never the canary; for a hidden task, nothing that identifies it.
        if self.set == "hidden":
            return "Task(set='hidden')"
        return f"Task(id={self.id!r}, set={self.set!r}, repo={self.repo!r}, pr={self.pr})"


# While a hidden task loads: its neutral label, and the signal to quote no values in errors.
_hidden_label: ContextVar[str | None] = ContextVar("hidden_label", default=None)


def _show(value: object) -> str:
    return "<hidden>" if _hidden_label.get() else repr(value)


def _refuse_hidden(set_dir: Path, entry: str) -> TaskError:
    return TaskError(
        f"task set directory {set_dir}: {entry} has a hidden id; hidden tasks load only from the"
        f" directory {HIDDEN_DIR_ENV} names"
    )


def is_within(path: Path, root: Path) -> bool:
    """Whether `path`, with every link followed, is `root` or inside it; aliases of `root` count."""
    resolved = Path(path).resolve()
    return any(p.exists() and os.path.samefile(p, root) for p in (resolved, *resolved.parents))


def load_tasks(*set_dirs: Path) -> list[Task]:
    """Load every public task under `set_dirs`, sorted by id; raise `TaskError` on the first bad one.

    Task ids and canaries must be unique across everything loaded in one call. A hidden task fails
    the load: hidden tasks load only through `rbench.tasks.hidden`.
    """
    return _load_tasks(set_dirs)


def _load_tasks(set_dirs, hidden_canary_prefix: str | None = None) -> list[Task]:
    """Load tasks; with `hidden_canary_prefix`, from hidden set directories only.

    Only `rbench.tasks.hidden` passes the prefix, once it has validated the hidden directory. Then
    every task must be hidden, and every task directory and file must resolve inside its set
    directory before it is read.
    """
    check_hidden_canary_prefix(hidden_canary_prefix)
    hidden = hidden_canary_prefix is not None
    loaded: list[tuple[str, Task]] = []
    for set_dir in map(Path, set_dirs):
        if not set_dir.is_dir():
            name = (
                f"the directory {HIDDEN_DIR_ENV} names"
                if hidden
                else f"task set directory {set_dir}"
            )
            raise TaskError(f"{name} does not exist")
        entries = [
            d for d in sorted(set_dir.iterdir()) if d.is_dir() and not d.name.startswith(".")
        ]
        for i, task_dir in enumerate(entries):
            if not hidden:
                if task_dir.name.startswith(HIDDEN_ID_PREFIX):
                    raise _refuse_hidden(set_dir, f"entry {i}")
                loaded.append((f"task {task_dir}", _load_task(task_dir, None)))
                continue
            label = f"{HIDDEN_DIR_ENV} entry {i}"
            # limit: checked, then opened; a link swapped in between is not caught. The directory
            # is the operator's own, so this guards against a mistake, not an attacker.
            for path, name in (
                (task_dir, "its directory"),
                (task_dir / TASK_FILE, TASK_FILE),
                (task_dir / GROUND_TRUTH_FILE, GROUND_TRUTH_FILE),
            ):
                if not is_within(path, set_dir):
                    raise TaskError(f"{label}: {name} resolves outside the hidden directory")
            token = _hidden_label.set(label)
            try:
                task = _load_task(task_dir, hidden_canary_prefix)
            finally:
                _hidden_label.reset(token)
            if task.set != "hidden":
                raise TaskError(
                    f"{label} is in {task.set!r}; the hidden directory holds only hidden tasks"
                )
            loaded.append((label, task))
    for attr in ("id", "canary"):
        seen: dict[str, str] = {}
        for label, task in loaded:
            other = seen.setdefault(getattr(task, attr), label)
            if other != label:
                raise TaskError(f"{label} has the same {attr} as {other}")
    return sorted((task for _, task in loaded), key=lambda t: t.id)


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


def load_task(task_dir: Path) -> Task:
    """Load and validate one public task directory; a hidden one fails."""
    task_dir = Path(task_dir)
    if task_dir.name.startswith(HIDDEN_ID_PREFIX):
        raise _refuse_hidden(task_dir.parent, "an entry")
    return _load_task(task_dir, None)


def _load_task(task_dir: Path, hidden_canary_prefix: str | None) -> Task:
    label = _hidden_label.get() or f"task {task_dir}"
    meta_path = task_dir / TASK_FILE
    meta = _read_object(label, meta_path)
    _check_keys(meta, TASK_KEYS, set(), f"{label}: {meta_path.name}")

    def where(key: str) -> str:
        return f"{label}: {meta_path.name}: {key!r}"

    task_id = _string(meta, "id", where, TASK_ID)
    if task_id != task_dir.name:
        raise TaskError(
            f"{where('id')} is {_show(task_id)}, but the directory is {_show(task_dir.name)}"
        )
    task_set = meta["set"]
    if task_set not in TASK_SETS:
        raise TaskError(
            f"{where('set')} must be one of {', '.join(TASK_SETS)}, not {_show(task_set)}"
        )
    if (task_set == "hidden") != task_id.startswith(HIDDEN_ID_PREFIX):
        raise TaskError(
            f"{where('id')}: the {HIDDEN_ID_PREFIX!r} id prefix is reserved for, and required in,"
            f" the hidden set; this task is in {task_set!r}"
        )
    pr = meta["pr"]
    if type(pr) is not int or pr < 1:
        raise TaskError(f"{where('pr')} must be a positive integer, not {_show(pr)}")

    gt_path = task_dir / GROUND_TRUTH_FILE
    gt = _read_object(label, gt_path)
    _check_keys(gt, GROUND_TRUTH_KEYS, set(), f"{label}: {gt_path.name}")

    def gt_where(key: str) -> str:
        return f"{label}: {gt_path.name}: {key!r}"

    canary = gt["canary"]
    if task_set == "hidden":
        if hidden_canary_prefix is None:
            raise _refuse_hidden(task_dir.parent, "an entry")
        prefix, prefix_name = hidden_canary_prefix, "the hidden canary prefix"
    else:
        prefix, prefix_name = PUBLIC_CANARY_PREFIX, repr(PUBLIC_CANARY_PREFIX)
    # The message never quotes the value: a hidden task's canary must not reach logs.
    if not (
        isinstance(canary, str)
        and canary.startswith(prefix)
        and CANARY_SUFFIX.fullmatch(canary.removeprefix(prefix))
    ):
        raise TaskError(
            f"{gt_where('canary')} must be {prefix_name} and 32 lowercase hex digits"
            f" for a task in {task_set!r}"
        )
    items = gt["items"]
    if not isinstance(items, list):
        raise TaskError(f"{gt_where('items')} must be a list")
    loaded = tuple(_item(label, gt_path, i, raw, task_set) for i, raw in enumerate(items))
    ids = [item.id for item in loaded]
    for item_id in ids:
        if ids.count(item_id) > 1:
            raise TaskError(f"{label}: {gt_path.name}: item id {_show(item_id)} is repeated")

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


def _item(task_label: str, path: Path, index: int, raw: object, task_set: str) -> GroundTruthItem:
    label = f"{task_label}: {path.name}: item {index}"
    if not isinstance(raw, dict):
        raise TaskError(f"{label} must be an object")
    if isinstance(raw.get("id"), str) and raw["id"] and not _hidden_label.get():
        label = f"{task_label}: {path.name}: item {raw['id']!r}"
    _check_keys(raw, ITEM_KEYS, ITEM_OPTIONAL_KEYS, label)

    def where(key: str) -> str:
        return f"{label}: {key!r}"

    file = _string(raw, "file", where)
    file_path = PurePosixPath(file)
    if file_path.is_absolute() or ".." in file_path.parts:
        raise TaskError(f"{where('file')} must be a path inside the repository, not {_show(file)}")
    start, end = raw["start_line"], raw["end_line"]
    if not (type(start) is int and type(end) is int and 1 <= start <= end):
        raise TaskError(
            f"{label}: line range must be integers with 1 <= start_line <= end_line,"
            f" not {_show(start)}-{_show(end)}"
        )
    severity = raw["severity"]
    if severity not in SEVERITIES:
        raise TaskError(
            f"{where('severity')} must be one of {', '.join(SEVERITIES)}, not {_show(severity)}"
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
        raise TaskError(
            f"{where} must be 'human', 'unknown' or '<engine>:<model>', not {_show(value)}"
        )
    return Producer("engine", engine, model)


def _read_object(label: str, path: Path) -> dict:
    if not path.is_file():
        raise TaskError(f"{label}: missing file {path.name}")
    error = repeated = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_JsonObject.build)
        repeated = _repeated_key(data)
    # ValueError covers malformed JSON, bad UTF-8 and Python's integer-digit limit: their messages
    # give a position, never content. An OSError's message holds the path, so only its reason.
    except OSError as e:
        error = f"cannot read {path.name}: {e.strerror}"
    except (ValueError, RecursionError) as e:
        error = f"cannot read {path.name}: {e}"
    # Raised outside the handler, so no exception with file contents is chained to it.
    if error:
        raise TaskError(f"{label}: {error}")
    if repeated:
        raise TaskError(f"{label}: {path.name}: {repeated}")
    if not isinstance(data, dict):
        raise TaskError(f"{label}: {path.name} must hold a JSON object")
    return data


class _JsonObject(dict):
    """A JSON object that remembers its first repeated key, which `json` would silently overwrite."""

    repeated: str | None = None

    @classmethod
    def build(cls, pairs: list[tuple[str, object]]) -> "_JsonObject":
        data = cls()
        for key, value in pairs:
            if key in data and data.repeated is None:
                data.repeated = key
            data[key] = value
        return data


KNOWN_KEYS = TASK_KEYS | GROUND_TRUTH_KEYS | ITEM_KEYS | ITEM_OPTIONAL_KEYS


def _repeated_key(value: object, where: str = "") -> str | None:
    """Describe the first repeated key, in document order, by its position only.

    Only field names this format defines are quoted: any other key, and every value, may hold a
    canary, which must not reach a message.
    """
    if isinstance(value, _JsonObject):
        if value.repeated is not None:
            key = repr(value.repeated) if value.repeated in KNOWN_KEYS else "a key"
            return f"{where or 'top level'}: {key} is repeated"
        for key, child in value.items():
            name = repr(key) if key in KNOWN_KEYS else "an unknown key"
            if found := _repeated_key(child, f"{where}: {name}" if where else name):
                return found
    elif isinstance(value, list):
        for i, child in enumerate(value):
            name = f"item {i}" if where == "'items'" else f"{where}[{i}]"
            if found := _repeated_key(child, name):
                return found
    return None


def _check_keys(data: dict, required: set[str], optional: set[str], label: str) -> None:
    if missing := sorted(required - data.keys()):
        raise TaskError(f"{label}: missing {', '.join(missing)}")
    if unknown := sorted(data.keys() - required - optional):
        names = f"{len(unknown)} keys" if _hidden_label.get() else ", ".join(unknown)
        raise TaskError(f"{label}: unknown {names}")


def _string(data: dict, key: str, where, pattern=None, allow_empty: bool = False) -> str:
    value = data[key]
    if not isinstance(value, str) or not (value.strip() or allow_empty):
        raise TaskError(f"{where(key)} must be a non-empty string, not {_show(value)}")
    if pattern is not None and not pattern.fullmatch(value):
        raise TaskError(f"{where(key)} has an invalid value {_show(value)}")
    return value
