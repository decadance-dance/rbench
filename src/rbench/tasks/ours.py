"""Mine the `ours` task set from merged PRs of public repositories (spec review-tasks; design D15).

For every repository, the PRs merged inside the window, opened inside it too and written by a
human, are taken in merge order until `max_per_repo` of them make tasks. A PR makes a task when:

- it changes at most `max_changed_lines` lines in at most `max_changed_files` files;
- the reviewed head is the commit of its first review by a human other than its author that has
  inline comments, and that commit is one of the PR's commits;
- at least one of that review round's comments is acted on: a thread-opening comment by a human
  other than the author, on the new side of the diff at the reviewed head, whose lines a later
  commit of the PR changes (`_touches`). Each such comment is one ground-truth item, with the
  comment and the commit as its source; a comment nobody acted on is not ground truth;
- its title and description were in force while the reviewed head was its head (design D1), and
  its repository's license allows redistribution (spec review-tasks, Source license). Failing
  either skips it, and the changelog names it.

Severity follows the rule in `pre_label` unless a label sheet filled by the maintainer says
otherwise: `write_label_sheet` writes one row per mined item, `import_ours(labels=...)` applies the
filled sheet, and with `require_labels` an unlabelled item refuses the import.

Every GitHub reply is untrusted data: it is validated, and a malformed one stops the import with
the PR it came from. PR text is data for the engines and the judge, never instructions to rbench.
"""

import argparse
import csv
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

from rbench.atomic import atomic_write
from rbench.tasks.archive import ArchiveError, build_archive
from rbench.tasks.importing import (
    ImportResult,
    PullRequest,
    TaskImportError,
    _changelog,
    _dumps,
    _gh,
    _gh_object,
    _lock,
    _recover,
    _swap,
    _time,
    _title_and_body,
    _write_task,
)
from rbench.tasks.license import LicenseError, LicenseLookupError
from rbench.tasks.loader import COMMIT, HIDDEN_ID_PREFIX, REPO, TASK_ID, TaskError
from rbench.tasks.manifest import (
    MANIFEST_FILE,
    MinedSource,
    check_version_history,
    load_task_set,
    manifest_task,
)

SET = "ours"
# The latest training cutoff of the matrix's models is June 2026, so only PRs opened and merged
# from July on; the end is fixed so the set can be mined again (design D15).
MERGED_AFTER = date(2026, 7, 1)
MERGED_BEFORE = date(2026, 11, 20)
MAX_PER_REPO = 3
MAX_CHANGED_LINES = 1500
MAX_CHANGED_FILES = 30
SOURCE_BASE = "https://github.com/"
# Active, permissively licensed, with a measured human review culture (design D15).
REPOS = (
    "psf/black",
    "pytest-dev/pytest",
    "pypa/pip",
    "pallets/click",
    "python-attrs/attrs",
    "django/django",
    "scikit-learn/scikit-learn",
    "caddyserver/caddy",
    "prometheus/prometheus",
    "etcd-io/bbolt",
    "charmbracelet/bubbletea",
    "urfave/cli",
    "tokio-rs/axum",
    "clap-rs/clap",
    "nushell/nushell",
    "expressjs/express",
    "fastify/fastify",
)

SEVERITY_MAP = {"M": "must", "S": "should", "N": "nit"}
LABELS = {severity: label for label, severity in SEVERITY_MAP.items()}
SEVERITY_RULE = {
    "nit": "the comment opens with nit, nitpick, minor, optional, style or typo, as a word or"
    " a Conventional Comments label",
    "must": "not nit, and the comment's first line has a (blocking) decoration or its review"
    " requested changes",
    "should": "every other item",
}
NIT = re.compile(r"[\s*_>`]*(?:nits?|nitpick|minor|optional|style|typo)\b", re.IGNORECASE)
BLOCKING = re.compile(r"\([^)\n]*\bblocking\b[^)\n]*\)", re.IGNORECASE)
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
PAGE = 100
EXCERPT = 200
SHEET_COLUMNS = ("task_id", "item_id", "location", "link", "pre_label", "label", "excerpt")


