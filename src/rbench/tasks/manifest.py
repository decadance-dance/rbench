"""Task-set manifests, content hashes and the core task list (spec review-tasks, design D11).

Every task set has `<set-dir>/manifest.json`:

    {"set": "ours", "version": 2,
     "tasks": {"<task-id>": {"sha256": "<content hash>", "license": "MIT"}},
     "changelog": [{"version": 1, "note": "...", "changed": {"<task-id>": "<hash>", ...}},
                   {"version": 2, "note": "...", "changed": {"<task-id>": "<hash>" | null}}]}

`license` is optional: the SPDX id of the source repository's license, recorded by the import
(spec review-tasks, Source license).

The `reviewbench` set also records where it was imported from (spec review-tasks, Pinned
ReviewBench source, Severity mapping from external sets; design D1): an optional top-level
`source` and an optional per-task `golden`, the ReviewBench file the task's ground truth was
mapped from, which is never stored itself:

    "source": {"repo": "review-bench/ReviewBench", "commit": "<40 hex>",
               "subset": "corpus/test/test.json",
               "severity_map": {"high": "must", ...}, "producer_map": {"human_review": "human", ...}}
    "tasks": {"<task-id>": {"sha256": ..., "license": ..., "golden": {"path": ..., "sha256": ...}}}

The `ours` set records how it was mined instead (design D15): the repositories, the merge window,
the filters, the severity rule and labels (spec review-tasks, Severity mapping from external sets):

    "source": {"repos": ["<owner>/<name>", ...], "merged_after": "2026-07-01",
               "merged_before": "2026-11-20", "filters": {"max_per_repo": 3, ...},
               "severity_map": {"M": "must", "S": "should", "N": "nit"},
               "severity_rule": {"must": "...", "should": "...", "nit": "..."},
               "severity_labels": "rule" | "maintainer" | "rule+maintainer"}

A changed `source` needs a higher version, like a changed task.

Changelog entries number the versions 1 to `version`; each names the tasks it changed with
their new hash, or null for a task it removed or an import skipped. A task's hash in `tasks` must
equal the one in the latest changelog entry naming it, so a new hash cannot be recorded without a
changelog entry. Against the manifest committed at a base revision (`check_version_history`), a
changed, added or removed task needs a higher version, and changelog entries are append-only.

A task's content hash is the SHA-256 of the canonical JSON of its `task.json` (metadata, PR title
and description, archive hash) and `ground_truth.json`: formatting and key order do not change it,
any value does, and it depends on no other task.

The core task list is `<tasks-root>/core.json`: `{"version": 1, "tasks": ["<task-id>", ...]}`.

Errors never quote a value read from these files, and never name a hidden task or a private
path: they give the set, the file, the field and the position instead. The hidden set loads only
through `load_hidden_task_set`, from the configuration `hidden_config` issued.
"""

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rbench.tasks.archive import LICENSED_SETS, SourceArchive
from rbench.tasks.hidden import HiddenConfig, load_hidden_tasks
from rbench.tasks.loader import (
    GROUND_TRUTH_FILE,
    HIDDEN_DIR_ENV,
    HIDDEN_ID_PREFIX,
    REPO,
    SEVERITIES,
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
MANIFEST_OPTIONAL_KEYS = {"source"}
MANIFEST_TASK_KEYS = {"sha256"}
MANIFEST_TASK_OPTIONAL_KEYS = {"license", "golden"}
SOURCE_KEYS = {"repo", "commit", "subset", "severity_map", "producer_map"}
MINED_SOURCE_KEYS = {
    "repos",
    "merged_after",
    "merged_before",
    "filters",
    "severity_map",
    "severity_rule",
    "severity_labels",
}
SEVERITY_LABELS = ("rule", "maintainer", "rule+maintainer")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
GOLDEN_KEYS = {"path", "sha256"}
SHA1 = re.compile(r"[0-9a-f]{40}")
# Only this set is imported from a pinned external source, with a golden file per task.
SOURCED_SETS = ("reviewbench",)
# This set records how it was mined.
MINED_SETS = ("ours",)
CHANGELOG_KEYS = {"version", "note", "changed"}
CORE_LIST_KEYS = {"version", "tasks"}


@dataclass(frozen=True)
class GoldenFile:
    """The external file a task's ground truth was mapped from, at the pinned commit."""

    path: str
    sha256: str


@dataclass(frozen=True)
class SourcePin:
    """The external commit a set was imported from, and how its severities and producers map."""

    repo: str
    commit: str
    subset: str
    severity_map: dict[str, str]
    producer_map: dict[str, str]


@dataclass(frozen=True)
class MinedSource:
    """How a mined set was built: repositories, merge window, filters and severity rule."""

    repos: tuple[str, ...]
    merged_after: str
    merged_before: str
    filters: dict[str, int]
    severity_map: dict[str, str]
    severity_rule: dict[str, str]
    severity_labels: str


@dataclass(frozen=True)
class ManifestTask:
    sha256: str
    license: str | None
    golden: GoldenFile | None = None


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
    source: SourcePin | MinedSource | None = None

    def __repr__(self) -> str:
        # A hidden manifest's tasks and changelog are hidden ids, and its path is private.
        if self.set == "hidden":
            return f"Manifest(set='hidden', version={self.version})"
        return f"Manifest(set={self.set!r}, version={self.version}, path={str(self.path)!r})"


def _manifest_label(task_set: str, path: Path) -> str:
    return (
        f"manifest {HIDDEN_DIR_ENV}/{MANIFEST_FILE}" if task_set == "hidden" else f"manifest {path}"
    )


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


def manifest_task(task: Task, archive: SourceArchive) -> ManifestTask:
    """The manifest entry of an imported `task` whose source `archive` was built for it.

    The archive must be the one `task.json` records. A `reviewbench` or `ours` task records the
    license id the archive's license check returned; a hidden task skips that check and records
    none.
    """
    where = "a task in set 'hidden'" if task.set == "hidden" else f"task {task.path}"
    if archive.sha256 != task.archive_sha256:
        raise TaskError(f"{where}: the archive's SHA-256 is not the one {TASK_FILE} records")
    if task.set not in LICENSED_SETS:
        return ManifestTask(task_content_sha256(task), None)
    if archive.license is None:
        raise TaskError(f"{where}: the archive was built without the source license check")
    return ManifestTask(task_content_sha256(task), archive.license)


def load_task_set(set_dir: Path, task_set: str) -> tuple[Manifest, list[Task]]:
    """Load the public set `task_set` from `set_dir` and check every task against its manifest.

    `task_set` comes from the caller (the directory), never from the files. This checks the set
    against its own manifest; `check_version_history` checks the manifest against the one
    committed before it. The hidden set loads through `load_hidden_task_set`.
    """
    set_dir = Path(set_dir)
    if task_set not in TASK_SETS:
        raise TaskError(f"unknown task set {task_set!r}")
    if task_set == "hidden":
        raise TaskError(
            f"the hidden set loads only through load_hidden_task_set, from {HIDDEN_DIR_ENV}"
        )
    tasks = load_tasks(set_dir)
    return _check_task_set(load_manifest(set_dir / MANIFEST_FILE, task_set), tasks), tasks


def load_hidden_task_set(config: HiddenConfig) -> tuple[Manifest, list[Task]]:
    """Load the hidden set of a configuration `hidden_config` issued and check it as above.

    Errors name no task id, canary or path: tasks by their position, files by their name in
    the directory `RBENCH_HIDDEN_DIR` names.
    """
    tasks = load_hidden_tasks(config)
    manifest = load_manifest(config.inside(MANIFEST_FILE), "hidden")
    return _check_task_set(manifest, tasks), tasks


def _check_task_set(manifest: Manifest, tasks: list[Task]) -> Manifest:
    task_set = manifest.set
    private = task_set == "hidden"
    label = _manifest_label(task_set, manifest.path)

    def who(index: int, task: Task) -> str:
        if private:
            return f"task {index + 1} of {len(tasks)} in {HIDDEN_DIR_ENV} (sorted by id)"
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
            raise TaskError(f"{label} {problem}: {names}")
    for index, task in enumerate(tasks):
        actual = task_content_sha256(task)
        if actual != manifest.tasks[task.id].sha256:
            now = "" if private else f" (now {actual})"
            raise TaskError(
                f"{who(index, task)}: content hash{now} does not match {label};"
                " a changed task needs a new set version whose changelog entry names it"
            )
    return manifest


def load_manifest(path: Path, task_set: str) -> Manifest:
    """Read and validate the manifest of `task_set`; this does not look at the tasks."""
    path = Path(path)
    label = _manifest_label(task_set, path)
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
    label = f"{_manifest_label(task_set, path)} at {rev}"
    if path.is_symlink():
        raise TaskError(f"{label} is a symlink")
    try:
        commit = _resolve(repo, rev)
        rel = _repo_path(repo, path)
        listed = _git(
            repo,
            ["ls-tree", "--name-only", "--end-of-options", commit, "--", rel],
            f"cannot list {label}",
        )
        text = listed and _git(
            repo, ["show", "--end-of-options", f"{commit}:{rel}"], f"cannot read {label}"
        )
        error = None
    except TaskError:
        if task_set != "hidden":
            raise
        # Git's messages name the private repository and its paths: only the label is kept, and
        # the error is raised outside the handler, so nothing is chained.
        error = TaskError(f"{label}: git cannot read it at that revision")
    if error:
        raise error
    if not listed:
        return None
    return _parse_manifest(_parse_json(text, label, path.name), label, path, task_set)


def check_version_history(base: Manifest | None, manifest: Manifest) -> None:
    """Check `manifest` against `base`, the same file committed before it (None for a new set).

    A task whose hash changed, or that was added or removed, needs a higher version; changelog
    entries are append-only. With both manifests valid, the new entries then name every change.
    """
    if base is None:
        return
    label = _manifest_label(manifest.set, manifest.path)
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

    def sha(entries: dict[str, ManifestTask], task_id: str) -> tuple | None:
        # The golden file is provenance of the ground truth: changing it is a change too.
        entry = entries.get(task_id)
        return (entry.sha256, entry.golden) if entry else None

    if manifest.source != base.source and manifest.version <= base.version:
        raise TaskError(
            f"{label}: 'source' changed since the committed version {base.version}, but"
            " 'version' is not higher; importing from another source makes a new set version"
        )
    ids = base.tasks.keys() | manifest.tasks.keys()
    changed = sorted(i for i in ids if sha(base.tasks, i) != sha(manifest.tasks, i))
    if changed and manifest.version <= base.version:
        names = f"{len(changed)} tasks" if hidden else ", ".join(changed)
        raise TaskError(
            f"{label}: tasks changed since the committed version {base.version} ({names}),"
            " but 'version' is not higher; a changed task needs a new set version whose"
            " changelog entry names it"
        )
    # Parsing ties a content hash to the changelog, not a golden file: check every change here.
    appended = {t for entry in manifest.changelog[len(base.changelog) :] for t in entry.changed}
    if unnamed := [task_id for task_id in changed if task_id not in appended]:
        names = f"{len(unnamed)} tasks" if hidden else ", ".join(unnamed)
        raise TaskError(
            f"{label}: tasks changed since the committed version {base.version} ({names}),"
            " but no changelog entry after it names them"
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
    _check_keys(data, MANIFEST_KEYS, MANIFEST_OPTIONAL_KEYS, label)
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
        if "golden" in entry and task_set not in SOURCED_SETS:
            raise TaskError(f"{where}: only the {', '.join(SOURCED_SETS)} set records 'golden'")
        license_id = entry.get("license")
        if license_id is not None and not (isinstance(license_id, str) and license_id.strip()):
            raise TaskError(f"{where}: 'license' must be a non-empty string")
        golden = _golden(entry["golden"], f"{where}: 'golden'") if "golden" in entry else None
        tasks[task_id] = ManifestTask(
            _sha256(entry["sha256"], f"{where}: 'sha256'"), license_id, golden
        )
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
    source = None
    if "source" in data:
        if task_set in MINED_SETS:
            source = _mined_source(data["source"], f"{label}: 'source'")
        elif task_set in SOURCED_SETS:
            source = _source(data["source"], f"{label}: 'source'")
        else:
            sets = " and ".join((*SOURCED_SETS, *MINED_SETS))
            raise TaskError(f"{label}: only the {sets} sets record 'source'")
    # A pinned source and per-task golden files only make sense together.
    for task_id, entry in tasks.items():
        if task_set in SOURCED_SETS and (entry.golden is None) != (source is None):
            need = (
                "'golden', since the manifest has a 'source'"
                if source
                else "a top-level 'source' for its 'golden'"
            )
            raise TaskError(f"{label}: {who(task_id, positions[task_id])} needs {need}")
    return Manifest(task_set, version, tasks, changelog, path, source)


def _source(raw: object, where: str) -> SourcePin:
    if not isinstance(raw, dict):
        raise TaskError(f"{where} must be an object")
    _check_keys(raw, SOURCE_KEYS, set(), where)
    repo = raw["repo"]
    if not (isinstance(repo, str) and REPO.fullmatch(repo)):
        raise TaskError(f"{where}: 'repo' must be an owner/name repository")
    commit = raw["commit"]
    if not (isinstance(commit, str) and SHA1.fullmatch(commit)):
        raise TaskError(f"{where}: 'commit' must be 40 lowercase hex digits")
    severities = _string_map(raw["severity_map"], f"{where}: 'severity_map'")
    if any(value not in SEVERITIES for value in severities.values()):
        raise TaskError(f"{where}: 'severity_map' values must be one of {', '.join(SEVERITIES)}")
    return SourcePin(
        repo=repo,
        commit=commit,
        subset=_tree_path(raw["subset"], f"{where}: 'subset'"),
        severity_map=severities,
        producer_map=_string_map(raw["producer_map"], f"{where}: 'producer_map'"),
    )


def _mined_source(raw: object, where: str) -> MinedSource:
    if not isinstance(raw, dict):
        raise TaskError(f"{where} must be an object")
    _check_keys(raw, MINED_SOURCE_KEYS, set(), where)
    repos = raw["repos"]
    if not (
        isinstance(repos, list)
        and repos
        and all(isinstance(r, str) and REPO.fullmatch(r) for r in repos)
        and len(set(repos)) == len(repos)
    ):
        raise TaskError(f"{where}: 'repos' must be a non-empty list of distinct owner/name ids")
    for key in ("merged_after", "merged_before"):
        if not (isinstance(raw[key], str) and DATE.fullmatch(raw[key])):
            raise TaskError(f"{where}: {key!r} must be a YYYY-MM-DD date")
    if raw["merged_after"] >= raw["merged_before"]:
        raise TaskError(f"{where}: 'merged_after' must be before 'merged_before'")
    filters = raw["filters"]
    if not (
        isinstance(filters, dict)
        and all(type(v) is int and v > 0 and k.strip() for k, v in filters.items())
    ):
        raise TaskError(f"{where}: 'filters' must map names to positive integers")
    severities = _string_map(raw["severity_map"], f"{where}: 'severity_map'")
    if any(value not in SEVERITIES for value in severities.values()):
        raise TaskError(f"{where}: 'severity_map' values must be one of {', '.join(SEVERITIES)}")
    rule = _string_map(raw["severity_rule"], f"{where}: 'severity_rule'")
    if set(rule) != set(SEVERITIES):
        raise TaskError(f"{where}: 'severity_rule' must have exactly {', '.join(SEVERITIES)}")
    if raw["severity_labels"] not in SEVERITY_LABELS:
        raise TaskError(f"{where}: 'severity_labels' must be one of {', '.join(SEVERITY_LABELS)}")
    return MinedSource(
        tuple(repos),
        raw["merged_after"],
        raw["merged_before"],
        dict(filters),
        severities,
        rule,
        raw["severity_labels"],
    )


def _golden(raw: object, where: str) -> GoldenFile:
    if not isinstance(raw, dict):
        raise TaskError(f"{where} must be an object")
    _check_keys(raw, GOLDEN_KEYS, set(), where)
    return GoldenFile(
        _tree_path(raw["path"], f"{where}: 'path'"), _sha256(raw["sha256"], f"{where}: 'sha256'")
    )


def _string_map(raw: object, where: str) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise TaskError(f"{where} must be a non-empty object")
    for key, value in raw.items():
        if not (isinstance(value, str) and key.strip() and value.strip()):
            raise TaskError(f"{where}: keys and values must be non-empty strings")
    return dict(raw)


def _tree_path(value: object, where: str) -> str:
    """A relative path inside a repository tree, with no empty, `.` or `..` parts."""
    parts = value.split("/") if isinstance(value, str) else [""]
    if any(part in ("", ".", "..") for part in parts) or any(ord(c) < 32 for c in value):
        raise TaskError(f"{where} must be a relative path inside the repository")
    return value


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
    # An OSError's message holds the path, which may be private: only its reason is kept. The
    # error is raised outside the handler, so the file's bytes are not chained to it.
    error, text = None, ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        error = e.strerror
    except ValueError as e:
        error = str(e)
    if error is not None:
        raise TaskError(f"{label}: cannot read {path.name}: {error}")
    return _parse_json(text, label, path.name)


def _parse_json(text: str, label: str, name: str) -> dict:
    error, data = None, None
    try:
        data = json.loads(text, object_pairs_hook=_unique_keys)
    # ValueError covers malformed JSON, repeated keys and the integer-digit limit; none of these
    # messages quotes the input. Raised outside the handler: a JSON error holds the whole text.
    except (ValueError, RecursionError) as e:
        error = str(e)
    if error is not None:
        raise TaskError(f"{label}: cannot read {name}: {error}")
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
