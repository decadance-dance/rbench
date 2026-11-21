"""What the task-set importers share (spec review-tasks; design D1, D11).

An importer builds a new set version whole in a staging directory next to the set, under an
exclusive lock, and swaps it in only once it validates; `_recover` finishes or undoes what a killed
import left. A task's title and description are the ones in force while its reviewed head was the
PR's head (`_title_and_body`, design D1); without that evidence the task is skipped and the
changelog names it. Every GitHub reply is untrusted data, validated before use.
"""

import fcntl
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from rbench.atomic import atomic_write
from rbench.tasks.loader import (
    GROUND_TRUTH_FILE,
    PUBLIC_CANARY_PREFIX,
    TASK_FILE,
    TaskError,
    load_task,
)
from rbench.tasks.manifest import Manifest

EDITS_PAGE = 100

EDITS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      body
      userContentEdits(first: 100) { totalCount nodes { editedAt } }
    }
  }
}
"""


class TaskImportError(ValueError):
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


@contextmanager
def _lock(set_dir: Path) -> Iterator[None]:
    """Hold an exclusive lock on `set_dir` for one import; a second importer fails at once."""
    set_dir.parent.mkdir(parents=True, exist_ok=True)
    path = set_dir.parent / f".{set_dir.name}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TaskImportError(
                f"another import of {set_dir} is running (it holds {path}); try again after it ends"
            ) from None
        yield
    finally:
        os.close(fd)


def _changelog(
    old: Manifest | None,
    source: object,
    entries: dict,
    skipped: dict[str, str],
    note: str,
    empty: str,
    force: bool = False,
) -> tuple[list | None, int]:
    """The new changelog and version, or (None, old version) when nothing changed.

    `force` makes a new version even then, for news only the changelog carries (a new skip).
    """
    current = {task_id: entry.sha256 for task_id, entry in entries.items()}
    if not force and old is not None and old.source == source and old.tasks == entries:
        return None, old.version
    if not current and not skipped:
        raise TaskImportError(empty)
    removed = sorted(old.tasks.keys() - current.keys() - skipped.keys()) if old else []
    changed = {**current, **dict.fromkeys(sorted(skipped)), **dict.fromkeys(removed)}
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


def _swap(set_dir: Path, stage: Path) -> None:
    """Replace `set_dir` with the validated `stage`; `_recover` finishes an interrupted swap."""
    previous = set_dir.parent / f".{set_dir.name}.previous-{uuid.uuid4().hex}"
    if set_dir.exists():
        os.rename(set_dir, previous)
    try:
        os.rename(stage, set_dir)
    except Exception:
        # Put the installed version back now; `_recover` covers only a process that died here.
        if previous.exists() and not set_dir.exists():
            os.rename(previous, set_dir)
        raise
    shutil.rmtree(previous, ignore_errors=True)


def _recover(set_dir: Path) -> None:
    """Undo what a killed import left next to `set_dir`, ending with a consistent set.

    A staged version was never installed, so it goes. A previous version whose swap did not finish
    is restored when `set_dir` is missing, and removed when the swap completed.
    """
    parent = set_dir.parent
    if not parent.is_dir():
        return
    for stale in parent.glob(f".{set_dir.name}.import-*"):
        shutil.rmtree(stale)
    previous = sorted(parent.glob(f".{set_dir.name}.previous-*"))
    if previous and not set_dir.exists():
        if len(previous) > 1:
            names = ", ".join(p.name for p in previous)
            raise TaskImportError(
                f"{set_dir} is missing and several earlier versions are next to it ({names});"
                " move the right one back by hand"
            )
        os.rename(previous.pop(), set_dir)
    for stale in previous:
        shutil.rmtree(stale)


def _write_task(
    staging: Path,
    task_set: str,
    task_id: str,
    pr: PullRequest,
    texts,
    archive_sha: str,
    items,
    set_dir,
    canary: str | None = None,
):
    title, body = texts
    task_dir = staging / task_id
    task_dir.mkdir()
    meta = {
        "id": task_id,
        "set": task_set,
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
    # An assigned canary stays; a new task gets `canary`, or a random one when none is given.
    canary = (
        _existing_canary(set_dir / task_id) or canary or PUBLIC_CANARY_PREFIX + uuid.uuid4().hex
    )
    atomic_write(task_dir / GROUND_TRUTH_FILE, _dumps({"canary": canary, "items": items}))
    try:
        return load_task(task_dir)
    except TaskError as err:
        raise TaskImportError(f"{pr.key}: the imported task does not load: {err}") from None


def _existing_canary(task_dir: Path) -> str | None:
    """Keep a re-imported task's canary, so an unchanged task keeps its content hash."""
    try:
        return load_task(task_dir).canary
    except TaskError:
        return None


