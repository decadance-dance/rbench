"""The `ours` import (spec review-tasks: Recorded origin of ground truth for our PRs, Source
license, Source archive, Task set versions; design D15).

The source repository and GitHub are synthetic: a git repository built in tmp_path and the fake
`gh`, which answers each API path from its `replies` (the first whose `match` is in the argv).
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
OPENED = "2026-08-01T10:00:00Z"
HEAD_DATE = "2026-08-01T09:00:00Z"
REVIEWED = "2026-08-02T10:00:00Z"
MERGED = "2026-08-03T10:00:00Z"
INSIDE = "2026-08-02T12:00:00Z"
TEN = "".join(f"line {n}\n" for n in range(1, 11))


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
        **kw,
    }


def diff(repo: Path, old: str, new: str, path: str = "app.py") -> str:
    """The patch GitHub shows for `path` between two commits: the hunks only."""
    text = git(repo, "diff", "-U3", old, new, "--", path)
    return text[text.index("@@") :]


@dataclass
class World:
    tmp: Path
    fake: FakeBin
    number: int = 7
    title: str = "Fix the widget"
    body: str = "Fixes it."
    renames: list = field(default_factory=list)
    created: str = OPENED
    merged: str | None = MERGED
    author: dict = field(default_factory=lambda: user("dev"))
    review_state: str = "COMMENTED"
    merge_before_fix: bool = False
    comments: list = field(default_factory=list)
    base: str = ""
    head: str = ""
    fix: str = ""
    patch: str = ""

    @property
    def repo(self) -> Path:
        return self.tmp / "src" / NWO

    @property
    def set_dir(self) -> Path:
        return self.tmp / "tasks" / "ours"

    @property
    def archives(self) -> Path:
        return self.tmp / "archives"

    @property
    def task_id(self) -> str:
        return f"acme_widget_{self.number}-{self.head[:8]}"

    def build(self) -> "World":
        self.repo.mkdir(parents=True)
        git(self.repo, "init", "-q", "-b", "main")
        self.base = commit(self.repo, {"LICENSE": MIT, "app.py": TEN}, "base")
        git(self.repo, "checkout", "-q", "-b", "feature")
        self.head = commit(self.repo, {"app.py": TEN.replace("line 3\n", "line three\n")}, "pr")
        fixed = TEN.replace("line 3\n", "line three\n").replace("line 5\n", "line five\n")
        self.fix = commit(self.repo, {"app.py": fixed}, "address review")
        self.patch = diff(self.repo, self.head, self.fix)
        if not self.comments:
            self.comments = [
                comment(1, 5, "nit: name this five"),  # line 5 is changed: an item
                comment(2, 9, "Is line 9 right?"),  # never changed: not an item
                comment(3, 5, "bot says so", who="lint[bot]"),
                comment(4, 5, "replying", in_reply_to_id=1),
                comment(5, 5, "my own note", who="dev"),
            ]
        self.answer()
        return self

    def answer(self, **override) -> None:
        api = f"repos/{NWO}"
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
            "base": {"sha": self.base},
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
        parents = [{"sha": self.head}]
        commits = [{"sha": self.head, "parents": [{"sha": self.base}]}]
        if self.merge_before_fix:
            commits.append({"sha": "e" * 40, "parents": [{"sha": self.head}, {"sha": self.base}]})
        commits.append({"sha": self.fix, "parents": parents})
        events = [
            {"event": "renamed", "created_at": t, "rename": {"from": a, "to": b}}
            for t, a, b in self.renames
        ]
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
        for c in self.comments:
            c.setdefault("original_commit_id", self.head)
        files = {"files": [{"filename": "app.py", "status": "modified", "patch": self.patch}]}
        replies = {
            f"{api}/license?ref=": license_reply("MIT"),
            f"{api}/pulls?state=closed": json.dumps([pull]),
            f"{api}/pulls/{self.number}/reviews": json.dumps([reviews]),
            f"{api}/pulls/{self.number}/comments": json.dumps([self.comments]),
            f"{api}/pulls/{self.number}/commits": json.dumps([commits]),
            f"{api}/pulls/{self.number}": json.dumps(pull),
            f"{api}/commits/{self.head}": json.dumps(
                {"commit": {"committer": {"date": HEAD_DATE}}}
            ),
            f"{api}/commits/{self.fix}": json.dumps(files),
            f"{api}/issues/{self.number}/timeline": json.dumps([events]),
            f"{api}/compare/": json.dumps({"merge_base_commit": {"sha": self.base}}),
            "name=widget": json.dumps(edits),
            api: json.dumps({"language": "Python"}),
            **override,
        }
        self.fake.scenario("gh", replies=[{"match": k, "stdout": v} for k, v in replies.items()])

    def run(self, **kw):
        return import_ours(
            self.set_dir,
            self.archives,
            Window(repos=(NWO,)),
            source_base=f"{self.tmp / 'src'}/",
            **kw,
        )

    def items(self) -> list[dict]:
        path = self.set_dir / self.task_id / "ground_truth.json"
        return json.loads(path.read_text())["items"]


@pytest.fixture
def w(tmp_path: Path, fake_bin: FakeBin) -> World:
    return World(tmp_path, fake_bin).build()


def test_acted_on_human_comments_become_items_with_their_source(w: World) -> None:
    result = w.run()

    assert result.imported == (w.task_id,) and result.skipped == {}
    assert w.items() == [
        {
            "id": f"{w.task_id}#1",
            "file": "app.py",
            "start_line": 5,
            "end_line": 5,
            "message": "nit: name this five",
            "category": "other",
            "severity": "nit",
            "producer": "human",
            "source": f"https://github.com/{NWO}/pull/7#discussion_r1 fixed-by {w.fix}",
        }
    ]
    manifest, tasks = load_task_set(w.set_dir, "ours")
    task = tasks[0]
    assert (task.base, task.head, task.title, task.language) == (
        w.base,
        w.head,
        "Fix the widget",
        "Python",
    )
    assert manifest.tasks[w.task_id].license == "MIT"
    assert isinstance(manifest.source, MinedSource) and manifest.source.repos == (NWO,)
    data = json.loads((w.set_dir / MANIFEST_FILE).read_text())
    assert data["source"]["merged_after"] == "2026-07-01"
    assert data["source"]["severity_map"] == SEVERITY_MAP
    assert data["source"]["severity_rule"] == SEVERITY_RULE
    assert data["source"]["severity_labels"] == "rule"
    assert list((w.archives).glob(f"{w.task_id}-*.bundle"))


def test_same_window_again_writes_nothing(w: World) -> None:
    w.run()
    before = {p: p.read_bytes() for p in w.set_dir.rglob("*") if p.is_file()}

    result = w.run()

    assert not result.changed and result.version == 1
    assert {p: p.read_bytes() for p in w.set_dir.rglob("*") if p.is_file()} == before


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
        setattr(w, key, value)
    w.answer()

    with pytest.raises(TaskImportError, match="the window holds no PR that makes a task"):
        w.run()


def test_comment_on_a_later_commit_is_not_an_item(w: World) -> None:
    w.comments = [comment(1, 5, "rename", original_commit_id=w.fix)]
    w.answer()

    with pytest.raises(TaskImportError, match="no PR that makes a task"):
        w.run()


def test_merge_commit_before_the_fix_ends_the_evidence(w: World) -> None:
    w.merge_before_fix = True
    w.answer()

    with pytest.raises(TaskImportError, match="no PR that makes a task"):
        w.run()


def test_title_changed_inside_the_window_skips_and_names_the_task(w: World) -> None:
    w.renames = [(INSIDE, "Old title", w.title)]
    w.answer()

    result = w.run()

    assert result.imported == ()
    assert result.skipped == {w.task_id: "title changed inside the review window"}
    log = json.loads((w.set_dir / MANIFEST_FILE).read_text())["changelog"]
    assert log[0]["changed"] == {w.task_id: None}
    assert f"{w.task_id} (title changed inside the review window)" in log[0]["note"]


def test_changes_requested_review_makes_must(w: World) -> None:
    w.comments = [comment(1, 5, "This breaks callers.")]
    w.review_state = "CHANGES_REQUESTED"
    w.answer()

    w.run()

    assert [i["severity"] for i in w.items()] == ["must"]


def test_label_sheet_round_trip(w: World, tmp_path: Path) -> None:
    w.comments = [comment(1, 9, "nit:\n rename"), comment(2, 4, "the loop above is wrong")]
    w.patch = "@@ -3,3 +3,4 @@\n line three\n-line 4\n+line four\n+line 4b\n line 5\n"
    w.answer()
    sheet = tmp_path / "sheet.csv"

    assert write_label_sheet(sheet, Window(repos=(NWO,))) == 1
    rows = list(csv.DictReader(sheet.open()))
    assert rows[0]["item_id"] == f"{w.task_id}#2"
    assert rows[0]["pre_label"] == "S" and rows[0]["label"] == ""
    assert rows[0]["location"] == "app.py:4-4"
    assert not w.set_dir.exists()  # a sheet run imports nothing

    with pytest.raises(TaskImportError, match=f"without a maintainer label: {w.task_id}#2"):
        w.run(labels=read_labels(sheet), require_labels=True)

    rows[0]["label"] = "m"
    with sheet.open("w", newline="") as f:
        writer = csv.DictWriter(f, rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    w.run(labels=read_labels(sheet), require_labels=True)

    assert [i["severity"] for i in w.items()] == ["must"]
    assert json.loads((w.set_dir / MANIFEST_FILE).read_text())["source"]["severity_labels"] == (
        "maintainer"
    )


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


def test_malformed_comment_refuses_the_import(w: World) -> None:
    w.comments = [comment(1, 5, "x", original_line="5")]
    w.answer()

    with pytest.raises(TaskImportError, match="acme/widget#7: review comment 1: malformed line"):
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
    w.fake.scenario("gh", stderr="HTTP 500", exit=1)
    assert main(argv) == 1
    assert "gh api repos/acme/widget failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ("@@ -5,1 +5,1 @@\n-a\n+b\n", None),  # replaces the line
        ("@@ -4,3 +4,2 @@\n x\n-y\n z\n", None),  # deletes it
        ("@@ -6,0 +7,1 @@\n+new\n", None),  # inserts right after the range
        ("@@ -1,1 +1,3 @@\n a\n+b\n+c\n", (7, 8)),  # inserts above: the range moves down
        ("@@ -1,2 +1,1 @@\n-a\n b\n", (4, 5)),  # deletes above: it moves up
        ("@@ -4,1 +4,2 @@\n x\n+new\n", (6, 7)),  # inserts right above: it moves down
        ("@@ -3,5 +3,5 @@\n a\n b\n c\n d\n e\n", (5, 6)),  # context only
        ("@@ -9,1 +9,1 @@\n-a\n+b\n", (5, 6)),  # below the range
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
