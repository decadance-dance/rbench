"""The `ours` import (spec review-tasks: Recorded origin of ground truth for our PRs, Source
license, Source archive, Task set versions; design D15).

The source repository and GitHub are synthetic: a git repository built in tmp_path and the fake
`gh`, which answers each API path from its `replies` (the first whose `match` is in the argv).
Follow-up commits after the reviewed head exist only in the fake's replies: the archive needs just
the base and the reviewed head.
"""

import csv
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest
from conftest import FakeBin
from test_archive import MIT, commit, git, license_reply

from rbench.cli import main
from rbench.tasks import MANIFEST_FILE, load_task_set
from rbench.tasks.importing import TaskImportError
from rbench.tasks.manifest import MinedSource
from rbench.tasks.ours import (
    SEVERITY_MAP,
    SEVERITY_RULE,
    Window,
    _touches,
    category,
    import_ours,
    pre_label,
    read_labels,
    write_label_sheet,
)

NWO = "acme/widget"
API = f"repos/{NWO}"
OPENED = "2026-08-01T10:00:00Z"
HEAD_DATE = "2026-08-01T09:00:00Z"
COMMENTED = "2026-08-02T09:30:00Z"
REVIEWED = "2026-08-02T10:00:00Z"
FIXED = "2026-08-02T11:00:00Z"
MERGED = "2026-08-03T10:00:00Z"
INSIDE = "2026-08-02T12:00:00Z"
TEN = "".join(f"line {n}\n" for n in range(1, 11))
LINE_5 = "@@ -5,1 +5,1 @@\n-line 5\n+line five\n"
FIX = "f" * 40
MERGE = "e" * 40
SIBLING = "c" * 40
EARLY = "a" * 40


def user(login: str, kind: str = "User") -> dict:
    return {"login": login, "type": kind}


def comment(cid: int, line: int, body: str, who: str = "rev", **kw) -> dict:
    return {
        "id": cid,
        "pull_request_review_id": 100,
        "user": user(who, "Bot" if who.endswith("[bot]") else "User"),
        "path": "app.py",
        "original_line": line,
        "original_start_line": None,
        "side": "RIGHT",
        "start_side": None,
        "subject_type": "line",
        "body": body,
        "created_at": COMMENTED,
        **kw,
    }


def changed(patch: str | None = LINE_5, path: str = "app.py", **kw) -> dict:
    """One `files` entry of GitHub's commit reply."""
    return {"filename": path, "status": "modified", "patch": patch, "changes": 2, **kw}


def followup(
    sha: str = FIX,
    *files: dict,
    merge: bool = False,
    when: str = FIXED,
    pages=None,
    parents: list[str] | None = None,
) -> dict:
    """A commit after the reviewed head: its id, its files by page and its parents.

    By default its first parent is the commit listed before it, and a merge's second is the base.
    """
    return {
        "sha": sha,
        "pages": pages or [list(files)],
        "merge": merge,
        "when": when,
        "parents": parents,
    }