@dataclass(frozen=True)
class Item:
    """One mined ground-truth item before its severity is final."""

    id: str
    file: str
    start_line: int
    end_line: int
    message: str
    pre_label: str
    link: str
    fixed_by: str

    def ground_truth(self, severity: str) -> dict:
        return {
            "id": self.id,
            "file": self.file,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "message": self.message,
            "category": category(self.file),
            "severity": severity,
            "producer": "human",
            "source": f"{self.link} fixed-by {self.fixed_by}",
        }


@dataclass(frozen=True)
class Mined:
    """A PR that makes a task: its id, the PR at the reviewed head, its texts and its items."""

    task_id: str
    pr: PullRequest
    texts: tuple[str, str]
    items: tuple[Item, ...]


@dataclass(frozen=True)
class Window:
    repos: tuple[str, ...] = REPOS
    merged_after: date = MERGED_AFTER
    merged_before: date = MERGED_BEFORE
    max_per_repo: int = MAX_PER_REPO
    max_changed_lines: int = MAX_CHANGED_LINES
    max_changed_files: int = MAX_CHANGED_FILES


def pre_label(body: str, review_state: str | None) -> str:
    """The rule's severity of a comment (design D15): nit, must or should."""
    first = body.lstrip().split("\n", 1)[0]
    if NIT.match(body):
        return "nit"
    if BLOCKING.search(first) or review_state == "CHANGES_REQUESTED":
        return "must"
    return "should"


def category(path: str) -> str:
    """The item's category from its path alone; no metric uses it (spec review-scoring)."""
    pure = PurePosixPath(path)
    name, parts = pure.name.lower(), [p.lower() for p in pure.parts[:-1]]
    if (
        {"test", "tests", "testing", "__tests__", "spec"} & set(parts)
        or name.startswith("test_")
        or re.search(r"(_test\.go|_test\.py|\.test\.[jt]sx?|\.spec\.[jt]sx?)$", name)
    ):
        return "testing"
    if {"doc", "docs"} & set(parts) or pure.suffix.lower() in (".md", ".rst", ".txt"):
        return "documentation"
    return "other"


def _touches(patch: str, start: int, end: int, label: str) -> tuple[int, int] | None:
    """Follow lines `start`..`end` of a file through one commit's unified diff `patch`.

    None when the commit deletes or replaces one of them, or inserts lines between them or right
    after the last one; otherwise their new numbers.
    """
    shift = 0
    old = None
    for line in patch.split("\n"):
        if line.startswith("@@"):
            hunk = HUNK.match(line)
            if hunk is None:
                raise TaskImportError(f"{label}: malformed hunk header in a commit's diff")
            first, length = int(hunk[1]), hunk[2]
            # A hunk that removes nothing (`-a,0`) inserts after line a.
            old = first + 1 if length == "0" else first
        elif old is None or line.startswith("\\"):
            continue
        elif line.startswith("-"):
            if start <= old <= end:
                return None
            shift -= old < start
            old += 1
        elif line.startswith("+"):
            # `old` is the line the insertion lands before.
            if start < old <= end + 1:
                return None
            shift += old <= start
        else:
            old += 1
    return start + shift, end + shift


def _human(user: object) -> str | None:
    """The login of a human GitHub user, or None for a bot or a malformed user."""
    if not isinstance(user, dict) or user.get("type") != "User":
        return None
    login = user.get("login")
    if not isinstance(login, str) or not login or login.endswith("[bot]"):
        return None
    return login


def _pages(args: list[str], label: str) -> list:
    """Every item of a paginated list endpoint (`gh api --paginate --slurp`)."""
    pages = _gh(["api", "--paginate", "--slurp", *args], label)
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise TaskImportError(f"{label}: gh api did not return pages of lists")
    return [entry for page in pages for entry in page]


def _when(value: object, label: str) -> datetime:
    try:
        return _time(value)
    except (TypeError, ValueError) as err:
        raise TaskImportError(f"{label}: malformed timestamp from GitHub: {err}") from None


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _candidates(repo: str, window: Window) -> list[int]:
    """PRs of `repo` opened and merged inside the window by a human, in merge order."""
    after, before = _midnight(window.merged_after), _midnight(window.merged_before)
    found = []
    page = 1
    while True:
        args = ["api", f"repos/{repo}/pulls?state=closed&sort=created&direction=desc"]
        args[1] += f"&per_page={PAGE}&page={page}"
        pulls = _gh(args, repo)
        if not isinstance(pulls, list):
            raise TaskImportError(f"{repo}: gh api did not return a list of PRs")
        for pull in pulls:
            if not isinstance(pull, dict) or type(pull.get("number")) is not int:
                raise TaskImportError(f"{repo}: malformed PR in the list from GitHub")
            label = f"{repo}#{pull['number']}"
            created = _when(pull.get("created_at"), label)
            merged = pull.get("merged_at")
            if created < after or merged is None or _human(pull.get("user")) is None:
                continue
            merged = _when(merged, label)
            if merged < before:
                found.append((merged, pull["number"]))
        oldest = _when(pulls[-1].get("created_at"), repo) if pulls else None
        if len(pulls) < PAGE or oldest is None or oldest < after:
            return [number for _, number in sorted(found)]
        page += 1


