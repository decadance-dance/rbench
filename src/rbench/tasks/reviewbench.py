"""Import the `reviewbench` task set from a pinned ReviewBench commit (spec review-tasks; design D1).

The importer reads three kinds of file from a local clone of ReviewBench, always as blobs of the
pinned commit (`git cat-file`), so the clone's working tree cannot change what is imported:

    <subset>               a JSON list of PR objects (default `corpus/test/test.json`)
    golden/<pr_key>.json   {"pr_key", "pr": {...}, "findings": [...]}, one per PR

`pr_key` is the PR's `nwo` with `/` replaced by `_`, then `_<pr_number>-<first 8 hex of head>`.
For every PR it then:

- rebuilds the title and description in force while the pinned head was the PR's head, from the
  original repository's timeline `renamed` events and body edit history; with a change inside the
  window, or no readable history, the task is skipped and the changelog names it;
- builds the source archive with `build_archive`: the license is read on the original repository
  (`nwo`) at the head, the commits are fetched from ReviewBench's mirror of it; a refused license
  also skips the task;
- maps the golden file's `tp` findings to ground truth (ids `<pr_key>#<n>`, `n` the index among
  all findings) with the severity and producer maps below, and drops every other field.

The golden file is never stored: the manifest records its path and SHA-256. Every PR is untrusted
data and is validated whole; an error names the PR key and the finding's index.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from rbench.atomic import atomic_write
from rbench.tasks.archive import ArchiveError, build_archive
from rbench.tasks.license import LicenseError
from rbench.tasks.loader import (
    GROUND_TRUTH_FILE,
    HIDDEN_ID_PREFIX,
    PUBLIC_CANARY_PREFIX,
    REPO,
    TASK_FILE,
    TASK_ID,
    TaskError,
    load_task,
)
from rbench.tasks.manifest import (
    MANIFEST_FILE,
    GoldenFile,
    Manifest,
    SourcePin,
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
EDITS_PAGE = 100

EDITS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      body
      userContentEdits(first: 100) { totalCount nodes { editedAt deletedAt diff } }
    }
  }
}
"""


class ReviewBenchError(ValueError):
    """An import that cannot go on; the message names the PR key or the file."""


@dataclass(frozen=True)
class PullRequest:
    key: str
    nwo: str
    number: int
    base: str
    head: str
    language: str


@dataclass(frozen=True)
class ImportResult:
    version: int
    changed: bool
    imported: tuple[str, ...]
    skipped: dict[str, str]


