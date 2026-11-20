"""Task-set manifests, content hashes and the core task list (spec review-tasks, design D11).

Every task set has `<set-dir>/manifest.json`:

    {"set": "ours", "version": 2,
     "tasks": {"<task-id>": {"sha256": "<content hash>", "license": "MIT"}},
     "changelog": [{"version": 1, "note": "...", "changed": {"<task-id>": "<hash>", ...}},
                   {"version": 2, "note": "...", "changed": {"<task-id>": "<hash>" | null}}]}

`license` is optional: the SPDX id of the source repository's license, recorded by the import
(spec review-tasks, Source license). Changelog entries number the versions 1 to `version`; each
names the tasks it changed with their new hash, or null for a task it removed. A task's hash in
`tasks` must equal the one in the latest changelog entry naming it, so a new hash cannot be
recorded without a changelog entry.

A task's content hash is the SHA-256 of the canonical JSON of its `task.json` (metadata, PR title
and description, archive hash) and `ground_truth.json`: formatting and key order do not change it,
any value does, and it depends on no other task.

The core task list is `<tasks-root>/core.json`: `{"version": 1, "tasks": ["<task-id>", ...]}`.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from rbench.tasks.loader import (
    GROUND_TRUTH_FILE,
    HIDDEN_ID_PREFIX,
    SHA256,
    TASK_FILE,
    TASK_ID,
    TASK_SETS,
    Task,
    TaskError,
    load_tasks,
)

MANIFEST_FILE = "manifest.json"
CORE_LIST_FILE = "core.json"
CORE_SETS = ("reviewbench", "ours")

MANIFEST_KEYS = {"set", "version", "tasks", "changelog"}
MANIFEST_TASK_KEYS = {"sha256"}
MANIFEST_TASK_OPTIONAL_KEYS = {"license"}
CHANGELOG_KEYS = {"version", "note", "changed"}
CORE_LIST_KEYS = {"version", "tasks"}


@dataclass(frozen=True)
class ManifestTask:
    sha256: str
    license: str | None


@dataclass(frozen=True)
class ChangelogEntry:
    version: int
    note: str
    changed: dict[str, str | None]


@dataclass(frozen=True)
class Manifest:
    set: str
    version: int
    tasks: dict[str, ManifestTask]
    changelog: tuple[ChangelogEntry, ...]
    path: Path


@dataclass(frozen=True)
class CoreList:
    version: int
    task_ids: tuple[str, ...]
    path: Path


def task_content_sha256(task: Task) -> str:
    """Hash a loaded task's `task.json` and `ground_truth.json` as canonical JSON."""
    content = {
        "task": _read_json(task.path / TASK_FILE, f"task {task.path}"),
        "ground_truth": _read_json(task.path / GROUND_TRUTH_FILE, f"task {task.path}"),
    }
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_task_set(
    set_dir: Path, hidden_canary_prefix: str | None = None
) -> tuple[Manifest, list[Task]]:
    """Load a task set and check every task against its manifest; raise `TaskError` naming the task.

    This is the repository check: an edited task whose manifest was not given a new version fails.
    """
    set_dir = Path(set_dir)
    tasks = load_tasks(set_dir, hidden_canary_prefix=hidden_canary_prefix)
    manifest = load_manifest(set_dir / MANIFEST_FILE)
    on_disk = {task.id: task for task in tasks}
    if unlisted := sorted(on_disk.keys() - manifest.tasks.keys()):
        raise TaskError(f"{manifest.path} does not list tasks: {', '.join(unlisted)}")
    if gone := sorted(manifest.tasks.keys() - on_disk.keys()):
        raise TaskError(f"{manifest.path} lists tasks with no directory: {', '.join(gone)}")
    for task in tasks:
        if task.set != manifest.set:
            raise TaskError(
                f"task {task.path} is in set {task.set!r}, but {manifest.path} is for"
                f" {manifest.set!r}"
            )
        actual = task_content_sha256(task)
        if actual != manifest.tasks[task.id].sha256:
            raise TaskError(
                f"task {task.path}: content hash {actual} does not match {manifest.path};"
                " a changed task needs a new set version whose changelog entry names it"
            )
    return manifest, tasks


