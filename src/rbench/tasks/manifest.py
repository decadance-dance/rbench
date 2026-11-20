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
recorded without a changelog entry. Against the manifest committed at a base revision
(`check_version_history`), a changed, added or removed task needs a higher version, and
changelog entries are append-only.

A task's content hash is the SHA-256 of the canonical JSON of its `task.json` (metadata, PR title
and description, archive hash) and `ground_truth.json`: formatting and key order do not change it,
any value does, and it depends on no other task.

The core task list is `<tasks-root>/core.json`: `{"version": 1, "tasks": ["<task-id>", ...]}`.

Errors never quote a value read from these files, and never name a hidden task: they give the
set, the file, the field and the position instead.
"""

import hashlib
import json
import subprocess
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
    where = "a task in set 'hidden'" if task.set == "hidden" else f"task {task.path}"
    content = {}
    for key, name in (("task", TASK_FILE), ("ground_truth", GROUND_TRUTH_FILE)):
        doc = _read_json(task.path / name, where)
        try:
            json.dumps(doc, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            raise TaskError(f"{where}: {name} holds a string that is not valid Unicode") from None
        content[key] = doc
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_task_set(
    set_dir: Path, hidden_canary_prefix: str | None = None
) -> tuple[Manifest, list[Task]]:
    """Load a task set and check every task against its manifest; raise `TaskError` naming the task.

    This checks the set against its own manifest; `check_version_history` checks the manifest
    against the one committed before it.
    """
    set_dir = Path(set_dir)
    tasks = load_tasks(set_dir, hidden_canary_prefix=hidden_canary_prefix)
    manifest = load_manifest(set_dir / MANIFEST_FILE)
    private = manifest.set == "hidden" or any(task.set == "hidden" for task in tasks)

    def who(index: int, task: Task) -> str:
        if private:
            return f"task {index + 1} of {len(tasks)} in {set_dir} (sorted by id)"
        return f"task {task.path}"

    on_disk = {task.id: task for task in tasks}
    for ids, problem in (
        (sorted(on_disk.keys() - manifest.tasks.keys()), "does not list tasks"),
        (sorted(manifest.tasks.keys() - on_disk.keys()), "lists tasks with no directory"),
    ):
        if ids:
            names = f"{len(ids)} of them" if private else ", ".join(ids)
            raise TaskError(f"{manifest.path} {problem}: {names}")
    for index, task in enumerate(tasks):
        if task.set != manifest.set:
            raise TaskError(
                f"{who(index, task)} is in set {task.set!r}, but {manifest.path} is for"
                f" {manifest.set!r}"
            )
        actual = task_content_sha256(task)
        if actual != manifest.tasks[task.id].sha256:
            now = "" if private else f" (now {actual})"
            raise TaskError(
                f"{who(index, task)}: content hash{now} does not match {manifest.path};"
                " a changed task needs a new set version whose changelog entry names it"
            )
    return manifest, tasks


def load_manifest(path: Path) -> Manifest:
    """Read and validate one manifest file; this does not look at the tasks."""
    path = Path(path)
    return _parse_manifest(_read_json(path, f"manifest {path}"), f"manifest {path}", path)


def load_committed_manifest(repo: Path, rev: str, path: Path) -> Manifest | None:
    """The manifest at `path` as committed at `rev` in the git repository `repo`.

    Returns None when `rev` has no file there (a new set). Anything else that stops the read, such
    as a revision missing from a shallow clone, raises `TaskError`: the history check never passes
    for want of a base.
    """
    repo, path = Path(repo), Path(path)
    if not rev or rev.startswith("-"):
        raise TaskError(f"invalid base revision {rev!r}")
    try:
        rel = path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        raise TaskError(f"manifest {path} is not inside the repository {repo}") from None
    commit = _git(
        repo,
        ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
        f"cannot resolve the base revision {rev!r} in {repo}; the manifest history check needs"
        " it (in CI, check out with fetch-depth: 0)",
    ).strip()
    label = f"manifest {path} at {rev}"
    if not _git(repo, ["ls-tree", "--name-only", commit, "--", rel], f"cannot list {label}"):
        return None
    text = _git(repo, ["show", f"{commit}:{rel}"], f"cannot read {label}")
    return _parse_manifest(_parse_json(text, label, path.name), label, path)


def check_version_history(base: Manifest | None, manifest: Manifest) -> None:
    """Check `manifest` against `base`, the same file committed before it (None for a new set).

    A task whose hash changed, or that was added or removed, needs a higher version; changelog
    entries are append-only. With both manifests valid, the new entries then name every change.
    """
    if base is None:
        return
    label = f"manifest {manifest.path}"
    if manifest.set != base.set:
        raise TaskError(f"{label}: 'set' differs from the committed manifest")
    kept = manifest.changelog[: len(base.changelog)]
    for index, entry in enumerate(base.changelog):
        if index >= len(kept) or kept[index] != entry:
            raise TaskError(
                f"{label}: changelog entry {index} differs from the committed manifest;"
                " changelog entries are append-only"
            )

    def sha(entries: dict[str, ManifestTask], task_id: str) -> str | None:
        return entries[task_id].sha256 if task_id in entries else None

    ids = base.tasks.keys() | manifest.tasks.keys()
    changed = sorted(i for i in ids if sha(base.tasks, i) != sha(manifest.tasks, i))
    if changed and manifest.version <= base.version:
        names = f"{len(changed)} tasks" if manifest.set == "hidden" else ", ".join(changed)
        raise TaskError(
            f"{label}: tasks changed since the committed version {base.version} ({names}),"
            " but 'version' is not higher; a changed task needs a new set version whose"
            " changelog entry names it"
        )


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
    for index, task_id in enumerate(ids):
        where = f"{label}: 'tasks' entry {index}"
        if isinstance(task_id, str) and task_id.startswith(HIDDEN_ID_PREFIX):
            raise TaskError(f"{where}: hidden tasks cannot be core tasks; the list is public")
        _task_id(task_id, where, "ours")
        if ids.count(task_id) > 1:
            raise TaskError(f"{label}: task {task_id!r} is listed twice")
    return CoreList(version, tuple(ids), path)


def select_core(core: CoreList, tasks: Iterable[Task]) -> list[Task]:
    """Return exactly the core tasks, sorted by id; every one must be loaded, from both sets."""
    by_id: dict[str, Task] = {}
    for task in tasks:
        other = by_id.setdefault(task.id, task)
        if other is not task:
            if "hidden" in (task.set, other.set):
                raise TaskError("two loaded hidden tasks share an id")
            raise TaskError(f"task id {task.id!r} is loaded twice: {other.path} and {task.path}")
    if missing := [task_id for task_id in core.task_ids if task_id not in by_id]:
        raise TaskError(f"core list {core.path}: tasks not loaded: {', '.join(missing)}")
    selected = sorted((by_id[task_id] for task_id in core.task_ids), key=lambda t: t.id)
    if absent := [s for s in CORE_SETS if s not in {task.set for task in selected}]:
        raise TaskError(
            f"core list {core.path}: must include tasks of both the {' and '.join(CORE_SETS)}"
            f" sets; none from {', '.join(absent)}"
        )
    return selected


def _parse_manifest(data: dict, label: str, path: Path) -> Manifest:
    _check_keys(data, MANIFEST_KEYS, set(), label)
    task_set = data["set"]
    if task_set not in TASK_SETS:
        raise TaskError(f"{label}: 'set' must be one of {', '.join(TASK_SETS)}")
    hidden = task_set == "hidden"
    version = _version(data["version"], f"{label}: 'version'")

    def who(task_id: str, position: str) -> str:
        return position if hidden else f"task {task_id!r}"

    raw_tasks = data["tasks"]
    if not isinstance(raw_tasks, dict):
        raise TaskError(f"{label}: 'tasks' must be an object of task ids")
    tasks: dict[str, ManifestTask] = {}
    positions: dict[str, str] = {}
    for index, (task_id, entry) in enumerate(raw_tasks.items()):
        position = f"'tasks' entry {index}"
        _task_id(task_id, f"{label}: {position}", task_set)
        where = f"{label}: {who(task_id, position)}"
        if not isinstance(entry, dict):
            raise TaskError(f"{where} must be an object")
        _check_keys(entry, MANIFEST_TASK_KEYS, MANIFEST_TASK_OPTIONAL_KEYS, where)
        license_id = entry.get("license")
        if license_id is not None and not (isinstance(license_id, str) and license_id.strip()):
            raise TaskError(f"{where}: 'license' must be a non-empty string")
        tasks[task_id] = ManifestTask(_sha256(entry["sha256"], f"{where}: 'sha256'"), license_id)
        positions[task_id] = position

    raw_log = data["changelog"]
    if not isinstance(raw_log, list) or not raw_log:
        raise TaskError(f"{label}: 'changelog' must be a non-empty list")
    changelog = tuple(_changelog_entry(raw, i, label, task_set) for i, raw in enumerate(raw_log))
    if changelog[-1].version != version:
        raise TaskError(
            f"{label}: 'version' is {version}, but the last changelog entry is version"
            f" {changelog[-1].version}; every version needs a changelog entry"
        )

    latest: dict[str, tuple[int, str | None]] = {}
    for index, entry in enumerate(changelog):
        for position, (task_id, sha) in enumerate(entry.changed.items()):
            latest[task_id] = (entry.version, sha)
            positions.setdefault(task_id, f"changelog entry {index} 'changed' entry {position}")
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
            f"{label}: {who(task_id, positions[task_id])}: {problem}; a changed task needs a new"
            " set version whose changelog entry names it with its new hash"
        )
    return Manifest(task_set, version, tasks, changelog, path)


def _changelog_entry(raw: object, index: int, label: str, task_set: str) -> ChangelogEntry:
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
    for position, (task_id, sha) in enumerate(changed.items()):
        entry_where = f"{where}: 'changed' entry {position}"
        _task_id(task_id, entry_where, task_set)
        if sha is not None:
            _sha256(sha, entry_where)
    return ChangelogEntry(version, note, dict(changed))


def _git(repo: Path, args: list[str], failure: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError as e:
        raise TaskError(f"{failure}: {e}") from None
    if done.returncode != 0:
        detail = done.stderr.strip()
        raise TaskError(f"{failure}{': ' + detail if detail else ''}")
    return done.stdout


def _read_json(path: Path, label: str) -> dict:
    if not path.is_file():
        raise TaskError(f"{label}: missing file {path.name}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as e:
        raise TaskError(f"{label}: cannot read {path.name}: {e}") from None
    return _parse_json(text, label, path.name)


def _parse_json(text: str, label: str, name: str) -> dict:
    try:
        data = json.loads(text, object_pairs_hook=_unique_keys)
    # ValueError covers malformed JSON, repeated keys and the integer-digit limit; none of these
    # messages quotes the input.
    except (ValueError, RecursionError) as e:
        raise TaskError(f"{label}: cannot read {name}: {e}") from None
    if not isinstance(data, dict):
        raise TaskError(f"{label}: {name} must hold a JSON object")
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
    # Unknown keys are counted, not quoted: a key is input like any value.
    if unknown := data.keys() - required - optional:
        raise TaskError(f"{label}: {len(unknown)} unknown keys")


def _version(value: object, where: str) -> int:
    if type(value) is not int or value < 1:
        raise TaskError(f"{where} must be a positive integer")
    return value


def _sha256(value: object, where: str) -> str:
    if not (isinstance(value, str) and SHA256.fullmatch(value)):
        raise TaskError(f"{where} must be 64 lowercase hex digits")
    return value


def _task_id(value: object, where: str, task_set: str) -> str:
    """Apply the loader's id rules: the hidden prefix is reserved for, and required in, `hidden`."""
    if not (isinstance(value, str) and TASK_ID.fullmatch(value)):
        raise TaskError(f"{where}: invalid task id")
    if (task_set == "hidden") != value.startswith(HIDDEN_ID_PREFIX):
        raise TaskError(
            f"{where}: the {HIDDEN_ID_PREFIX!r} id prefix is reserved for, and required in,"
            f" the hidden set; this manifest is for {task_set!r}"
        )
    return value
