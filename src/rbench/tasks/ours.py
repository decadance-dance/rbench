"""Mine the `ours` task set from merged PRs of public repositories (spec review-tasks; design D15).

For every repository, the PRs merged inside the window, opened inside it too and written by a
human, are taken in merge order until `max_per_repo` of them make tasks. A PR makes a task when:

- it changes at most `max_changed_lines` lines in at most `max_changed_files` files;
- the reviewed head is the commit of its first review by a human other than its author that has
  inline comments; a head missing from the PR's commits (a rewritten branch) skips the PR;
- at least one comment is acted on: a thread-opening comment by a human other than the author, on
  the new side of the diff at the reviewed head, whose lines a later non-merge commit, committed
  after the comment, changes (`_touches`). The lines are followed along the PR's first-parent
  path from the reviewed head, merges and pure renames included, and only a commit on that path
  is evidence; a PR whose path cannot be traced is skipped and named. Each such comment is one
  ground-truth item, with the comment and the commit as its source; a comment nobody acted on is
  not ground truth;
- its title and description were in force while the reviewed head was its head (design D1), and
  its repository's license allows redistribution (spec review-tasks, Source license). Failing
  either skips it, and the changelog names it. Both are checked before the PR takes a place.

The window and the cap per repository may only be narrowed, and a private repository is refused
before any PR is read.

Severity follows the rule in `pre_label` unless a label sheet filled by the maintainer says
otherwise: `write_label_sheet` writes one row per mined item, `import_ours(labels=...)` applies the
filled sheet, and with `require_labels` an unlabelled item refuses the import.

Every GitHub reply is untrusted data: it is validated, and a malformed one stops the import with
the PR it came from. PR text is data for the engines and the judge, never instructions to rbench.
"""

import argparse
import csv
import hashlib
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

from rbench.atomic import atomic_write
from rbench.tasks.archive import ArchiveError, archive_name, build_archive
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
from rbench.tasks.license import LicenseError, LicenseLookupError, read_license
from rbench.tasks.loader import (
    COMMIT,
    HIDDEN_ID_PREFIX,
    PUBLIC_CANARY_PREFIX,
    REPO,
    TASK_ID,
    TaskError,
)
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
DECORATIONS = re.compile(r"\(([^)\n]*)\)")
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
PAGE = 100
REVIEW_STATES = ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING")
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
    # A Conventional Comments decoration is one token of a comma list: `(blocking, security)`.
    tokens = {t.strip().lower() for d in DECORATIONS.findall(first) for t in d.split(",")}
    if "blocking" in tokens or review_state == "CHANGES_REQUESTED":
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