def _mine(repo: str, number: int, language: str, window: Window) -> Mined | tuple[str, str] | None:
    """The task PR `repo#number` makes; (task id, reason) for a skip; None when it makes none."""
    label = f"{repo}#{number}"
    pull = _gh_object(["api", f"repos/{repo}/pulls/{number}"], label)
    try:
        author = _human(pull["user"])
        lines = pull["additions"] + pull["deletions"]
        files, base_sha = pull["changed_files"], pull["base"]["sha"]
        if not (type(lines) is int and type(files) is int and COMMIT.fullmatch(base_sha)):
            raise TypeError("size or base is malformed")
    except (KeyError, TypeError) as err:
        raise TaskImportError(f"{label}: malformed PR from GitHub: {err!r}") from None
    if author is None or lines > window.max_changed_lines or files > window.max_changed_files:
        return None

    reviews = _pages([f"repos/{repo}/pulls/{number}/reviews"], label)
    comments = _pages([f"repos/{repo}/pulls/{number}/comments"], label)
    commits = _pages([f"repos/{repo}/pulls/{number}/commits"], label)
    try:
        states = {r["id"]: r["state"] for r in reviews}
        inline = {c["pull_request_review_id"] for c in comments}
        rounds = sorted(
            (_when(r["submitted_at"], label), r["commit_id"])
            for r in reviews
            if r["id"] in inline and _human(r["user"]) not in (None, author) and r["submitted_at"]
        )
        shas = [c["sha"] for c in commits]
        merges = [len(c["parents"]) > 1 for c in commits]
        if not all(isinstance(sha, str) and COMMIT.fullmatch(sha) for sha in shas):
            raise TypeError("a commit id is malformed")
    except (KeyError, TypeError) as err:
        raise TaskImportError(f"{label}: malformed review data from GitHub: {err!r}") from None
    if not rounds or rounds[0][1] not in shas:
        return None
    head = rounds[0][1]

    task_id = f"{repo.replace('/', '_')}_{number}-{head[:8]}".lower()
    if not TASK_ID.fullmatch(task_id) or task_id.startswith(HIDDEN_ID_PREFIX):
        raise TaskImportError(f"{label}: {task_id} is not a valid public task id")
    open_ = {}
    for comment in comments:
        found = _comment(comment, head, author, label)
        if found is not None:
            cid, path, start, end, body, review = found
            open_[cid] = (path, start, end, body, pre_label(body, states.get(review)))
    tracked = {cid: (path, start, end) for cid, (path, start, end, *_) in open_.items()}
    fixed: dict[int, str] = {}
    for sha, merge in list(zip(shas, merges))[shas.index(head) + 1 :]:
        if merge or not tracked:
            break  # a merge brings in the base branch: its changes are nobody's fix
        changed = _commit_files(repo, sha, label)
        for cid, (path, start, end) in list(tracked.items()):
            if path not in changed:
                continue
            new_path, status, patch = changed[path]
            if status != "removed" and patch is None:
                del tracked[cid]  # GitHub shows no diff (binary, too large): no evidence
                continue
            moved = None if status == "removed" else _touches(patch or "", start, end, label)
            if moved is None:
                fixed[cid] = sha
                del tracked[cid]
            else:
                tracked[cid] = (new_path, *moved)
    if not fixed:
        return None
    items = tuple(
        Item(
            f"{task_id}#{cid}",
            open_[cid][0],
            open_[cid][1],
            open_[cid][2],
            open_[cid][3],
            open_[cid][4],
            f"https://github.com/{repo}/pull/{number}#discussion_r{cid}",
            fixed[cid],
        )
        for cid in sorted(fixed)
    )
    pr = PullRequest(label, repo, number, base_sha, head, language)
    texts = _title_and_body(pr)
    if isinstance(texts, str):
        return task_id, texts
    compare = _gh_object(["api", f"repos/{repo}/compare/{base_sha}...{head}"], label)
    try:
        base = compare["merge_base_commit"]["sha"]
    except (KeyError, TypeError) as err:
        raise TaskImportError(f"{label}: malformed compare reply from GitHub: {err!r}") from None
    if not (isinstance(base, str) and COMMIT.fullmatch(base)):
        raise TaskImportError(f"{label}: malformed merge base from GitHub")
    return Mined(task_id, replace(pr, base=base), texts, items)


