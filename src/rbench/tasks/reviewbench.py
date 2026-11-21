"""Import the `reviewbench` task set from a pinned ReviewBench commit (spec review-tasks; design D1).

The importer reads two kinds of file from a local clone of ReviewBench, always as blobs of the
pinned commit (`git cat-file`, replacements and configuration off), so neither the clone's working
tree nor its settings can change what is imported:

    <subset>               a JSON list of PR objects (default `corpus/test/test.json`)
    golden/<pr_key>.json   {"pr_key", "pr": {...}, "findings": [...]}, one per PR

`pr_key` is the PR's `nwo` with `/` replaced by `_`, then `_<pr_number>-<first 8 hex of head>`.
Every golden file is validated whole first, `fp` findings included. Then, for every PR, it:

- rebuilds the title and description in force while the pinned head was the PR's head, from the
  original repository's timeline `renamed` events and body edit history; with a change inside the
  window, a description edited after it, or no readable history, the task is skipped and the
  changelog names it;
- builds the source archive with `build_archive`: the license is read on the original repository
  (`nwo`) at the head, the commits are fetched from ReviewBench's mirror of it; a refused license
  also skips the task, while a failed lookup stops the import;
- maps the golden file's `tp` findings to ground truth (ids `<pr_key>#<n>`, `n` the index among
  all findings) with the severity and producer maps below, and drops every other field.

The new version is built and checked whole in a staging directory next to the set, then swapped
in: a failure leaves the installed version as it was, and a rerun after a crash first restores a
consistent set. Archives are named by task id and SHA-256, so a new version never overwrites one
the installed version uses.

The golden file is never stored: the manifest records its path and SHA-256. Every PR is untrusted
data and is validated whole; an error names the PR key and the finding's index.
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

from rbench.atomic import atomic_write
from rbench.tasks.archive import ArchiveError, archive_name, build_archive
from rbench.tasks.importing import (
    ImportResult,
    PullRequest,
    TaskImportError,
    _changelog,
    _dumps,
    _inside_repo,
    _json,
    _lock,
    _recover,
    _swap,
    _title_and_body,
    _write_task,
)
from rbench.tasks.license import LicenseError, LicenseLookupError
from rbench.tasks.loader import HIDDEN_ID_PREFIX, REPO, TASK_ID, TaskError
from rbench.tasks.manifest import (
    MANIFEST_FILE,
    GoldenFile,
    SourcePin,
    check_version_history,
    load_task_set,
    manifest_task,
)

SET = "reviewbench"
REVIEWBENCH_REPO = "review-bench/ReviewBench"
PINNED_COMMIT = "082f65f6746c39b6ac25b0a62c1b0d5131a3803a"
TEST_SUBSET = "corpus/test/test.json"
MIRROR_BASE = "https://github.com/review-bench/"

SEVERITY_MAP = {"high": "must", "medium": "should", "low": "nit", "style": "nit"}
# How producers map; any other `<engine>:<model>` is kept verbatim.
PRODUCER_MAP = {
    "human_review": "human",
    "deterministic_tool:<tool>": "unknown",
    "<engine>:<model>": "<engine>:<model>",
}

PR_KEYS = {
    "repo",
    "pr_number",
    "pr_url",
    "base",
    "head",
    "title",
    "body",
    "nwo",
    "language",
    "repo_size_kb",
    "lines_added",
    "lines_removed",
    "files_changed",
}
GOLDEN_KEYS = {"pr_key", "pr", "findings"}
GOLDEN_PR_KEYS = {"repo", "pr_number", "base", "head"}
FINDING_KEYS = {
    "file",
    "start_line",
    "end_line",
    "message",
    "tp_fp",
    "severity",
    "category",
    "scope",
    "difficulty",
    "context_required",
    "tp_fp_justification",
    "severity_justification",
    "category_justification",
    "producer",
    "source",
}
SHA1 = re.compile(r"[0-9a-f]{40}")

# The shared import error, under the name callers of this module already use.
ReviewBenchError = TaskImportError


def import_reviewbench(
    checkout: Path,
    set_dir: Path,
    archive_dir: Path,
    commit: str = PINNED_COMMIT,
    subset: str = TEST_SUBSET,
    mirror_base: str = MIRROR_BASE,
) -> ImportResult:
    """Import the PRs of `subset` at `commit` of the ReviewBench clone `checkout` into `set_dir`.

    Archives go to `archive_dir/<sha256>.bundle`, the name `rbench tasks release` uploads. Against
    an existing set in `set_dir`, a run that changes nothing writes no set file; any change, and any
    other commit or subset, makes a new version whose changelog entry names every imported task
    with its hash and every removed or skipped task with null.
    """
    set_dir, archive_dir = Path(set_dir), Path(archive_dir)
    sets, archives = set_dir.resolve(), archive_dir.resolve()
    if sets == archives or sets in archives.parents or archives in sets.parents:
        raise ReviewBenchError(
            f"the archive directory {archive_dir} and the set directory {set_dir} overlap;"
            " the swap of a new version would move or delete the archives"
        )
    with _lock(set_dir):
        return _import(checkout, set_dir, archive_dir, commit, subset, mirror_base)


def _import(
    checkout: Path, set_dir: Path, archive_dir: Path, commit: str, subset: str, mirror_base: str
) -> ImportResult:
    _recover(set_dir)
    for stale in archive_dir.glob(".import-*"):
        shutil.rmtree(stale)  # archives a killed import staged; none is in use
    _require_commit(checkout, commit)
    source = SourcePin(REVIEWBENCH_REPO, commit, subset, SEVERITY_MAP, PRODUCER_MAP)
    old = load_task_set(set_dir, SET)[0] if (set_dir / MANIFEST_FILE).exists() else None
    prs = _read_subset(checkout, commit, subset)
    goldens = {pr.key: _blob(checkout, commit, f"golden/{pr.key}.json") for pr in prs}
    items = {pr.key: _ground_truth(pr, goldens[pr.key]) for pr in prs}

    token = uuid.uuid4().hex
    stage = set_dir.parent / f".{set_dir.name}.import-{token}"
    archive_stage = archive_dir / f".import-{token}"
    stage.mkdir(parents=True)
    archive_stage.mkdir(parents=True)
    try:
        entries, skipped, bundles = {}, {}, {}
        for pr in prs:
            task_id = pr.key.lower()
            texts = _title_and_body(pr)
            if isinstance(texts, str):
                skipped[task_id] = texts
                continue
            mirror = f"{mirror_base}{pr.nwo.replace('/', '_')}"
            out = archive_stage / f"{task_id}.bundle"
            try:
                archive = build_archive(pr.nwo, mirror, pr.base, pr.head, out, SET)
            except LicenseLookupError:
                raise
            except LicenseError as err:
                skipped[task_id] = f"license refused: {err}"
                continue
            task = _write_task(
                stage, SET, task_id, pr, texts, archive.sha256, items[pr.key], set_dir
            )
            golden = GoldenFile(
                f"golden/{pr.key}.json", hashlib.sha256(goldens[pr.key]).hexdigest()
            )
            entries[task_id] = replace(manifest_task(task, archive), golden=golden)
            bundles[task_id] = archive
        changelog, version = _changelog(
            old,
            source,
            entries,
            skipped,
            f"Imported from {source.repo} at {source.commit}, subset {source.subset}.",
            f"the subset {source.subset} at {source.commit} lists no PRs",
        )
        if changelog is not None:
            atomic_write(
                stage / MANIFEST_FILE, _dumps(_manifest(source, version, entries, changelog))
            )
            try:
                new = load_task_set(stage, SET)[0]
                check_version_history(old, new)
            except TaskError as err:
                raise ReviewBenchError(f"the new version does not validate: {err}") from None
        # A name per archive content: a replaced file has the same bytes, so the installed set's
        # archives never change.
        for archive in bundles.values():
            os.replace(archive.path, archive_dir / archive_name(archive.sha256))
        if changelog is not None:
            _swap(set_dir, stage)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(archive_stage, ignore_errors=True)
    return ImportResult(version, changelog is not None, tuple(sorted(entries)), skipped)


def _manifest(source: SourcePin, version: int, entries: dict, changelog: list) -> dict:
    return {
        "set": SET,
        "version": version,
        "source": {
            "repo": source.repo,
            "commit": source.commit,
            "subset": source.subset,
            "severity_map": source.severity_map,
            "producer_map": source.producer_map,
        },
        "tasks": {
            task_id: {
                "sha256": entry.sha256,
                "license": entry.license,
                "golden": {"path": entry.golden.path, "sha256": entry.golden.sha256},
            }
            for task_id, entry in entries.items()
        },
        "changelog": changelog,
    }


def _read_subset(checkout: Path, commit: str, subset: str) -> list[PullRequest]:
    data = _json(_blob(checkout, commit, subset), subset)
    if not isinstance(data, list):
        raise ReviewBenchError(f"{subset} must hold a JSON list of PRs")
    prs, seen = [], set()
    for index, raw in enumerate(data):
        where = f"{subset}: entry {index}"
        if not isinstance(raw, dict) or raw.keys() != PR_KEYS:
            raise ReviewBenchError(
                f"{where} must be an object with exactly {', '.join(sorted(PR_KEYS))}"
            )
        nwo, number, base, head = raw["nwo"], raw["pr_number"], raw["base"], raw["head"]
        if not (isinstance(nwo, str) and REPO.fullmatch(nwo)):
            raise ReviewBenchError(f"{where}: 'nwo' must be an owner/name repository")
        if raw["repo"] != f"https://github.com/{nwo}":
            raise ReviewBenchError(f"{where}: 'repo' is not the URL of {nwo}")
        if type(number) is not int or number < 1:
            raise ReviewBenchError(f"{where}: 'pr_number' must be a positive integer")
        for name, value in (("base", base), ("head", head)):
            if not (isinstance(value, str) and SHA1.fullmatch(value)):
                raise ReviewBenchError(f"{where}: {name!r} must be a full commit id")
        language = raw["language"]
        if not (isinstance(language, str) and language.strip()):
            raise ReviewBenchError(f"{where}: 'language' must be a non-empty string")
        key = f"{nwo.replace('/', '_')}_{number}-{head[:8]}"
        task_id = key.lower()
        if not TASK_ID.fullmatch(task_id) or task_id.startswith(HIDDEN_ID_PREFIX):
            raise ReviewBenchError(f"{where}: {key} does not make a valid public task id")
        if task_id in seen:
            raise ReviewBenchError(f"{where}: {key} is listed twice")
        seen.add(task_id)
        prs.append(PullRequest(key, nwo, number, base, head, language))
    return prs


def _ground_truth(pr: PullRequest, blob: bytes) -> list[dict]:
    """The `tp` findings of a golden file as ground-truth items; anything malformed refuses it."""
    label = f"{pr.key}: golden/{pr.key}.json"
    data = _json(blob, label)
    if not isinstance(data, dict) or data.keys() != GOLDEN_KEYS:
        raise ReviewBenchError(
            f"{label} must be an object with exactly {', '.join(sorted(GOLDEN_KEYS))}"
        )
    meta = data["pr"]
    if data["pr_key"] != pr.key or not isinstance(meta, dict) or meta.keys() != GOLDEN_PR_KEYS:
        raise ReviewBenchError(f"{label}: 'pr_key' or 'pr' does not describe this PR")
    if type(meta["pr_number"]) is not int:
        raise ReviewBenchError(f"{label}: 'pr': 'pr_number' must be an integer")
    if meta["repo"] not in (pr.nwo, f"https://github.com/{pr.nwo}") or (
        meta["pr_number"],
        meta["base"],
        meta["head"],
    ) != (pr.number, pr.base, pr.head):
        raise ReviewBenchError(f"{label}: 'pr' does not match the subset's entry")
    findings = data["findings"]
    if not isinstance(findings, list):
        raise ReviewBenchError(f"{label}: 'findings' must be a list")
    items = []
    for index, raw in enumerate(findings):
        where = f"{label}: finding {index}"
        if not isinstance(raw, dict) or raw.keys() != FINDING_KEYS:
            raise ReviewBenchError(
                f"{where} must be an object with exactly the golden finding keys"
            )
        if raw["tp_fp"] not in ("tp", "fp"):
            raise ReviewBenchError(f"{where}: 'tp_fp' must be 'tp' or 'fp', not {raw['tp_fp']!r}")
        severity = SEVERITY_MAP.get(raw["severity"]) if isinstance(raw["severity"], str) else None
        if severity is None:
            raise ReviewBenchError(f"{where}: unknown severity {raw['severity']!r}")
        producer = _producer(raw["producer"], where)
        file, start, end = raw["file"], raw["start_line"], raw["end_line"]
        if not (isinstance(file, str) and _inside_repo(file)):
            raise ReviewBenchError(f"{where}: 'file' must be a path inside the repository")
        if not (type(start) is int and type(end) is int and 1 <= start <= end):
            raise ReviewBenchError(f"{where}: line range must be 1 <= start_line <= end_line")
        for key in ("message", "category"):
            if not (isinstance(raw[key], str) and raw[key].strip()):
                raise ReviewBenchError(f"{where}: {key!r} must be a non-empty string")
        mapped = {
            "id": f"{pr.key}#{index}",
            "file": file,
            "start_line": start,
            "end_line": end,
            "message": raw["message"],
            "category": raw["category"],
            "severity": severity,
            "producer": producer,
        }
        # Only `tp` items become ground truth (design D1); `fp` ones were validated all the same.
        if raw["tp_fp"] == "tp":
            items.append(mapped)
    return items


def _producer(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise ReviewBenchError(f"{where}: unknown producer form {value!r}")
    if value == "human_review":
        return "human"
    engine, sep, model = value.partition(":")
    if not (sep and engine.strip() and model.strip()):
        raise ReviewBenchError(f"{where}: unknown producer form {value!r}")
    return "unknown" if engine == "deterministic_tool" else value


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess:
    """Git in the clone with no configuration, inherited `GIT_*` variables or replace refs."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    return subprocess.run(
        ["git", "--no-replace-objects", "-C", str(checkout), *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )


def _require_commit(checkout: Path, commit: str) -> None:
    if not SHA1.fullmatch(commit):
        raise ReviewBenchError(f"ReviewBench commit {commit!r} is not a full commit id")
    found = _git(checkout, "cat-file", "-t", commit)
    if found.returncode != 0 or found.stdout.strip() != b"commit":
        raise ReviewBenchError(f"{commit} is not a commit in {checkout}")


def _blob(checkout: Path, commit: str, path: str) -> bytes:
    result = _git(checkout, "cat-file", "blob", f"{commit}:{path}")
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise ReviewBenchError(f"cannot read {path} at {commit} in {checkout}: {stderr}")
    return result.stdout


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The arguments of `rbench tasks import reviewbench`."""
    parser.add_argument(
        "--checkout", type=Path, required=True, help="a local clone of review-bench/ReviewBench"
    )
    parser.add_argument("--commit", default=PINNED_COMMIT, help="the ReviewBench commit")
    parser.add_argument("--subset", default=TEST_SUBSET, help="the PR list at that commit")
    parser.add_argument(
        "--tasks", type=Path, default=Path("tasks") / SET, help="the set directory to write"
    )
    parser.add_argument(
        "--archives", type=Path, required=True, help="where to write the source archives"
    )
    parser.add_argument(
        "--mirror-base", default=MIRROR_BASE, help="prefix of the source mirrors' URLs"
    )


def run(args: argparse.Namespace) -> int:
    """Run `rbench tasks import`; print what changed, or the error, and return the exit code."""
    try:
        result = import_reviewbench(
            args.checkout, args.tasks, args.archives, args.commit, args.subset, args.mirror_base
        )
    except (ReviewBenchError, TaskError, ArchiveError, LicenseError, OSError) as err:
        print(f"rbench tasks import: {err}", file=sys.stderr)
        return 1
    state = "new version" if result.changed else "unchanged, version"
    print(f"{SET}: {state} {result.version}, {len(result.imported)} tasks")
    for task_id, reason in sorted(result.skipped.items()):
        print(f"skipped {task_id}: {reason}")
    return 0
