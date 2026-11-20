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
import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rbench.tasks.loader import (
    GROUND_TRUTH_FILE,
    HIDDEN_ID_PREFIX,
    SHA256,
    TASK_FILE,
    TASK_ID,
    TASK_SETS,
    Task,
    TaskError,
    load_task,
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
    set_dir: Path, task_set: str, hidden_canary_prefix: str | None = None
) -> tuple[Manifest, list[Task]]:
    """Load the set `task_set` from `set_dir` and check every task against its manifest.

    `task_set` comes from the caller (the directory or the hidden configuration), never from the
    files: it decides what errors may name. In the hidden set they name no task id and quote no
    loader message. This checks the set against its own manifest; `check_version_history` checks
    the manifest against the one committed before it.
    """
    set_dir = Path(set_dir)
    if task_set not in TASK_SETS:
        raise TaskError(f"unknown task set {task_set!r}")
    private = task_set == "hidden"
    tasks = _load_tasks(set_dir, hidden_canary_prefix) if private else load_tasks(set_dir)
    manifest = load_manifest(set_dir / MANIFEST_FILE, task_set)

    def who(index: int, task: Task) -> str:
        if private:
            return f"task {index + 1} of {len(tasks)} in {set_dir} (sorted by id)"
        return f"task {task.path}"

    for index, task in enumerate(tasks):
        if task.set != task_set:
            raise TaskError(f"{who(index, task)} is in set {task.set!r}, not {task_set!r}")
    on_disk = {task.id: task for task in tasks}
    for ids, problem in (
        (sorted(on_disk.keys() - manifest.tasks.keys()), "does not list tasks"),
        (sorted(manifest.tasks.keys() - on_disk.keys()), "lists tasks with no directory"),
    ):
        if ids:
            names = f"{len(ids)} of them" if private else ", ".join(ids)
            raise TaskError(f"{manifest.path} {problem}: {names}")
    for index, task in enumerate(tasks):
        actual = task_content_sha256(task)
        if actual != manifest.tasks[task.id].sha256:
            now = "" if private else f" (now {actual})"
            raise TaskError(
                f"{who(index, task)}: content hash{now} does not match {manifest.path};"
                " a changed task needs a new set version whose changelog entry names it"
            )
    return manifest, tasks


def _load_tasks(set_dir: Path, hidden_canary_prefix: str | None) -> list[Task]:
    """`load_tasks` for the hidden set: a failure gives the task's position, not its message.

    The loader's messages name the task directory, which is the task id, and can quote values.
    """
    if not set_dir.is_dir():
        raise TaskError(f"task set directory {set_dir} does not exist")
    dirs = sorted(d for d in set_dir.iterdir() if d.is_dir() and not d.name.startswith("."))
    for index, task_dir in enumerate(dirs):
        try:
            load_task(task_dir, hidden_canary_prefix)
        except TaskError:
            error = TaskError(
                f"hidden task {index + 1} of {len(dirs)} in {set_dir} (sorted by directory name)"
                " does not load; its message is withheld, since it can carry a hidden id or canary"
            )
        else:
            continue
        raise error
    try:
        return load_tasks(set_dir, hidden_canary_prefix=hidden_canary_prefix)
    except TaskError:
        error = TaskError(
            f"the hidden set {set_dir} does not load: two of its tasks share an id or a canary"
        )
    raise error


def load_manifest(path: Path, task_set: str) -> Manifest:
    """Read and validate the manifest of `task_set`; this does not look at the tasks."""
    path = Path(path)
    label = f"manifest {path}"
    return _parse_manifest(_read_json(path, label), label, path, task_set)


def check_repository_tasks(
    root: Path, repo: Path | None = None, base: str | None = None
) -> list[Task]:
    """The repository check (design D11) of the public tasks directory `root`.

    Nothing under `root` may be a symlink, every directory is a public set named by its
    directory, and every task matches its set's manifest; a core list, if present, resolves.
    Given a git `repo` and a `base` revision, every set at the base still exists and every
    manifest passes `check_version_history` against its committed version.
    """
    root = Path(root)
    for top, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            if os.path.islink(os.path.join(top, name)):
                raise TaskError(f"{os.path.join(top, name)} is a symlink; {root} may hold none")
    if root.is_symlink():
        raise TaskError(f"{root} is a symlink")
    sets = sorted(d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))
    if unknown := [name for name in sets if name not in CORE_SETS]:
        raise TaskError(
            f"{root} holds only the public sets {', '.join(CORE_SETS)}, not {', '.join(unknown)}"
        )
    if repo is not None:
        commit = _resolve(Path(repo), base)
        rel = _repo_path(Path(repo), root)
        listed = _git(
            repo,
            ["ls-tree", "-d", "--name-only", "--end-of-options", commit, "--", f"{rel}/"],
            f"cannot list {root} at {base}",
        )
        if gone := sorted({PurePosixPath(p).name for p in listed.splitlines()} - set(sets)):
            raise TaskError(
                f"task sets {', '.join(gone)} exist at {base} but not in {root};"
                " a set is never removed or renamed"
            )
    tasks: list[Task] = []
    for name in sets:
        manifest, loaded = load_task_set(root / name, name)
        if repo is not None:
            path = root / name / MANIFEST_FILE
            check_version_history(load_committed_manifest(repo, base, path, name), manifest)
        tasks += loaded
    if (root / CORE_LIST_FILE).exists():
        select_core(load_core_list(root / CORE_LIST_FILE), tasks)
    return tasks