def _comment(comment: object, head: str, author: str, label: str) -> tuple | None:
    """(id, path, start, end, body, review id) of a comment that can become an item, else None."""
    if not isinstance(comment, dict):
        raise TaskImportError(f"{label}: malformed review comment from GitHub")
    cid = comment.get("id")
    where = f"{label}: review comment {cid}"
    if type(cid) is not int:
        raise TaskImportError(f"{label}: review comment without an id")
    if (
        "in_reply_to_id" in comment
        or comment.get("original_commit_id") != head
        or _human(comment.get("user")) in (None, author)
        or comment.get("subject_type", "line") != "line"
        or comment.get("side") != "RIGHT"
        or comment.get("start_side") not in (None, "RIGHT")
    ):
        return None
    path, end, body = comment.get("path"), comment.get("original_line"), comment.get("body")
    start = comment.get("original_start_line") or end
    if not (isinstance(path, str) and path.strip() and not path.startswith("/")):
        raise TaskImportError(f"{where}: malformed path")
    if ".." in PurePosixPath(path).parts:
        raise TaskImportError(f"{where}: malformed path")
    if not (type(start) is int and type(end) is int and 1 <= start <= end):
        raise TaskImportError(f"{where}: malformed line range")
    if not (isinstance(body, str) and body.strip()):
        return None
    return cid, path, start, end, body, comment.get("pull_request_review_id")


def _commit_files(repo: str, sha: str, label: str) -> dict[str, tuple[str, str, str | None]]:
    """The files one commit changes: old path -> (new path, status, patch or None)."""
    reply = _gh_object(["api", f"repos/{repo}/commits/{sha}"], label)
    files = reply.get("files")
    if not isinstance(files, list):
        raise TaskImportError(f"{label}: malformed commit {sha} from GitHub")
    changed = {}
    for entry in files:
        try:
            name, status = entry["filename"], entry["status"]
            old = entry.get("previous_filename", name)
            patch = entry.get("patch")
            if not (isinstance(name, str) and isinstance(old, str) and isinstance(status, str)):
                raise TypeError("filename or status is not a string")
            if patch is not None and not isinstance(patch, str):
                raise TypeError("patch is not a string")
        except (KeyError, TypeError) as err:
            raise TaskImportError(f"{label}: malformed commit {sha}: {err!r}") from None
        changed[old] = (name, status, patch)
    return changed


def _language(repo: str) -> str:
    reply = _gh_object(["api", f"repos/{repo}"], repo)
    language = reply.get("language")
    if not (isinstance(language, str) and language.strip()):
        raise TaskImportError(f"{repo}: GitHub reports no language")
    return language


def mine(window: Window) -> tuple[list[Mined], dict[str, str]]:
    """The PRs that make tasks, up to `max_per_repo` per repository, and the skipped ones."""
    for repo in window.repos:
        if not REPO.fullmatch(repo):
            raise TaskImportError(f"{repo!r} is not an owner/name repository")
    found, skipped = [], {}
    for repo in window.repos:
        language, taken = _language(repo), 0
        for number in _candidates(repo, window):
            if taken == window.max_per_repo:
                break
            result = _mine(repo, number, language, window)
            if isinstance(result, tuple):
                skipped[result[0]] = result[1]
            elif result is not None:
                found.append(result)
                taken += 1
    return found, skipped