@dataclass
class PR:
    number: int = 7
    title: str = "Fix the widget"
    body: str = "Fixes it."
    renames: list = field(default_factory=list)
    created: str = OPENED
    merged: str | None = MERGED
    author: dict = field(default_factory=lambda: user("dev"))
    review_state: object = "COMMENTED"
    license: str = "MIT"
    comments: list = field(default_factory=list)
    followups: list = field(default_factory=lambda: [followup(FIX, changed())])
    in_history: bool = True  # False: the PR's commit list no longer has the reviewed head
    head: str = ""
    tip: str | None = None  # the PR's head commit; by default its last listed commit

    @property
    def task_id(self) -> str:
        return f"acme_widget_{self.number}-{self.head[:8]}"

    def replies(self, base: str) -> dict:
        pull = {
            "number": self.number,
            "user": self.author,
            "created_at": self.created,
            "merged_at": self.merged,
            "closed_at": self.merged or MERGED,
            "title": self.title,
            "additions": 2,
            "deletions": 1,
            "changed_files": 1,
            "base": {"sha": base},
        }
        reviews = [
            {
                "id": 100,
                "user": user("rev"),
                "state": self.review_state,
                "submitted_at": REVIEWED,
                "commit_id": self.head,
            }
        ]
        for c in self.comments:
            c.setdefault("original_commit_id", self.head)
        commits = [
            {
                "sha": self.head if self.in_history else "d" * 40,
                "parents": [{"sha": base}],
                "commit": {"committer": {"date": HEAD_DATE}},
            }
        ]
        for f in self.followups:
            parents = f["parents"] or [commits[-1]["sha"], *([base] if f["merge"] else [])]
            commits.append(
                {
                    "sha": f["sha"],
                    "parents": [{"sha": p} for p in parents],
                    "commit": {"committer": {"date": f["when"]}},
                }
            )
        pull["head"] = {"sha": self.tip or commits[-1]["sha"]}
        edits = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "body": self.body,
                        "userContentEdits": {"totalCount": 0, "nodes": []},
                    }
                }
            }
        }
        events = [
            {"event": "renamed", "created_at": t, "rename": {"from": a, "to": b}}
            for t, a, b in self.renames
        ]
        n = self.number
        return {
            f"{API}/license?ref={self.head}": license_reply(self.license),
            f"{API}/pulls/{n}/reviews": json.dumps([reviews]),
            f"{API}/pulls/{n}/comments": json.dumps([self.comments]),
            f"{API}/pulls/{n}/commits": json.dumps([commits]),
            f"{API}/pulls/{n}": json.dumps(pull),
            f"{API}/commits/{self.head}": json.dumps(
                {"commit": {"committer": {"date": HEAD_DATE}}}
            ),
            **{
                f"{API}/commits/{f['sha']}": json.dumps([{"files": p} for p in f["pages"]])
                for f in self.followups
            },
            f"{API}/issues/{n}/timeline": json.dumps([events]),
            f"number={n}": json.dumps(edits),
        }

    def listed(self) -> dict:
        return {
            "number": self.number,
            "user": self.author,
            "created_at": self.created,
            "merged_at": self.merged,
        }


@dataclass
class World:
    tmp: Path
    fake: FakeBin
    prs: list[PR] = field(default_factory=lambda: [PR()])
    private: object = False
    base: str = ""

    @property
    def pr(self) -> PR:
        return self.prs[-1]

    @property
    def repo(self) -> Path:
        return self.tmp / "src" / NWO

    @property
    def set_dir(self) -> Path:
        return self.tmp / "tasks" / "ours"

    @property
    def archives(self) -> Path:
        return self.tmp / "archives"

    def build(self) -> "World":
        self.repo.mkdir(parents=True)
        git(self.repo, "init", "-q", "-b", "main")
        self.base = commit(self.repo, {"LICENSE": MIT, "app.py": TEN}, "base")
        for pr in self.prs:
            self.add_head(pr)
        self.answer()
        return self

    def add_head(self, pr: PR) -> None:
        git(self.repo, "checkout", "-q", "-b", f"pr-{pr.number}", self.base)
        text = TEN.replace("line 3\n", f"line three of {pr.number}\n")
        pr.head = commit(self.repo, {"app.py": text}, f"pr {pr.number}")
        if not pr.comments:
            pr.comments = [
                comment(1, 5, "nit: name this five"),  # line 5 is changed: an item
                comment(2, 9, "Is line 9 right?"),  # never changed: not an item
                comment(3, 5, "bot says so", who="lint[bot]"),
                comment(4, 5, "replying", in_reply_to_id=1),
                comment(5, 5, "my own note", who="dev"),
                comment(6, 5, "the old line was odd", side="LEFT"),
            ]

    def answer(self, **override) -> None:
        replies = {}
        for pr in self.prs:
            replies |= pr.replies(self.base)
        replies |= {
            f"{API}/pulls?state=closed": json.dumps([pr.listed() for pr in self.prs]),
            f"{API}/compare/": json.dumps({"merge_base_commit": {"sha": self.base}}),
            API: json.dumps({"language": "Python", "private": self.private}),
            **override,
        }
        self.fake.scenario("gh", replies=[{"match": k, "stdout": v} for k, v in replies.items()])

    def run(self, set_dir: Path | None = None, window: Window | None = None, **kw):
        return import_ours(
            set_dir or self.set_dir,
            kw.pop("archives", self.archives),
            window or Window(repos=(NWO,)),
            source_base=f"{self.tmp / 'src'}/",
            **kw,
        )

    def items(self, pr: PR | None = None) -> list[dict]:
        path = self.set_dir / (pr or self.pr).task_id / "ground_truth.json"
        return json.loads(path.read_text())["items"]

    def manifest(self) -> dict:
        return json.loads((self.set_dir / MANIFEST_FILE).read_text())