def merge_base(repo: Path, ref: str) -> str:
    """The merge base of HEAD and `ref` in `repo`; a shallow clone is refused, not trusted."""
    repo = Path(repo)
    shallow = _git(
        repo, ["rev-parse", "--is-shallow-repository"], f"cannot inspect the repository {repo}"
    )
    if shallow.strip() != "false":
        raise TaskError(
            f"{repo} is a shallow clone, so its merge base cannot be trusted; fetch the full"
            " history (git fetch --unshallow, or fetch-depth: 0 in CI)"
        )
    commit = _resolve(repo, ref)
    return _git(
        repo,
        ["merge-base", "--end-of-options", "HEAD", commit],
        f"cannot find the merge base of HEAD and {ref!r} in {repo}",
    ).strip()


def load_committed_manifest(repo: Path, rev: str, path: Path, task_set: str) -> Manifest | None:
    """The manifest of `task_set` at `path` as committed at `rev` in the git repository `repo`.

    Returns None when `rev` has no file there (a new set). Anything else that stops the read, such
    as a revision missing from a shallow clone, raises `TaskError`: the history check never passes
    for want of a base.
    """
    repo, path = Path(repo), Path(path)
    if path.is_symlink():
        raise TaskError(f"manifest {path} is a symlink")
    commit = _resolve(repo, rev)
    rel = _repo_path(repo, path)
    label = f"manifest {path} at {rev}"
    listed = _git(
        repo,
        ["ls-tree", "--name-only", "--end-of-options", commit, "--", rel],
        f"cannot list {label}",
    )
    if not listed:
        return None
    text = _git(repo, ["show", "--end-of-options", f"{commit}:{rel}"], f"cannot read {label}")
    return _parse_manifest(_parse_json(text, label, path.name), label, path, task_set)


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
    hidden = manifest.set == "hidden"
    for index, old in enumerate(base.changelog):
        if index >= len(manifest.changelog):
            raise TaskError(
                f"{label}: changelog entry {index} of the committed manifest was removed;"
                " changelog entries are append-only"
            )
        new = manifest.changelog[index]
        if new == old:
            continue
        missing = object()
        order = [*new.changed, *(k for k in old.changed if k not in new.changed)]
        differ = [
            (position, task_id)
            for position, task_id in enumerate(order)
            if new.changed.get(task_id, missing) != old.changed.get(task_id, missing)
        ]
        if not differ:
            what = "its note changed"
        elif hidden:
            what = "'changed' entries " + ", ".join(str(p) for p, _ in differ) + " changed"
        else:
            what = "tasks " + ", ".join(t for _, t in differ) + " changed"
        raise TaskError(
            f"{label}: changelog entry {index} differs from the committed manifest ({what});"
            " changelog entries are append-only, so a changed task needs a new set version"
        )

    def sha(entries: dict[str, ManifestTask], task_id: str) -> str | None:
        return entries[task_id].sha256 if task_id in entries else None

    ids = base.tasks.keys() | manifest.tasks.keys()
    changed = sorted(i for i in ids if sha(base.tasks, i) != sha(manifest.tasks, i))
    if changed and manifest.version <= base.version:
        names = f"{len(changed)} tasks" if hidden else ", ".join(changed)
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


def _parse_manifest(data: dict, label: str, path: Path, task_set: str) -> Manifest:
    """Validate a manifest of `task_set`, which the caller trusts; the file's own 'set' is not."""
    if task_set not in TASK_SETS:
        raise TaskError(f"unknown task set {task_set!r}")
    _check_keys(data, MANIFEST_KEYS, set(), label)
    if data["set"] != task_set:
        raise TaskError(f"{label}: 'set' must be {task_set!r}, the set it is loaded as")
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


def _resolve(repo: Path, rev: str | None) -> str:
    """Resolve `rev` to a commit; a ref that looks like an option stays a ref."""
    if not rev:
        raise TaskError("no base revision given")
    return _git(
        repo,
        ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}"],
        f"cannot resolve the base revision {rev!r} in {repo}; the manifest history check needs"
        " it (in CI, check out with fetch-depth: 0)",
    ).strip()


def _repo_path(repo: Path, path: Path) -> str:
    """`path` relative to `repo`, with symlinks resolved in its parents only."""
    try:
        return (path.parent.resolve() / path.name).relative_to(repo.resolve()).as_posix()
    except ValueError:
        raise TaskError(f"{path} is not inside the repository {repo}") from None


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