def write_label_sheet(path: Path, window: Window = Window()) -> int:
    """Mine and write the label sheet: one row per item; return the number of rows."""
    found, _ = mine(window)
    rows = [
        (
            m.task_id,
            item.id,
            f"{item.file}:{item.start_line}-{item.end_line}",
            item.link,
            LABELS[item.pre_label],
            "",
            " ".join(item.message.split())[:EXCERPT],
        )
        for m in found
        for item in m.items
    ]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(SHEET_COLUMNS)
        writer.writerows(rows)
    os.replace(tmp, path)
    return len(rows)


def read_labels(path: Path) -> dict[str, str]:
    """The filled label sheet: item id -> severity, for every row with a label."""
    labels = {}
    with Path(path).open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not {"item_id", "label"} <= set(reader.fieldnames or ()):
            raise TaskImportError(f"label sheet {path}: needs the columns item_id and label")
        for row, entry in enumerate(reader, start=2):
            item, label = (entry["item_id"] or "").strip(), (entry["label"] or "").strip()
            if not item:
                raise TaskImportError(f"label sheet {path}: row {row} has no item_id")
            if item in labels:
                raise TaskImportError(f"label sheet {path}: item {item} is listed twice")
            if label and label.upper() not in SEVERITY_MAP:
                raise TaskImportError(
                    f"label sheet {path}: item {item} has label {label!r}, not M, S or N"
                )
            labels[item] = SEVERITY_MAP[label.upper()] if label else ""
    return labels


def import_ours(
    set_dir: Path,
    archive_dir: Path,
    window: Window = Window(),
    labels: dict[str, str] | None = None,
    require_labels: bool = False,
    source_base: str = SOURCE_BASE,
) -> ImportResult:
    """Mine `window` into the set `set_dir`; archives go to `archive_dir/<task-id>-<sha256>.bundle`.

    `labels` (from `read_labels`) set the severity of the items they name; with `require_labels`
    every imported item needs one. A run that changes nothing writes no set file; any change
    makes a new version whose changelog names every imported, skipped or removed task.
    """
    set_dir, archive_dir = Path(set_dir), Path(archive_dir)
    sets, archives = set_dir.resolve(), archive_dir.resolve()
    if sets == archives or sets in archives.parents or archives in sets.parents:
        raise TaskImportError(
            f"the archive directory {archive_dir} and the set directory {set_dir} overlap;"
            " the swap of a new version would move or delete the archives"
        )
    with _lock(set_dir):
        return _import(set_dir, archive_dir, window, labels, require_labels, source_base)


def _severities(found: list[Mined], labels: dict[str, str] | None, required: bool) -> dict:
    labels = labels or {}
    mined = {item.id: item for m in found for item in m.items}
    if unknown := sorted(labels.keys() - mined.keys()):
        raise TaskImportError(f"the label sheet names items this import did not mine: {unknown}")
    if required and (missing := sorted(i for i in mined if not labels.get(i))):
        raise TaskImportError(f"items without a maintainer label: {', '.join(missing)}")
    return {i: labels.get(i) or item.pre_label for i, item in mined.items()}