def import_reviewbench(
    checkout: Path,
    set_dir: Path,
    archive_dir: Path,
    commit: str = PINNED_COMMIT,
    subset: str = TEST_SUBSET,
    mirror_base: str = MIRROR_BASE,
) -> ImportResult:
    """Import the PRs of `subset` at `commit` of the ReviewBench clone `checkout` into `set_dir`.

    Archives go to `archive_dir/<task-id>.bundle`. Against an existing set in `set_dir`, a run that
    changes nothing writes nothing; any change, and any other commit or subset, makes a new
    version whose changelog entry names every imported task with its hash and every removed or
    skipped task with null.
    """
    if not SHA1.fullmatch(commit):
        raise ReviewBenchError(f"ReviewBench commit {commit!r} is not a full commit id")
    set_dir, archive_dir = Path(set_dir), Path(archive_dir)
    source = SourcePin(REVIEWBENCH_REPO, commit, subset, SEVERITY_MAP, PRODUCER_MAP)
    old = load_task_set(set_dir, SET)[0] if (set_dir / MANIFEST_FILE).exists() else None
    prs = _read_subset(checkout, commit, subset)

    staging = set_dir / f".import-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        entries, skipped = {}, {}
        for pr in prs:
            task_id = pr.key.lower()
            golden_path = f"golden/{pr.key}.json"
            blob = _blob(checkout, commit, golden_path)
            items = _ground_truth(pr, blob)
            texts = _title_and_body(pr)
            if isinstance(texts, str):
                skipped[task_id] = texts
                continue
            out = archive_dir / f"{task_id}.bundle"
            mirror = f"{mirror_base}{pr.nwo.replace('/', '_')}"
            try:
                archive = build_archive(pr.nwo, mirror, pr.base, pr.head, out, SET)
            except LicenseError as err:
                skipped[task_id] = f"license refused: {err}"
                continue
            task = _write_task(staging, task_id, pr, texts, archive.sha256, items, set_dir)
            golden = GoldenFile(golden_path, hashlib.sha256(blob).hexdigest())
            entries[task_id] = replace(manifest_task(task, archive), golden=golden)
        changelog, version = _changelog(old, source, entries, skipped)
        if changelog is None:
            return ImportResult(version, False, tuple(sorted(entries)), skipped)
        _install(set_dir, staging, entries, old)
        manifest = {
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
        atomic_write(set_dir / MANIFEST_FILE, _dumps(manifest))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    load_task_set(set_dir, SET)
    return ImportResult(version, True, tuple(sorted(entries)), skipped)


def _changelog(
    old: Manifest | None,
    source: SourcePin,
    entries: dict,
    skipped: dict[str, str],
) -> tuple[list | None, int]:
    """The new changelog and version, or (None, old version) when nothing changed."""
    current = {task_id: entry.sha256 for task_id, entry in entries.items()}
    if old is not None:
        before = {task_id: entry.sha256 for task_id, entry in old.tasks.items()}
        if old.source == source and before == current:
            return None, old.version
    if not current and not skipped:
        raise ReviewBenchError(f"the subset {source.subset} at {source.commit} lists no PRs")
    removed = sorted(old.tasks.keys() - current.keys() - skipped.keys()) if old else []
    changed = {**current, **dict.fromkeys(sorted(skipped)), **dict.fromkeys(removed)}
    note = f"Imported from {source.repo} at {source.commit}, subset {source.subset}."
    if skipped:
        note += " Skipped: " + "; ".join(f"{t} ({skipped[t]})" for t in sorted(skipped)) + "."
    if removed:
        note += " Removed: " + ", ".join(removed) + "."
    version = old.version + 1 if old else 1
    history = [
        {"version": e.version, "note": e.note, "changed": e.changed}
        for e in (old.changelog if old else ())
    ]
    return [*history, {"version": version, "note": note, "changed": changed}], version


def _install(set_dir: Path, staging: Path, entries: dict, old: Manifest | None) -> None:
    """Move the staged task directories into `set_dir` and drop the tasks no longer in it."""
    for task_id in sorted({*entries, *(old.tasks if old else ())}):
        target = set_dir / task_id
        if target.exists():
            shutil.rmtree(target)
        if task_id in entries:
            os.replace(staging / task_id, target)


def _write_task(
    staging: Path, task_id: str, pr: PullRequest, texts, archive_sha: str, items, set_dir
):
    title, body = texts
    task_dir = staging / task_id
    task_dir.mkdir()
    meta = {
        "id": task_id,
        "set": SET,
        "repo": pr.nwo,
        "pr": pr.number,
        "base": pr.base,
        "head": pr.head,
        "archive_sha256": archive_sha,
        "language": pr.language,
        "title": title,
        "description": body,
    }
    atomic_write(task_dir / TASK_FILE, _dumps(meta))
    canary = _existing_canary(set_dir / task_id) or PUBLIC_CANARY_PREFIX + uuid.uuid4().hex
    atomic_write(task_dir / GROUND_TRUTH_FILE, _dumps({"canary": canary, "items": items}))
    try:
        return load_task(task_dir)
    except TaskError as err:
        raise ReviewBenchError(f"{pr.key}: the imported task does not load: {err}") from None


def _existing_canary(task_dir: Path) -> str | None:
    """Keep a re-imported task's canary, so an unchanged task keeps its content hash."""
    try:
        return load_task(task_dir).canary
    except TaskError:
        return None


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
        if raw["tp_fp"] == "fp":
            continue
        items.append(
            {
                "id": f"{pr.key}#{index}",
                "file": raw["file"],
                "start_line": raw["start_line"],
                "end_line": raw["end_line"],
                "message": raw["message"],
                "category": raw["category"],
                "severity": severity,
                "producer": producer,
            }
        )
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


def _title_and_body(pr: PullRequest) -> tuple[str, str] | str:
    """The title and body in force from the head's committer date to the merge or close.

    Returns the reason for a skip instead when the history shows a change inside that window or
    cannot show the value. A failing `gh` stops the import: a skip must come from evidence, not
    from an outage.
    """
    owner, name = pr.nwo.split("/")
    pull = _gh_object(["api", f"repos/{pr.nwo}/pulls/{pr.number}"], pr)
    commit = _gh_object(["api", f"repos/{pr.nwo}/commits/{pr.head}"], pr)
    pages = _gh(["api", "--paginate", "--slurp", f"repos/{pr.nwo}/issues/{pr.number}/timeline"], pr)
    edits = _gh_object(
        [
            "api",
            "graphql",
            "-f",
            f"query={EDITS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={pr.number}",
        ],
        pr,
    )
    try:
        start = _time(commit["commit"]["committer"]["date"])
        created = _time(pull["created_at"])
        end_text = pull["merged_at"] or pull["closed_at"]
        end = _time(end_text) if end_text is not None else None
        title = pull["title"]
        events = [event for page in pages for event in page]  # type: ignore[union-attr]
        renames = [
            (_time(e["created_at"]), e["rename"]["from"], e["rename"]["to"])
            for e in events
            if e["event"] == "renamed"
        ]
        node = edits["data"]["repository"]["pullRequest"]
        body, history = node["body"], node["userContentEdits"]
        total, nodes = history["totalCount"], history["nodes"]
        revisions = [(_time(n["editedAt"]), n["deletedAt"], n["diff"]) for n in nodes]
    except (KeyError, TypeError, ValueError, AttributeError) as err:
        raise ReviewBenchError(f"{pr.key}: malformed reply from GitHub: {err!r}") from None
    if not all(isinstance(s, str) for s in (title, body)):
        raise ReviewBenchError(f"{pr.key}: malformed reply from GitHub: title or body")

    def inside(when: datetime) -> bool:
        return start <= when and (end is None or when <= end)

    def after(when: datetime) -> bool:
        return end is not None and when > end

    renames.sort(key=lambda rename: rename[0])
    if any(inside(when) for when, _, _ in renames):
        return "title changed inside the review window"
    for when, old, new in reversed(renames):
        if not after(when):
            break
        if new != title or not isinstance(old, str):
            return "title history does not lead to the current title"
        title = old

    if total != len(revisions) or total > EDITS_PAGE:
        return "description edit history is not fully readable"
    revisions.sort(key=lambda revision: revision[0])
    # GitHub keeps the text the PR was opened with as its oldest revision; that is no change.
    if any(inside(when) for when, _, _ in revisions if when > created):
        return "description changed inside the review window"
    if revisions and after(revisions[-1][0]):
        # Edited after the window: the value in force is the last revision before it. The newest
        # revision must equal the current body, which shows that a revision holds the whole text.
        before = [revision for revision in revisions if not after(revision[0])]
        if revisions[-1][2] != body or not before or before[-1][1] is not None:
            return "description before a later edit cannot be read"
        body = before[-1][2]
        if not isinstance(body, str):
            return "description before a later edit cannot be read"
    if not title.strip():
        return "title is empty"
    return title, body


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("a timestamp is not a string")
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        raise ValueError("a timestamp has no time zone")
    return when


def _gh(args: list[str], pr: PullRequest) -> object:
    what = "gh api " + next(a for a in args[1:] if not a.startswith("-"))
    try:
        result = subprocess.run(
            ["gh", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
        )
    except OSError as err:
        raise ReviewBenchError(f"{pr.key}: cannot run gh: {err}") from None
    if result.returncode != 0:
        raise ReviewBenchError(f"{pr.key}: {what} failed: {result.stderr.strip()}")
    return _json(result.stdout.encode(), f"{pr.key}: {what}")


def _gh_object(args: list[str], pr: PullRequest) -> dict:
    reply = _gh(args, pr)
    if not isinstance(reply, dict):
        raise ReviewBenchError(f"{pr.key}: gh api did not return an object")
    return reply


def _blob(checkout: Path, commit: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(checkout), "cat-file", "blob", f"{commit}:{path}"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise ReviewBenchError(f"cannot read {path} at {commit} in {checkout}: {stderr}")
    return result.stdout


def _json(data: bytes, label: str) -> object:
    error = None
    try:
        return json.loads(data, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as err:
        error = str(err)
    raise ReviewBenchError(f"{label}: not valid JSON: {error}")


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError("a key is repeated")
    return data


def _dumps(data: object) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The arguments of `rbench tasks import`."""
    parser.add_argument("set", choices=[SET], help="the task set to import")
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