def _inside_repo(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(path.strip()) and not path.startswith("/") and ".." not in parts


def _title_and_body(pr: PullRequest) -> tuple[str, str] | str:
    """The title and body in force from the head's committer date to the merge or close.

    Returns the reason for a skip instead when the history shows a change inside that window or
    cannot show the value. A failing `gh` stops the import: a skip must come from evidence, not
    from an outage.
    """
    owner, name = pr.nwo.split("/")
    pull = _gh_object(["api", f"repos/{pr.nwo}/pulls/{pr.number}"], pr.key)
    commit = _gh_object(["api", f"repos/{pr.nwo}/commits/{pr.head}"], pr.key)
    pages = _gh(
        ["api", "--paginate", "--slurp", f"repos/{pr.nwo}/issues/{pr.number}/timeline"], pr.key
    )
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
        pr.key,
    )
    try:
        start = _time(commit["commit"]["committer"]["date"])
        created = _time(pull["created_at"])
        end_text = pull["merged_at"] or pull["closed_at"]
        end = _time(end_text) if end_text is not None else None
        title = pull["title"]
        if not isinstance(title, str):
            raise TypeError("'title' is not a string")
    except (KeyError, TypeError, ValueError) as err:
        raise TaskImportError(f"{pr.key}: malformed reply from GitHub: {err!r}") from None
    renames = _renames(pages)
    if renames is None:
        return "title history is not readable"
    edited = _edit_times(edits)
    if edited is None:
        return "description edit history is not readable"

    def inside(when: datetime) -> bool:
        return start <= when and (end is None or when <= end)

    def after(when: datetime) -> bool:
        return end is not None and when > end

    if any(inside(when) for when, _, _ in renames):
        return "title changed inside the review window"
    for when, old, new in reversed(renames):
        if not after(when):
            break
        if new != title:
            return "title history does not lead to the current title"
        title = old
    # GitHub keeps the text the PR was opened with as its oldest revision; that is no change.
    changes = [when for when in edited if when > created]
    if any(inside(when) for when in changes):
        return "description changed inside the review window"
    if any(after(when) for when in changes):
        # Edit history does not give the earlier text reliably, so it is not rebuilt.
        return "description edited after the review window"
    if not title.strip():
        return "title is empty"
    return title, edits["data"]["repository"]["pullRequest"]["body"]


def _renames(pages: object) -> list[tuple[datetime, str, str]] | None:
    """The `renamed` events of a `gh api --paginate --slurp` timeline, oldest first.

    None when the reply is not a list of pages of event objects, or a rename is malformed.
    """
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        return None
    renames = []
    for event in (event for page in pages for event in page):
        if not (isinstance(event, dict) and isinstance(event.get("event"), str)):
            return None
        if event["event"] != "renamed":
            continue
        rename = event.get("rename")
        try:
            when = _time(event.get("created_at"))
        except (TypeError, ValueError):
            return None
        if not (
            isinstance(rename, dict)
            and isinstance(rename.get("from"), str)
            and isinstance(rename.get("to"), str)
        ):
            return None
        renames.append((when, rename["from"], rename["to"]))
    return sorted(renames, key=lambda rename: rename[0])


def _edit_times(reply: dict) -> list[datetime] | None:
    """When the PR body was edited, from the GraphQL reply; None when that is not fully readable."""
    try:
        node = reply["data"]["repository"]["pullRequest"]
        history = node["userContentEdits"]
        total, nodes = history["totalCount"], history["nodes"]
        if not isinstance(node["body"], str) or type(total) is not int or total > EDITS_PAGE:
            return None
        if not isinstance(nodes, list) or len(nodes) != total:
            return None
        return [_time(n["editedAt"]) for n in nodes]
    except (KeyError, TypeError, ValueError):
        return None


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("a timestamp is not a string")
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        raise ValueError("a timestamp has no time zone")
    return when


def _gh(args: list[str], label: str) -> object:
    """Run `gh` with `args` and parse its JSON reply; errors start with `label` (the PR)."""
    what = "gh api " + next(a for a in args[1:] if not a.startswith("-"))
    try:
        result = subprocess.run(
            ["gh", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
        )
    except OSError as err:
        raise TaskImportError(f"{label}: cannot run gh: {err}") from None
    if result.returncode != 0:
        raise TaskImportError(f"{label}: {what} failed: {result.stderr.strip()}")
    return _json(result.stdout.encode(), f"{label}: {what}")


def _gh_object(args: list[str], label: str) -> dict:
    reply = _gh(args, label)
    if not isinstance(reply, dict):
        raise TaskImportError(f"{label}: gh api did not return an object")
    return reply


def _json(data: bytes, label: str) -> object:
    error = None
    try:
        return json.loads(data, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as err:
        error = str(err)
    raise TaskImportError(f"{label}: not valid JSON: {error}")


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError("a key is repeated")
    return data


def _dumps(data: object) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