def _import(
    set_dir: Path,
    archive_dir: Path,
    window: Window,
    labels: dict[str, str] | None,
    require_labels: bool,
    source_base: str,
) -> ImportResult:
    _recover(set_dir)
    for stale in archive_dir.glob(".import-*"):
        shutil.rmtree(stale)  # archives a killed import staged; none is in use
    old = load_task_set(set_dir, SET)[0] if (set_dir / MANIFEST_FILE).exists() else None
    found, skipped = mine(window)
    severity = _severities(found, labels, require_labels)
    if require_labels:
        how = "maintainer"
    elif labels and any(labels.values()):
        how = "rule+maintainer"
    else:
        how = "rule"
    filters = {
        "max_per_repo": window.max_per_repo,
        "max_changed_lines": window.max_changed_lines,
        "max_changed_files": window.max_changed_files,
    }
    source = MinedSource(
        window.repos,
        window.merged_after.isoformat(),
        window.merged_before.isoformat(),
        filters,
        SEVERITY_MAP,
        SEVERITY_RULE,
        how,
    )

    token = uuid.uuid4().hex
    stage = set_dir.parent / f".{set_dir.name}.import-{token}"
    archive_stage = archive_dir / f".import-{token}"
    stage.mkdir(parents=True)
    archive_stage.mkdir(parents=True)
    try:
        entries, bundles = {}, {}
        for m in found:
            pr = m.pr
            out = archive_stage / f"{m.task_id}.bundle"
            try:
                archive = build_archive(
                    pr.nwo, f"{source_base}{pr.nwo}", pr.base, pr.head, out, SET
                )
            except LicenseLookupError:
                raise
            except LicenseError as err:
                skipped[m.task_id] = f"license refused: {err}"
                continue
            items = [item.ground_truth(severity[item.id]) for item in m.items]
            task = _write_task(stage, SET, m.task_id, pr, m.texts, archive.sha256, items, set_dir)
            entries[m.task_id] = manifest_task(task, archive)
            bundles[m.task_id] = archive
        note = (
            f"Mined from {len(window.repos)} repositories, PRs opened and merged from"
            f" {source.merged_after} to before {source.merged_before} (design D15);"
            f" severity by {how}."
        )
        changelog, version = _changelog(
            old, source, entries, skipped, note, "the window holds no PR that makes a task"
        )
        if changelog is not None:
            atomic_write(
                stage / MANIFEST_FILE, _dumps(_manifest(source, version, entries, changelog))
            )
            try:
                new = load_task_set(stage, SET)[0]
                check_version_history(old, new)
            except TaskError as err:
                raise TaskImportError(f"the new version does not validate: {err}") from None
        for task_id, archive in bundles.items():
            os.replace(archive.path, archive_dir / f"{task_id}-{archive.sha256}.bundle")
        if changelog is not None:
            _swap(set_dir, stage)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(archive_stage, ignore_errors=True)
    return ImportResult(version, changelog is not None, tuple(sorted(entries)), skipped)


def _manifest(source: MinedSource, version: int, entries: dict, changelog: list) -> dict:
    return {
        "set": SET,
        "version": version,
        "source": {
            "repos": list(source.repos),
            "merged_after": source.merged_after,
            "merged_before": source.merged_before,
            "filters": source.filters,
            "severity_map": source.severity_map,
            "severity_rule": source.severity_rule,
            "severity_labels": source.severity_labels,
        },
        "tasks": {
            task_id: {"sha256": entry.sha256, "license": entry.license}
            for task_id, entry in entries.items()
        },
        "changelog": changelog,
    }


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The arguments of `rbench tasks import ours`."""
    parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="a repository to mine, owner/name; repeat it (default: the list in design D15)",
    )
    parser.add_argument("--merged-after", type=date.fromisoformat, default=MERGED_AFTER)
    parser.add_argument("--merged-before", type=date.fromisoformat, default=MERGED_BEFORE)
    parser.add_argument("--max-per-repo", type=int, default=MAX_PER_REPO)
    parser.add_argument(
        "--tasks", type=Path, default=Path("tasks") / SET, help="the set directory to write"
    )
    parser.add_argument("--archives", type=Path, help="where to write the source archives")
    parser.add_argument(
        "--label-sheet",
        type=Path,
        help="only mine, and write the items to this CSV file for the maintainer to label",
    )
    parser.add_argument("--labels", type=Path, help="a filled label sheet to apply")
    parser.add_argument(
        "--require-labels",
        action="store_true",
        help="refuse the import while an item has no maintainer label",
    )
    parser.add_argument(
        "--source-base", default=SOURCE_BASE, help="prefix of the source repositories' URLs"
    )


def run(args: argparse.Namespace) -> int:
    """Run `rbench tasks import ours`; print what changed, or the error, and return the code."""
    window = Window(
        tuple(args.repos) if args.repos else REPOS,
        args.merged_after,
        args.merged_before,
        args.max_per_repo,
    )
    try:
        if args.label_sheet is not None:
            rows = write_label_sheet(args.label_sheet, window)
            print(f"{SET}: wrote {rows} items to {args.label_sheet}")
            return 0
        if args.archives is None:
            raise TaskImportError("--archives is required to import")
        labels = read_labels(args.labels) if args.labels else None
        result = import_ours(
            args.tasks, args.archives, window, labels, args.require_labels, args.source_base
        )
    except (TaskImportError, TaskError, ArchiveError, LicenseError, OSError) as err:
        print(f"rbench tasks import: {err}", file=sys.stderr)
        return 1
    state = "new version" if result.changed else "unchanged, version"
    print(f"{SET}: {state} {result.version}, {len(result.imported)} tasks")
    for task_id, reason in sorted(result.skipped.items()):
        print(f"skipped {task_id}: {reason}")
    return 0