@pytest.fixture
def w(tmp_path: Path, fake_bin: FakeBin) -> World:
    return World(tmp_path, fake_bin).build()


def no_task(w: World) -> None:
    with pytest.raises(TaskImportError, match="the window holds no PR that makes a task"):
        w.run()


def write_rows(sheet: Path, rows: list[dict]) -> None:
    with sheet.open("w", newline="") as f:
        writer = csv.DictWriter(f, rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def test_acted_on_human_comments_become_items_with_their_source(w: World) -> None:
    result = w.run()

    pr = w.pr
    assert result.imported == (pr.task_id,) and result.skipped == {}
    assert w.items() == [
        {
            "id": f"{pr.task_id}#1",
            "file": "app.py",
            "start_line": 5,
            "end_line": 5,
            "message": "nit: name this five",
            "category": "other",
            "severity": "nit",
            "producer": "human",
            "source": f"https://github.com/{NWO}/pull/7#discussion_r1 fixed-by {FIX}",
        }
    ]
    manifest, tasks = load_task_set(w.set_dir, "ours")
    task = tasks[0]
    assert (task.base, task.head, task.title, task.language) == (
        w.base,
        pr.head,
        "Fix the widget",
        "Python",
    )
    assert manifest.tasks[pr.task_id].license == "MIT"
    assert isinstance(manifest.source, MinedSource) and manifest.source.repos == (NWO,)
    data = w.manifest()
    assert data["source"]["merged_after"] == "2026-07-01"
    assert data["source"]["severity_map"] == SEVERITY_MAP
    assert data["source"]["severity_rule"] == SEVERITY_RULE
    assert data["source"]["severity_labels"] == "rule"
    assert list(w.archives.glob(f"{pr.task_id}-*.bundle"))


def test_same_window_again_writes_nothing(w: World) -> None:
    w.run()
    before = {p: p.read_bytes() for p in w.set_dir.rglob("*") if p.is_file()}

    result = w.run()

    assert not result.changed and result.version == 1
    assert {p: p.read_bytes() for p in w.set_dir.rglob("*") if p.is_file()} == before


def test_fresh_imports_are_byte_identical(w: World) -> None:
    one, two = w.tmp / "one" / "ours", w.tmp / "two" / "ours"
    w.run(set_dir=one, archives=w.tmp / "a1")
    w.run(set_dir=two, archives=w.tmp / "a2")

    files = sorted(p.relative_to(one) for p in one.rglob("*") if p.is_file())
    assert files and files == sorted(p.relative_to(two) for p in two.rglob("*") if p.is_file())
    assert all((one / f).read_bytes() == (two / f).read_bytes() for f in files)


@pytest.mark.parametrize(
    "change",
    [
        {"created": "2026-06-30T23:00:00Z"},  # opened before the cutoff
        {"merged": "2026-11-20T00:00:00Z"},  # merged at the window's end
        {"merged": None},  # closed without a merge
        {"author": user("dependabot[bot]", "Bot")},
    ],
)
def test_prs_outside_the_window_or_by_bots_make_no_task(w: World, change: dict) -> None:
    for key, value in change.items():
        setattr(w.pr, key, value)
    w.answer()
    no_task(w)


@pytest.mark.parametrize(
    "window",
    [
        Window(repos=(NWO,), merged_after=date(2026, 6, 1)),
        Window(repos=(NWO,), merged_before=date(2026, 12, 1)),
        Window(repos=(NWO,), merged_after=date(2026, 9, 1), merged_before=date(2026, 9, 1)),
    ],
)
def test_window_cannot_reach_past_the_cutoff(w: World, window: Window) -> None:
    w.pr.created, w.pr.merged = "2026-06-28T10:00:00Z", "2026-06-29T10:00:00Z"
    w.answer()

    with pytest.raises(TaskImportError, match="window"):
        w.run(window=window)
    assert w.fake.calls() == []


def test_narrower_window_is_allowed(w: World) -> None:
    window = Window(repos=(NWO,), merged_after=date(2026, 7, 15), merged_before=date(2026, 9, 1))
    assert w.run(window=window).imported == (w.pr.task_id,)


def test_private_repository_is_refused_before_any_pr_is_read(w: World, tmp_path: Path) -> None:
    w.private = True
    w.answer()

    with pytest.raises(TaskImportError, match="acme/widget is not public"):
        write_label_sheet(tmp_path / "sheet.csv", Window(repos=(NWO,)))
    assert [c["argv"] for c in w.fake.calls()] == [["api", API]]
    assert not (tmp_path / "sheet.csv").exists()


def test_comment_on_a_later_commit_is_not_an_item(w: World) -> None:
    w.pr.comments = [comment(1, 5, "rename", original_commit_id=FIX)]
    w.answer()
    no_task(w)


def test_merge_commit_is_never_the_evidence(w: World) -> None:
    w.pr.followups = [followup(MERGE, changed(), merge=True)]
    w.answer()
    no_task(w)


def test_lines_are_followed_across_a_merge_to_a_later_fix(w: World) -> None:
    other = changed("@@ -1,1 +1,1 @@\n-a\n+b\n", path="other.py")
    shifted = changed("@@ -1,0 +2,2 @@\n+x\n+y\n")  # the merge moves line 5 to 7
    w.pr.followups = [
        followup(MERGE, other, shifted, merge=True),
        followup(FIX, changed("@@ -7,1 +7,1 @@\n-line 5\n+line five\n")),
    ]
    w.answer()

    w.run()

    assert [(i["id"], i["source"].split()[-1]) for i in w.items()] == [(f"{w.pr.task_id}#1", FIX)]


def test_commit_off_the_first_parent_path_is_never_evidence(w: World) -> None:
    # The review's repro: a sibling branch from the base edits its own line 6, which the reviewed
    # head moved to 7; the PR merges it, and GitHub's merge patch is against the merge's first
    # parent, so line 6 at the reviewed head stays until the real fix.
    w.pr.comments = [comment(1, 6, "check this")]
    w.pr.followups = [
        followup(SIBLING, changed("@@ -6,1 +6,1 @@\n-line 6\n+six\n"), parents=[w.base]),
        followup(MERGE, changed("@@ -7,1 +7,1 @@\n-line 6\n+six\n"), parents=[w.pr.head, SIBLING]),
        followup(FIX, changed("@@ -6,1 +6,1 @@\n-line 5\n+line five\n")),
    ]
    w.answer()

    w.run()

    assert [i["source"].split()[-1] for i in w.items()] == [FIX]
    assert not [c for c in w.fake.calls() if f"commits/{SIBLING}" in " ".join(c["argv"])]


@pytest.mark.parametrize(
    "change",
    [
        {"followups": [followup(FIX, changed(), parents=["0" * 40])]},  # not back to the head
        {"tip": "9" * 40},  # the PR's head is not among its commits
    ],
)
def test_unreachable_first_parent_path_is_a_named_skip(w: World, change: dict) -> None:
    for key, value in change.items():
        setattr(w.pr, key, value)
    w.answer()

    result = w.run()

    reason = "the PR's head does not lead back to the reviewed head by first parents"
    assert result.skipped == {w.pr.task_id: reason}


@pytest.mark.parametrize(
    "first",
    [
        followup(MERGE, changed("@@ -5,0 +6,1 @@\n+x\n"), merge=True),
        followup(EARLY, changed("@@ -5,0 +6,1 @@\n+x\n"), when="2026-08-02T09:00:00Z"),
    ],
)
def test_insertion_right_after_the_lines_keeps_them_followed(w: World, first: dict) -> None:
    # Neither a merge nor a commit made before the comment is evidence, so the line it leaves
    # intact is still followed, and the later fix is credited.
    w.pr.followups = [first, followup(FIX, changed())]
    w.answer()

    w.run()

    assert [i["source"].split()[-1] for i in w.items()] == [FIX]


def test_max_per_repo_may_only_narrow_the_cap(w: World, capsys) -> None:
    with pytest.raises(TaskImportError, match="max_per_repo 4 is above the cap of 3"):
        w.run(window=Window(repos=(NWO,), max_per_repo=4))
    assert w.fake.calls() == []

    argv = ["tasks", "import", "ours", "--repo", NWO, "--tasks", str(w.set_dir)]
    assert main([*argv, "--archives", str(w.archives), "--max-per-repo", "4"]) == 1
    assert "above the cap of 3" in capsys.readouterr().err


def test_lines_are_followed_through_a_pure_rename(w: World) -> None:
    rename = changed(None, path="moved.py", status="renamed", previous_filename="app.py", changes=0)
    w.pr.followups = [followup("a" * 40, rename), followup(FIX, changed(path="moved.py"))]
    w.answer()

    w.run()

    assert [i["file"] for i in w.items()] == ["app.py"]


def test_commit_files_on_a_later_page_count(w: World) -> None:
    other = changed("@@ -1,1 +1,1 @@\n-a\n+b\n", path="x")
    w.pr.followups = [followup(FIX, pages=[[other], [changed()]])]
    w.answer()

    w.run()

    assert [i["id"] for i in w.items()] == [f"{w.pr.task_id}#1"]
    calls = [c["argv"] for c in w.fake.calls() if f"commits/{FIX}" in " ".join(c["argv"])]
    assert calls == [["api", "--paginate", "--slurp", f"{API}/commits/{FIX}"]]


def test_change_made_before_the_comment_is_not_its_evidence(w: World) -> None:
    w.pr.comments = [comment(1, 5, "rename this", created_at="2026-08-02T12:00:00Z")]
    w.answer()
    no_task(w)


def test_rewritten_history_is_a_named_skip(w: World) -> None:
    w.pr.in_history = False
    w.answer()

    result = w.run()

    assert result.skipped == {w.pr.task_id: "the reviewed head is not in the PR's commits"}
    assert w.manifest()["changelog"][0]["changed"] == {w.pr.task_id: None}


def test_title_changed_inside_the_window_skips_and_names_the_task(w: World) -> None:
    w.pr.renames = [(INSIDE, "Old title", w.pr.title)]
    w.answer()

    result = w.run()

    assert result.imported == ()
    assert result.skipped == {w.pr.task_id: "title changed inside the review window"}
    log = w.manifest()["changelog"]
    assert log[0]["changed"] == {w.pr.task_id: None}
    assert f"{w.pr.task_id} (title changed inside the review window)" in log[0]["note"]


def test_new_skip_makes_a_new_version_naming_it(w: World) -> None:
    w.run()
    earlier = PR(number=6, created="2026-07-30T10:00:00Z")
    earlier.renames = [(INSIDE, "Old title", earlier.title)]
    w.add_head(earlier)
    w.prs.insert(0, earlier)
    w.answer()

    result = w.run()

    assert result.changed and result.version == 2
    assert w.manifest()["changelog"][1]["changed"][earlier.task_id] is None
    assert not w.run().changed  # the same skip again is not news


def test_license_refusal_does_not_spend_the_repository_slot(w: World, tmp_path: Path) -> None:
    refused = PR(number=6, license="Unlicense", created="2026-07-30T10:00:00Z")
    w.add_head(refused)
    w.prs.insert(0, refused)
    w.answer()
    window = Window(repos=(NWO,), max_per_repo=1)
    sheet = tmp_path / "sheet.csv"

    write_label_sheet(sheet, window)

    rows = list(csv.DictReader(sheet.open()))
    assert {r["task_id"] for r in rows} == {w.pr.task_id}
    for row in rows:
        row["label"] = "S"
    write_rows(sheet, rows)
    result = w.run(window=window, labels=read_labels(sheet), require_labels=True)
    assert result.imported == (w.pr.task_id,)
    assert result.skipped[refused.task_id].startswith("license refused")


def test_changes_requested_review_makes_must(w: World) -> None:
    w.pr.comments = [comment(1, 5, "This breaks callers.")]
    w.pr.review_state = "CHANGES_REQUESTED"
    w.answer()

    w.run()

    assert [i["severity"] for i in w.items()] == ["must"]


def test_label_sheet_round_trip(w: World, tmp_path: Path) -> None:
    w.pr.comments = [comment(1, 9, "nit:\n rename"), comment(2, 4, "the loop above is wrong")]
    w.pr.followups = [followup(FIX, changed("@@ -3,3 +3,4 @@\n x\n-line 4\n+four\n+4b\n y\n"))]
    w.answer()
    sheet = tmp_path / "sheet.csv"

    assert write_label_sheet(sheet, Window(repos=(NWO,))) == 1
    rows = list(csv.DictReader(sheet.open()))
    assert rows[0]["item_id"] == f"{w.pr.task_id}#2"
    assert rows[0]["pre_label"] == "S" and rows[0]["label"] == ""
    assert rows[0]["location"] == "app.py:4-4"
    assert not w.set_dir.exists()  # a sheet run imports nothing

    with pytest.raises(TaskImportError, match=f"without a maintainer label: {w.pr.task_id}#2"):
        w.run(labels=read_labels(sheet), require_labels=True)

    rows[0]["label"] = "m"
    write_rows(sheet, rows)
    w.run(labels=read_labels(sheet), require_labels=True)

    assert [i["severity"] for i in w.items()] == ["must"]
    assert w.manifest()["source"]["severity_labels"] == "maintainer"


def test_severity_source_follows_the_labels_applied(w: World, tmp_path: Path) -> None:
    sheet = tmp_path / "sheet.csv"
    write_label_sheet(sheet, Window(repos=(NWO,)))
    rows = list(csv.DictReader(sheet.open()))
    rows[0]["label"] = "S"
    write_rows(sheet, rows)

    w.run(labels=read_labels(sheet))

    assert w.manifest()["source"]["severity_labels"] == "maintainer"


def test_label_sheet_escapes_formulas_and_refuses_tracked_paths(w: World, tmp_path: Path) -> None:
    w.pr.comments = [comment(1, 5, "=HYPERLINK(1)", path="@x.py")]
    w.pr.followups = [followup(FIX, changed(path="@x.py"))]
    w.answer()
    sheet = tmp_path / "sheet.csv"

    write_label_sheet(sheet, Window(repos=(NWO,)))

    (row,) = csv.DictReader(sheet.open())
    assert row["excerpt"] == "'=HYPERLINK(1)" and row["location"] == "'@x.py:5-5"
    assert read_labels(sheet) == {f"{w.pr.task_id}#1": ""}

    tracked = w.repo / "sheet.csv"
    commit(w.repo, {"sheet.csv": "kept\n"}, "tracked file")
    with pytest.raises(TaskImportError, match="is tracked by git"):
        write_label_sheet(tracked, Window(repos=(NWO,)))
    assert tracked.read_text() == "kept\n"


def test_label_sheet_rejects_unknown_items_and_bad_labels(w: World, tmp_path: Path) -> None:
    sheet = tmp_path / "sheet.csv"
    sheet.write_text("item_id,label\nacme_widget_7-00000000#9,S\n")
    with pytest.raises(TaskImportError, match="did not mine"):
        w.run(labels=read_labels(sheet))

    sheet.write_text("item_id,label\nx#1,Q\n")
    with pytest.raises(TaskImportError, match="label 'Q', not M, S or N"):
        read_labels(sheet)
    sheet.write_text("item_id,label\nx#1,S\nx#1,N\n")
    with pytest.raises(TaskImportError, match="listed twice"):
        read_labels(sheet)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"original_line": "5"}, "review comment 1: malformed line range"),
        ({"original_start_line": 0}, "review comment 1: malformed line range"),
        ({"pull_request_review_id": "100"}, "review comment 1: malformed review id"),
        ({"created_at": "yesterday"}, "review comment 1: malformed timestamp"),
    ],
)
def test_malformed_comment_refuses_the_import(w: World, change: dict, message: str) -> None:
    w.pr.comments = [comment(1, 5, "x", **change)]
    w.answer()

    with pytest.raises(TaskImportError, match=f"acme/widget#7: {message}"):
        w.run()