def _touches(patch: str, start: int, end: int, label: str) -> tuple[tuple[int, int] | None, bool]:
    """Follow lines `start`..`end` of a file through one commit's unified diff `patch`.

    Returns their new range and whether the commit changed them. The range is None when the
    commit deletes or replaces one of them. Inserting lines between them grows the range;
    inserting right after the last one keeps it; both change the lines.
    """
    shift = grow = 0
    changed = False
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
                return None, True
            shift -= old < start
            old += 1
        elif line.startswith("+"):
            # `old` is the line the insertion lands before.
            changed |= start < old <= end + 1
            grow += start < old <= end
            shift += old <= start
        else:
            old += 1
    return (start + shift, end + shift + grow), changed


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
        files, base_sha, tip = pull["changed_files"], pull["base"]["sha"], pull["head"]["sha"]
        if not (type(lines) is int and type(files) is int and COMMIT.fullmatch(base_sha)):
            raise TypeError("size or base is malformed")
        if not (isinstance(tip, str) and COMMIT.fullmatch(tip)):
            raise TypeError("head is malformed")
    except (KeyError, TypeError) as err:
        raise TaskImportError(f"{label}: malformed PR from GitHub: {err!r}") from None
    if author is None or lines > window.max_changed_lines or files > window.max_changed_files:
        return None

    reviews = [_review(r, label) for r in _pages([f"repos/{repo}/pulls/{number}/reviews"], label)]
    comments = [
        _comment(c, label) for c in _pages([f"repos/{repo}/pulls/{number}/comments"], label)
    ]
    commits = [_commit(c, label) for c in _pages([f"repos/{repo}/pulls/{number}/commits"], label)]
    inline = {c.review for c in comments}
    rounds = sorted(
        (r.submitted, r.commit)
        for r in reviews
        if r.id in inline
        and r.submitted is not None
        and r.commit is not None
        and _human(r.user) not in (None, author)
    )
    if not rounds:
        return None
    head = rounds[0][1]
    task_id = f"{repo.replace('/', '_')}_{number}-{head[:8]}".lower()
    if not TASK_ID.fullmatch(task_id) or task_id.startswith(HIDDEN_ID_PREFIX):
        raise TaskImportError(f"{label}: {task_id} is not a valid public task id")
    states = {r.id: r.state for r in reviews}
    open_ = {c.id: c for c in comments if c.candidate(head, author)}
    if not open_:
        return None
    if head not in {c.sha for c in commits}:
        # The branch was rewritten after the review: the follow-ups cannot be ordered.
        return task_id, "the reviewed head is not in the PR's commits"
    path = _first_parents(commits, head, tip)
    if path is None:
        return task_id, "the PR's head does not lead back to the reviewed head by first parents"
    tracked = {c.id: (c.path, c.start, c.end) for c in open_.values()}
    fixed: dict[int, str] = {}
    for later in path:
        if not tracked:
            break
        files = _commit_files(repo, later.sha, label)
        for cid, (name, start, end) in list(tracked.items()):
            if name not in files:
                continue
            new_name, status, patch, changes = files[name]
            if status == "removed":
                moved, touched = None, True
            elif patch is not None:
                moved, touched = _touches(patch, start, end, label)
            elif status == "renamed" and changes == 0:
                moved, touched = (start, end), False
            else:
                del tracked[cid]  # GitHub shows no diff (binary, too large): no evidence
                continue
            # A merge brings in the base branch, and a commit made before the comment cannot be
            # an answer to it: neither is evidence, and lines they leave in place stay followed.
            if touched and not later.merge and later.when > open_[cid].created:
                fixed[cid] = later.sha
                del tracked[cid]
            elif moved is None:
                del tracked[cid]
            else:
                tracked[cid] = (new_name, *moved)
    if not fixed:
        return None
    items = tuple(
        Item(
            f"{task_id}#{cid}",
            open_[cid].path,
            open_[cid].start,
            open_[cid].end,
            open_[cid].body,
            pre_label(open_[cid].body, states.get(open_[cid].review)),
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


@dataclass(frozen=True)
class Review:
    id: int
    user: object
    state: str
    submitted: datetime | None
    commit: str | None


@dataclass(frozen=True)
class Comment:
    id: int
    review: int | None
    reply: bool
    user: object
    commit: str
    kind: str
    side: object
    start_side: object
    created: datetime
    body: str
    path: str | None
    start: int | None
    end: int | None

    def candidate(self, head: str, author: str) -> bool:
        """A thread-opening human comment, not the author's, on new lines at the head."""
        return (
            not self.reply
            and self.commit == head
            and _human(self.user) not in (None, author)
            and self.kind == "line"
            and self.side == "RIGHT"
            and self.start_side in (None, "RIGHT")
            and bool(self.body.strip())
        )


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    when: datetime

    @property
    def merge(self) -> bool:
        return len(self.parents) > 1


def _first_parents(commits: list[Commit], head: str, tip: str) -> list[Commit] | None:
    """The commits from `head` (excluded) to the PR's head `tip`, by first parents.

    GitHub gives a commit's diff, a merge's too, against its first parent, so only this path
    carries the reviewed lines; a commit off it never touched them. None when the PR's commits
    do not lead from `tip` back to `head`.
    """
    by_sha = {c.sha: c for c in commits}
    path = []
    sha = tip
    while sha != head:
        commit = by_sha.pop(sha, None)  # popped, so malformed data cannot loop
        if commit is None or not commit.parents:
            return None
        path.append(commit)
        sha = commit.parents[0]
    return path[::-1]


def _review(raw: object, label: str) -> Review:
    rid = raw.get("id") if isinstance(raw, dict) else None
    if type(rid) is not int:
        raise TaskImportError(f"{label}: review without an id")
    where = f"{label}: review {rid}"
    state, submitted, commit = raw.get("state"), raw.get("submitted_at"), raw.get("commit_id")
    if state not in REVIEW_STATES:
        raise TaskImportError(f"{where}: malformed state")
    if commit is not None and not (isinstance(commit, str) and COMMIT.fullmatch(commit)):
        raise TaskImportError(f"{where}: malformed commit id")
    when = None if submitted is None else _when(submitted, where)
    return Review(rid, raw.get("user"), state, when, commit)


def _comment(raw: object, label: str) -> Comment:
    """A validated review comment; the fields an item needs are checked on every line comment."""
    cid = raw.get("id") if isinstance(raw, dict) else None
    if type(cid) is not int:
        raise TaskImportError(f"{label}: review comment without an id")
    where = f"{label}: review comment {cid}"
    review, reply = raw.get("pull_request_review_id"), raw.get("in_reply_to_id")
    if review is not None and type(review) is not int:
        raise TaskImportError(f"{where}: malformed review id")
    if reply is not None and type(reply) is not int:
        raise TaskImportError(f"{where}: malformed reply id")
    commit, kind, body = (
        raw.get("original_commit_id"),
        raw.get("subject_type", "line"),
        raw.get("body"),
    )
    if not (isinstance(commit, str) and COMMIT.fullmatch(commit)):
        raise TaskImportError(f"{where}: malformed commit id")
    if not (isinstance(kind, str) and isinstance(body, str)):
        raise TaskImportError(f"{where}: malformed subject type or body")
    try:
        created = _time(raw.get("created_at"))
    except (TypeError, ValueError):
        raise TaskImportError(f"{where}: malformed timestamp") from None
    path = start = end = None
    if kind == "line":
        path, end, first = raw.get("path"), raw.get("original_line"), raw.get("original_start_line")
        start = end if first is None else first
        if not (isinstance(path, str) and path.strip() and not path.startswith("/")):
            raise TaskImportError(f"{where}: malformed path")
        if ".." in PurePosixPath(path).parts:
            raise TaskImportError(f"{where}: malformed path")
        if not (type(start) is int and type(end) is int and 1 <= start <= end):
            raise TaskImportError(f"{where}: malformed line range")
    return Comment(
        cid,
        review,
        reply is not None,
        raw.get("user"),
        commit,
        kind,
        raw.get("side"),
        raw.get("start_side"),
        created,
        body,
        path,
        start,
        end,
    )


def _commit(raw: object, label: str) -> Commit:
    try:
        sha, parents = raw["sha"], tuple(p["sha"] for p in raw["parents"])
        when = _time(raw["commit"]["committer"]["date"])
        if not (isinstance(sha, str) and COMMIT.fullmatch(sha)):
            raise TypeError("sha")
        if not all(isinstance(p, str) and COMMIT.fullmatch(p) for p in parents):
            raise TypeError("parents")
    except (KeyError, TypeError, ValueError) as err:
        raise TaskImportError(f"{label}: malformed commit from GitHub: {err!r}") from None
    return Commit(sha, parents, when)


def _commit_files(repo: str, sha: str, label: str) -> dict[str, tuple[str, str, str | None, int]]:
    """The files one commit changes, every page: old path -> (new path, status, patch, changes).

    A merge commit's files are its changes against its first parent, the previous commit on the
    PR's first-parent path.
    """
    pages = _gh(["api", "--paginate", "--slurp", f"repos/{repo}/commits/{sha}"], label)
    if not (isinstance(pages, list) and all(isinstance(p, dict) for p in pages)):
        raise TaskImportError(f"{label}: malformed commit {sha} from GitHub")
    changed = {}
    for page in pages:
        files = page.get("files")
        if not isinstance(files, list):
            raise TaskImportError(f"{label}: malformed commit {sha} from GitHub")
        for entry in files:
            try:
                name, status, changes = entry["filename"], entry["status"], entry["changes"]
                old = entry.get("previous_filename", name)
                patch = entry.get("patch")
                if not (isinstance(name, str) and isinstance(old, str) and isinstance(status, str)):
                    raise TypeError("filename or status is not a string")
                if patch is not None and not isinstance(patch, str):
                    raise TypeError("patch is not a string")
                if type(changes) is not int or changes < 0:
                    raise TypeError("changes is not a count")
            except (KeyError, TypeError) as err:
                raise TaskImportError(f"{label}: malformed commit {sha}: {err!r}") from None
            changed[old] = (name, status, patch, changes)
    return changed


def _language(repo: str) -> str:
    """The primary language of a public `repo`; a private one is refused before anything else."""
    reply = _gh_object(["api", f"repos/{repo}"], repo)
    if reply.get("private") is not False:
        raise TaskImportError(f"{repo} is not public; only public repositories are mined")
    language = reply.get("language")
    if not (isinstance(language, str) and language.strip()):
        raise TaskImportError(f"{repo}: GitHub reports no language")
    return language


def mine(window: Window) -> tuple[list[Mined], dict[str, str]]:
    """The PRs that make tasks, up to `max_per_repo` per repository, and the skipped ones.

    A PR spends a repository's slot only once it passed every check: filters, items, D1's title
    and description rule and the license at its reviewed head.
    """
    _check_window(window)
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
                try:
                    read_license(repo, result.pr.head)
                except LicenseLookupError:
                    raise
                except LicenseError as err:
                    skipped[result.task_id] = f"license refused: {err}"
                    continue
                found.append(result)
                taken += 1
    return found, skipped


def _check_window(window: Window) -> None:
    """Refuse a window that reaches outside the approved one, or a malformed one."""
    for repo in window.repos:
        if not REPO.fullmatch(repo):
            raise TaskImportError(f"{repo!r} is not an owner/name repository")
    if not MERGED_AFTER <= window.merged_after < window.merged_before <= MERGED_BEFORE:
        raise TaskImportError(
            f"the window {window.merged_after} to {window.merged_before} must lie inside"
            f" {MERGED_AFTER} to {MERGED_BEFORE}, after the matrix's training cutoffs (design D15)"
        )
    limits = (window.max_per_repo, window.max_changed_lines, window.max_changed_files)
    if any(type(n) is not int or n < 1 for n in limits):
        raise TaskImportError("the window's limits must be positive integers")
    if window.max_per_repo > MAX_PER_REPO:
        raise TaskImportError(
            f"max_per_repo {window.max_per_repo} is above the cap of {MAX_PER_REPO} tasks per"
            " repository (design D15); it may only narrow it"
        )


def write_label_sheet(path: Path, window: Window = Window()) -> int:
    """Mine and write the label sheet: one row per item; return the number of rows.

    The sheet is working data for the maintainer: a path git tracks is refused, and every cell
    built from GitHub text is written so a spreadsheet shows it as text, not a formula.
    """
    path = Path(path)
    _refuse_tracked(path)
    found, _ = mine(window)
    rows = [
        (
            m.task_id,
            item.id,
            _text(f"{item.file}:{item.start_line}-{item.end_line}"),
            _text(item.link),
            LABELS[item.pre_label],
            "",
            _text(" ".join(item.message.split())[:EXCERPT]),
        )
        for m in found
        for item in m.items
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(SHEET_COLUMNS)
        writer.writerows(rows)
    os.replace(tmp, path)
    return len(rows)


def _text(cell: str) -> str:
    """A cell a spreadsheet keeps as text: a leading formula character gets a quote first."""
    return f"'{cell}" if cell[:1] in ("=", "+", "-", "@", "\t", "\r") else cell


def _refuse_tracked(path: Path) -> None:
    """Refuse a label-sheet path that a git work tree tracks; the sheet is never committed."""
    folder = path.parent if path.parent.is_dir() else None
    if folder is None:
        return  # a new directory holds no tracked file
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    found = subprocess.run(
        ["git", "-C", str(folder), "ls-files", "--error-unmatch", "--", path.name],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if found.returncode == 0:
        raise TaskImportError(f"label sheet {path} is tracked by git; write it outside the repo")


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
    """Mine `window` into the set `set_dir`; archives go to `archive_dir/<sha256>.bundle`.

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

    token = uuid.uuid4().hex
    stage = set_dir.parent / f".{set_dir.name}.import-{token}"
    archive_stage = archive_dir / f".import-{token}"
    stage.mkdir(parents=True)
    archive_stage.mkdir(parents=True)
    try:
        entries, bundles, labelled = {}, {}, []
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
            labelled += [bool((labels or {}).get(item.id)) for item in m.items]
            task = _write_task(
                stage,
                SET,
                m.task_id,
                pr,
                m.texts,
                archive.sha256,
                items,
                set_dir,
                _canary(m.task_id),
            )
            entries[m.task_id] = manifest_task(task, archive)
            bundles[m.task_id] = archive
        # Where the imported items' severities came from, not which options were given.
        if labelled and all(labelled):
            how = "maintainer"
        else:
            how = "rule+maintainer" if any(labelled) else "rule"
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
        note = (
            f"Mined from {len(window.repos)} repositories, PRs opened and merged from"
            f" {source.merged_after} to before {source.merged_before} (design D15);"
            f" severity by {how}."
        )
        latest = {t: h for e in (old.changelog if old else ()) for t, h in e.changed.items()}
        # A skip the changelog does not name yet is news, even when no task changed.
        news = any(latest.get(task_id, "") is not None for task_id in skipped)
        changelog, version = _changelog(
            old,
            source,
            entries,
            skipped,
            note,
            "the window holds no PR that makes a task",
            force=news,
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
        for archive in bundles.values():
            os.replace(archive.path, archive_dir / archive_name(archive.sha256))
        if changelog is not None:
            _swap(set_dir, stage)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(archive_stage, ignore_errors=True)
    return ImportResult(version, changelog is not None, tuple(sorted(entries)), skipped)


def _canary(task_id: str) -> str:
    """A new task's canary, derived from its id so a fresh import is byte-reproducible.

    It is unique because task ids are; the `ours` ground truth is public anyway, so a canary that
    anyone can derive gives nothing away.
    """
    digest = hashlib.sha256(f"rbench-ours-canary:{task_id}".encode()).hexdigest()
    return PUBLIC_CANARY_PREFIX + digest[:32]


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