def load_manifest(path: Path) -> Manifest:
    """Read and validate one manifest file; this does not look at the tasks."""
    path = Path(path)
    label = f"manifest {path}"
    data = _read_json(path, label)
    _check_keys(data, MANIFEST_KEYS, set(), label)
    task_set = data["set"]
    if task_set not in TASK_SETS:
        raise TaskError(f"{label}: 'set' must be one of {', '.join(TASK_SETS)}, not {task_set!r}")
    version = _version(data["version"], f"{label}: 'version'")

    raw_tasks = data["tasks"]
    if not isinstance(raw_tasks, dict):
        raise TaskError(f"{label}: 'tasks' must be an object of task ids")
    tasks: dict[str, ManifestTask] = {}
    for task_id, entry in sorted(raw_tasks.items()):
        where = f"{label}: task {_task_id(task_id, label)!r}"
        if not isinstance(entry, dict):
            raise TaskError(f"{where} must be an object")
        _check_keys(entry, MANIFEST_TASK_KEYS, MANIFEST_TASK_OPTIONAL_KEYS, where)
        license_id = entry.get("license")
        if license_id is not None and not (isinstance(license_id, str) and license_id.strip()):
            raise TaskError(f"{where}: 'license' must be a non-empty string, not {license_id!r}")
        tasks[task_id] = ManifestTask(_sha256(entry["sha256"], f"{where}: 'sha256'"), license_id)

    raw_log = data["changelog"]
    if not isinstance(raw_log, list) or not raw_log:
        raise TaskError(f"{label}: 'changelog' must be a non-empty list")
    changelog = tuple(_changelog_entry(raw, i, label) for i, raw in enumerate(raw_log))
    if changelog[-1].version != version:
        raise TaskError(
            f"{label}: 'version' is {version}, but the last changelog entry is version"
            f" {changelog[-1].version}; every version needs a changelog entry"
        )

    latest: dict[str, tuple[int, str | None]] = {}
    for entry in changelog:
        for task_id, sha in entry.changed.items():
            latest[task_id] = (entry.version, sha)
    for task_id in sorted(tasks.keys() | latest.keys()):
        logged_version, logged = latest.get(task_id, (None, None))
        recorded = tasks[task_id].sha256 if task_id in tasks else None
        if logged == recorded:
            continue
        if logged_version is None:
            problem = "no changelog entry names it"
        elif recorded is None:
            problem = f"changelog version {logged_version} records it, but 'tasks' lacks it"
        else:
            problem = (
                f"its hash differs from changelog version {logged_version}, the latest naming it"
            )
        raise TaskError(
            f"{label}: task {task_id!r}: {problem}; a changed task needs a new set version"
            " whose changelog entry names it with its new hash"
        )
    return Manifest(task_set, version, tasks, changelog, path)


def load_core_list(path: Path) -> CoreList:
    """Read and validate the core task list; `select_core` checks it against loaded tasks."""
    path = Path(path)
    label = f"core list {path}"
    data = _read_json(path, label)
    _check_keys(data, CORE_LIST_KEYS, set(), label)
    version = _version(data["version"], f"{label}: 'version'")
    ids = data["tasks"]
    if not isinstance(ids, list) or not ids:
        raise TaskError(f"{label}: 'tasks' must be a non-empty list of task ids")
    for task_id in ids:
        _task_id(task_id, label)
        if task_id.startswith(HIDDEN_ID_PREFIX):
            raise TaskError(f"{label}: hidden tasks cannot be core tasks; the list is public")
        if ids.count(task_id) > 1:
            raise TaskError(f"{label}: task {task_id!r} is listed twice")
    return CoreList(version, tuple(ids), path)


def select_core(core: CoreList, tasks: Iterable[Task]) -> list[Task]:
    """Return exactly the core tasks, sorted by id; every one must be loaded, from both sets."""
    by_id = {task.id: task for task in tasks}
    if missing := [task_id for task_id in core.task_ids if task_id not in by_id]:
        raise TaskError(f"core list {core.path}: tasks not loaded: {', '.join(missing)}")
    selected = sorted((by_id[task_id] for task_id in core.task_ids), key=lambda t: t.id)
    if absent := [s for s in CORE_SETS if s not in {task.set for task in selected}]:
        raise TaskError(
            f"core list {core.path}: must include tasks of both the {' and '.join(CORE_SETS)}"
            f" sets; none from {', '.join(absent)}"
        )
    return selected


def _changelog_entry(raw: object, index: int, label: str) -> ChangelogEntry:
    where = f"{label}: changelog entry {index}"
    if not isinstance(raw, dict):
        raise TaskError(f"{where} must be an object")
    _check_keys(raw, CHANGELOG_KEYS, set(), where)
    version = _version(raw["version"], f"{where}: 'version'")
    if version != index + 1:
        raise TaskError(f"{where}: 'version' must be {index + 1}, not {version}")
    note = raw["note"]
    if not isinstance(note, str) or not note.strip():
        raise TaskError(f"{where}: 'note' must be a non-empty string")
    changed = raw["changed"]
    if not isinstance(changed, dict) or not changed:
        raise TaskError(f"{where}: 'changed' must name at least one task")
    for task_id, sha in changed.items():
        _task_id(task_id, where)
        if sha is not None:
            _sha256(sha, f"{where}: task {task_id!r}")
    return ChangelogEntry(version, note, dict(changed))


def _read_json(path: Path, label: str) -> dict:
    if not path.is_file():
        raise TaskError(f"{label}: missing file {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
    # ValueError covers malformed JSON, bad UTF-8, repeated keys and the integer-digit limit.
    except (OSError, ValueError, RecursionError) as e:
        raise TaskError(f"{label}: cannot read {path.name}: {e}") from None
    if not isinstance(data, dict):
        raise TaskError(f"{label}: {path.name} must hold a JSON object")
    return data


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        # The key is not quoted: in a task file it could be a hidden canary.
        raise ValueError("a key is repeated")
    return dict(pairs)


def _check_keys(data: dict, required: set[str], optional: set[str], label: str) -> None:
    if missing := sorted(required - data.keys()):
        raise TaskError(f"{label}: missing {', '.join(missing)}")
    if unknown := sorted(data.keys() - required - optional):
        raise TaskError(f"{label}: unknown {', '.join(unknown)}")


def _version(value: object, where: str) -> int:
    if type(value) is not int or value < 1:
        raise TaskError(f"{where} must be a positive integer, not {value!r}")
    return value


def _sha256(value: object, where: str) -> str:
    if not (isinstance(value, str) and SHA256.fullmatch(value)):
        raise TaskError(f"{where} must be 64 lowercase hex digits, not {value!r}")
    return value


def _task_id(value: object, where: str) -> str:
    if not (isinstance(value, str) and TASK_ID.fullmatch(value)):
        raise TaskError(f"{where}: invalid task id {value!r}")
    return value