def test_malformed_review_refuses_the_import(w: World) -> None:
    w.pr.review_state = []
    w.answer()

    with pytest.raises(TaskImportError, match="acme/widget#7: review 100: malformed state"):
        w.run()


def test_cli_imports_and_reports_failures(w: World, capsys) -> None:
    argv = [
        "tasks",
        "import",
        "ours",
        "--repo",
        NWO,
        "--tasks",
        str(w.set_dir),
        "--archives",
        str(w.archives),
        "--source-base",
        f"{w.tmp / 'src'}/",
    ]

    assert main(argv) == 0
    assert "ours: new version 1, 1 tasks" in capsys.readouterr().out
    assert main([*argv, "--merged-after", "2026-06-01"]) == 1
    assert "window" in capsys.readouterr().err
    w.fake.scenario("gh", stderr="HTTP 500", exit=1)
    assert main(argv) == 1
    assert "gh api repos/acme/widget failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ("@@ -5,1 +5,1 @@\n-a\n+b\n", (None, True)),  # replaces the line
        ("@@ -4,3 +4,2 @@\n x\n-y\n z\n", (None, True)),  # deletes it
        ("@@ -6,0 +7,1 @@\n+new\n", ((5, 6), True)),  # inserts right after: kept, changed
        ("@@ -5,0 +6,1 @@\n+new\n", ((5, 7), True)),  # inserts between: it grows, changed
        ("@@ -1,1 +1,3 @@\n a\n+b\n+c\n", ((7, 8), False)),  # inserts above: the range moves down
        ("@@ -1,2 +1,1 @@\n-a\n b\n", ((4, 5), False)),  # deletes above: it moves up
        ("@@ -4,1 +4,2 @@\n x\n+new\n", ((6, 7), False)),  # inserts right above: it moves down
        ("@@ -3,5 +3,5 @@\n a\n b\n c\n d\n e\n", ((5, 6), False)),  # context only
        ("@@ -9,1 +9,1 @@\n-a\n+b\n", ((5, 6), False)),  # below the range
    ],
)
def test_touches_follows_lines_through_a_diff(patch: str, expected) -> None:
    assert _touches(patch, 5, 6, "t") == expected


@pytest.mark.parametrize(
    ("body", "state", "expected"),
    [
        ("nit: spacing", "CHANGES_REQUESTED", "nit"),
        ("**Nitpick** rename", None, "nit"),
        ("Optional: inline this", None, "nit"),
        ("issue (blocking): this leaks", None, "must"),
        ("issue (blocking, security): this leaks", None, "must"),
        ("suggestion (non-blocking): use a clearer name", "COMMENTED", "should"),
        ("question (if-minor, not-blocking): why?", None, "should"),
        ("This leaks the handle.", "CHANGES_REQUESTED", "must"),
        ("Nice, but this leaks.", "COMMENTED", "should"),
        ("Why not reuse the helper?\n(blocking) later lines do not count", None, "should"),
    ],
)
def test_pre_label_rule(body: str, state, expected: str) -> None:
    assert pre_label(body, state) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_app.py", "testing"),
        ("pkg/server_test.go", "testing"),
        ("src/app.spec.ts", "testing"),
        ("docs/guide.rst", "documentation"),
        ("README.md", "documentation"),
        ("src/app.py", "other"),
    ],
)
def test_category_from_path(path: str, expected: str) -> None:
    assert category(path) == expected


def test_window_defaults_follow_the_cutoffs() -> None:
    window = Window()
    assert window.merged_after == date(2026, 7, 1) and window.merged_before == date(2026, 11, 20)
