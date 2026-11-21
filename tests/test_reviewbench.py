"""The `reviewbench` import (spec review-tasks: Severity mapping from external sets, Pinned
ReviewBench source, Source archive, Source license; design D1).

The ReviewBench clone, the source mirrors and GitHub are all synthetic: git repositories built in
tmp_path and the fake `gh`, which answers each API path from its `replies`.
"""

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import FakeBin
from test_archive import MIT, commit, git, license_reply

from rbench.cli import main
from rbench.tasks import HIDDEN_ID_PREFIX, MANIFEST_FILE, TaskError, load_task_set, reviewbench
from rbench.tasks.license import LicenseLookupError
from rbench.tasks.manifest import check_version_history, load_manifest
from rbench.tasks.reviewbench import (
    SEVERITY_MAP,
    TEST_SUBSET,
    ReviewBenchError,
    import_reviewbench,
)

HEAD_DATE = "2026-04-08T23:21:05Z"
OPENED = "2026-04-08T23:30:00Z"
MERGED = "2026-04-09T05:05:18Z"
LATER = "2026-04-09T05:06:14Z"
INSIDE = "2026-04-09T01:00:00Z"
BEFORE = "2026-04-08T20:00:00Z"


def finding(tp_fp: str = "tp", severity: str = "medium", producer: str = "human_review", **kw):
    return {
        "file": "app.py",
        "start_line": 1,
        "end_line": 2,
        "message": "The loop bound is off by one.",
        "tp_fp": tp_fp,
        "severity": severity,
        "category": "correctness",
        "scope": "line",
        "difficulty": "easy",
        "context_required": "diff",
        "tp_fp_justification": "golden-only justification text",
        "severity_justification": "golden-only severity text",
        "category_justification": "golden-only category text",
        "producer": producer,
        "source": {"type": "review_comment", "alignment": "exact"},
        **kw,
    }


@dataclass
class PR:
    """One synthetic PR: its mirror repository, its golden findings and its GitHub history."""

    nwo: str
    number: int
    base: str = ""
    head: str = ""
    findings: list = field(default_factory=lambda: [finding()])
    title: str = "Fix the loop bound"
    body: str = "Fixes the loop."
    renames: list = field(default_factory=list)  # (time, from, to)
    edits: list = field(default_factory=list)  # (time, text)
    edits_total: int | None = None
    license: str = "MIT"
    timeline: object = None  # the raw timeline reply, instead of one built from `renames`

    @property
    def key(self) -> str:
        return f"{self.nwo.replace('/', '_')}_{self.number}-{self.head[:8]}"

    @property
    def task_id(self) -> str:
        return self.key.lower()

    def entry(self) -> dict:
        return {
            "repo": f"https://github.com/{self.nwo}",
            "pr_number": self.number,
            "pr_url": f"https://github.com/{self.nwo}/pull/{self.number}",
            "base": self.base,
            "head": self.head,
            "title": "a later title the import must not use",
            "body": "a later body the import must not use",
            "nwo": self.nwo,
            "language": "Python",
            "repo_size_kb": 10,
            "lines_added": 1,
            "lines_removed": 1,
            "files_changed": 1,
        }

    def golden(self) -> dict:
        pr = {"repo": self.nwo, "pr_number": self.number, "base": self.base, "head": self.head}
        return {"pr_key": self.key, "pr": pr, "findings": self.findings}

    def replies(self) -> list[dict]:
        api = f"repos/{self.nwo}"
        events = [{"event": "labeled", "created_at": INSIDE}] + [
            {"event": "renamed", "created_at": t, "rename": {"from": old, "to": new}}
            for t, old, new in self.renames
        ]
        nodes = [{"editedAt": t, "deletedAt": None, "diff": text} for t, text in self.edits]
        edits = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "body": self.body,
                        "userContentEdits": {
                            "totalCount": len(nodes)
                            if self.edits_total is None
                            else self.edits_total,
                            "nodes": list(reversed(nodes)),
                        },
                    }
                }
            }
        }
        pull = {"title": self.title, "created_at": OPENED, "merged_at": MERGED, "closed_at": MERGED}
        return [
            {"match": f"{api}/license?ref={self.head}", "stdout": license_reply(self.license)},
            {"match": f"{api}/pulls/{self.number}", "stdout": json.dumps(pull)},
            {"match": f"{api}/commits/{self.head}", "stdout": commit_reply()},
            {
                "match": f"{api}/issues/{self.number}/timeline",
                "stdout": json.dumps([events] if self.timeline is None else self.timeline),
            },
            {"match": f"name={self.nwo.split('/')[1]}", "stdout": json.dumps(edits)},
        ]


def commit_reply() -> str:
    return json.dumps({"commit": {"committer": {"date": HEAD_DATE}}})


@dataclass
class World:
    tmp: Path
    fake: FakeBin
    prs: list[PR]
    checkout: Path
    commit: str = ""

    @property
    def set_dir(self) -> Path:
        return self.tmp / "tasks" / "reviewbench"

    @property
    def archives(self) -> Path:
        return self.tmp / "archives"

    @property
    def mirrors(self) -> str:
        return f"{self.tmp / 'mirrors'}/"

    def publish(self, prs: list[PR] | None = None, note: str = "corpus") -> str:
        """Commit the subset and golden files of `prs` to the ReviewBench clone; return the id."""
        prs = self.prs if prs is None else prs
        files = {TEST_SUBSET: json.dumps([pr.entry() for pr in prs], indent=1)}
        files |= {f"golden/{pr.key}.json": json.dumps(pr.golden()) for pr in prs}
        for name in files:
            (self.checkout / name).parent.mkdir(parents=True, exist_ok=True)
        self.commit = commit(self.checkout, files, note)
        self.answer()
        return self.commit

    def answer(self) -> None:
        self.fake.scenario("gh", replies=[r for pr in self.prs for r in pr.replies()])

    def run(self, **kw):
        return import_reviewbench(
            self.checkout,
            self.set_dir,
            self.archives,
            commit=kw.pop("commit", self.commit),
            mirror_base=self.mirrors,
            **kw,
        )


def mirror(root: Path, pr: PR) -> None:
    repo = root / pr.nwo.replace("/", "_")
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    pr.base = commit(repo, {"LICENSE": MIT, "app.py": "x = 1\n"}, "base")
    pr.head = commit(repo, {"app.py": f"x = {pr.number}\n"}, "change")


def world(tmp_path: Path, fake_bin: FakeBin, *prs: PR) -> World:
    for pr in prs:
        mirror(tmp_path / "mirrors", pr)
    checkout = tmp_path / "ReviewBench"
    checkout.mkdir()
    git(checkout, "init", "-q", "-b", "main")
    w = World(tmp_path, fake_bin, list(prs), checkout)
    w.publish()
    return w


def leftovers(w: World) -> list[str]:
    """Staging and previous-version directories an import left behind."""
    dirs = [*w.set_dir.parent.iterdir(), *w.archives.iterdir()]
    return sorted(p.name for p in dirs if p.name.startswith("."))


def snapshot(*dirs: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for d in dirs for p in d.rglob("*") if p.is_file()}


def gt(w: World, pr: PR) -> dict:
    return json.loads((w.set_dir / pr.task_id / "ground_truth.json").read_text())


def meta(w: World, pr: PR) -> dict:
    return json.loads((w.set_dir / pr.task_id / "task.json").read_text())


def manifest(w: World) -> dict:
    return json.loads((w.set_dir / MANIFEST_FILE).read_text())


def test_items_ids_severity_and_producer_follow_d1(tmp_path: Path, fake_bin: FakeBin) -> None:
    pr = PR(
        "acme/widget",
        7,
        findings=[
            finding("tp", "high", "human_review"),
            finding("fp", "medium", "llm_review:claude-sonnet-4.6"),
            finding("tp", "medium", "llm_review:claude-sonnet-4.6"),
            finding("tp", "low", "deterministic_tool:semgrep"),
            finding("tp", "style", "ccr:gpt-5.5[ReasoningEffort=medium]"),
        ],
    )
    w = world(tmp_path, fake_bin, pr)

    result = w.run()

    items = gt(w, pr)["items"]
    assert [i["id"] for i in items] == [f"{pr.key}#0", f"{pr.key}#2", f"{pr.key}#3", f"{pr.key}#4"]
    assert [i["severity"] for i in items] == ["must", "should", "nit", "nit"]
    assert [i["producer"] for i in items] == [
        "human",
        "llm_review:claude-sonnet-4.6",
        "unknown",
        "ccr:gpt-5.5[ReasoningEffort=medium]",
    ]
    assert set(items[0]) == {
        "id",
        "file",
        "start_line",
        "end_line",
        "message",
        "category",
        "severity",
        "producer",
    }
    assert result.imported == (pr.task_id,) and result.skipped == {}
    _, tasks = load_task_set(w.set_dir, "reviewbench")
    assert [t.id for t in tasks] == [pr.task_id]
    assert tasks[0].ground_truth[1].producer.engine == "llm_review"


def test_manifest_records_commit_mapping_golden_and_license(
    tmp_path: Path, fake_bin: FakeBin
) -> None:
    pr = PR("acme/widget", 7)
    w = world(tmp_path, fake_bin, pr)
    w.run()

    data = manifest(w)
    assert data["version"] == 1
    assert data["source"]["repo"] == "review-bench/ReviewBench"
    assert data["source"]["commit"] == w.commit
    assert data["source"]["subset"] == TEST_SUBSET
    assert data["source"]["severity_map"] == SEVERITY_MAP
    assert data["source"]["severity_map"]["high"] == "must"
    assert data["source"]["producer_map"]["human_review"] == "human"
    golden = git(w.checkout, "cat-file", "blob", f"{w.commit}:golden/{pr.key}.json")
    entry = data["tasks"][pr.task_id]
    assert entry["golden"] == {
        "path": f"golden/{pr.key}.json",
        "sha256": hashlib.sha256(golden.encode()).hexdigest(),
    }
    assert entry["license"] == "MIT"
    loaded = load_manifest(w.set_dir / MANIFEST_FILE, "reviewbench")
    assert loaded.source is not None and loaded.source.commit == w.commit


def test_archive_license_on_original_repo_and_no_golden_stored(
    tmp_path: Path, fake_bin: FakeBin
) -> None:
    pr = PR("acme/widget", 7)
    w = world(tmp_path, fake_bin, pr)
    w.run()

    sha = meta(w, pr)["archive_sha256"]
    bundle = w.archives / f"{pr.task_id}-{sha}.bundle"
    assert sha == hashlib.sha256(bundle.read_bytes()).hexdigest()
    license_calls = [c["argv"] for c in fake_bin.calls() if "license" in " ".join(c["argv"])]
    assert license_calls == [["api", f"repos/acme/widget/license?ref={pr.head}"]]
    stored = "".join(p.read_text() for p in w.set_dir.rglob("*") if p.is_file())
    assert "golden-only" not in stored and '"tp_fp"' not in stored
    assert "a later title" not in stored and "a later body" not in stored
    assert meta(w, pr)["title"] == pr.title and meta(w, pr)["description"] == pr.body
    assert leftovers(w) == []


def test_same_commit_again_writes_nothing(tmp_path: Path, fake_bin: FakeBin) -> None:
    pr = PR("acme/widget", 7)
    w = world(tmp_path, fake_bin, pr)
    w.run()
    before = {p: p.read_bytes() for p in w.set_dir.rglob("*") if p.is_file()}

    result = w.run()

    assert not result.changed and result.version == 1
    assert {p: p.read_bytes() for p in w.set_dir.rglob("*") if p.is_file()} == before


def test_another_commit_makes_a_new_version(tmp_path: Path, fake_bin: FakeBin) -> None:
    kept, dropped = PR("acme/widget", 7), PR("acme/gadget", 3)
    w = world(tmp_path, fake_bin, kept, dropped)
    w.run()
    v1 = load_manifest(w.set_dir / MANIFEST_FILE, "reviewbench")
    old_hash = v1.tasks[kept.task_id].sha256
    w.prs = [kept]
    w.publish(note="upstream update")

    result = w.run()

    v2 = load_manifest(w.set_dir / MANIFEST_FILE, "reviewbench")
    assert result.changed and v2.version == 2
    assert v2.source is not None and v2.source.commit == w.commit != v1.source.commit
    assert v2.changelog[-1].changed == {kept.task_id: old_hash, dropped.task_id: None}
    assert w.commit in v2.changelog[-1].note
    assert v2.changelog[0] == v1.changelog[0]
    assert not (w.set_dir / dropped.task_id).exists()
    check_version_history(v1, v2)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"renames": [(INSIDE, "Old title", "Fix the loop bound")]}, "title changed inside"),
        ({"renames": [(HEAD_DATE, "Old title", "Fix the loop bound")]}, "title changed inside"),
        ({"edits": [(OPENED, "Draft."), (MERGED, "Fixes the loop.")]}, "description changed"),
        (
            {"edits": [(OPENED, "Fixes the loop.")], "edits_total": 2},
            "description edit history is not readable",
        ),
        (
            {"edits": [(OPENED, "Draft."), (LATER, "Fixes the loop.")]},
            "description edited after the review window",
        ),
        ({"timeline": {}}, "title history is not readable"),
        ({"timeline": [{}]}, "title history is not readable"),
        ({"timeline": [[{"created_at": INSIDE}]]}, "title history is not readable"),
        (
            {"timeline": [[{"event": "renamed", "created_at": LATER, "rename": {"from": 1}}]]},
            "title history is not readable",
        ),
        (
            {"timeline": [[{"event": "renamed", "created_at": "soon", "rename": {}}]]},
            "title history is not readable",
        ),
    ],
)
def test_change_inside_the_window_skips_and_names_the_task(
    tmp_path: Path, fake_bin: FakeBin, change: dict, reason: str
) -> None:
    good, bad = PR("acme/widget", 7), PR("acme/gadget", 3, **change)
    w = world(tmp_path, fake_bin, good, bad)

    result = w.run()

    assert reason in result.skipped[bad.task_id]
    assert result.imported == (good.task_id,)
    assert not (w.set_dir / bad.task_id).exists()
    assert not list(w.archives.glob(f"{bad.task_id}-*"))
    entry = load_manifest(w.set_dir / MANIFEST_FILE, "reviewbench").changelog[-1]
    assert entry.changed[bad.task_id] is None
    assert f"{bad.task_id} ({reason}" in entry.note
    assert bad.task_id not in manifest(w)["tasks"]


def test_title_renamed_after_the_merge_is_rewound(tmp_path: Path, fake_bin: FakeBin) -> None:
    pr = PR(
        "acme/widget",
        7,
        title="implement data storages",
        body="Opened with this text.",
        renames=[(BEFORE, "First title", "Add dependencies"), (LATER, "Add dependencies", "x")],
        # The text the PR was opened with, inside the window, is GitHub's oldest revision.
        edits=[(OPENED, "Opened with this text.")],
    )
    # The later rename's `to` is the current title, as GitHub records it.
    pr.renames[1] = (LATER, "Add dependencies", pr.title)
    w = world(tmp_path, fake_bin, pr)

    w.run()

    assert meta(w, pr)["title"] == "Add dependencies"
    assert meta(w, pr)["description"] == "Opened with this text."


def test_refused_license_skips_the_task(tmp_path: Path, fake_bin: FakeBin) -> None:
    good, bad = PR("acme/widget", 7), PR("acme/gadget", 3, license="NOASSERTION")
    w = world(tmp_path, fake_bin, good, bad)

    result = w.run()

    assert "license refused" in result.skipped[bad.task_id]
    assert "acme/gadget" in result.skipped[bad.task_id]
    assert result.imported == (good.task_id,)
    for task in load_task_set(w.set_dir, "reviewbench")[1]:
        assert manifest(w)["tasks"][task.id]["license"] == "MIT"


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (finding(severity="critical"), r"finding 1: unknown severity 'critical'"),
        (finding(producer="semgrep"), r"finding 1: unknown producer form 'semgrep'"),
        (finding(producer="human"), r"finding 1: unknown producer form 'human'"),
        (finding(tp_fp="maybe"), r"finding 1: 'tp_fp' must be 'tp' or 'fp'"),
        ({**finding(), "extra": 1}, r"finding 1 must be an object with exactly"),
        (finding(start_line=0), r"finding 1: line range"),
        (finding(file="../etc/passwd"), r"finding 1: 'file' must be a path inside"),
        (finding(message=""), r"finding 1: 'message' must be a non-empty string"),
        (finding("fp") | {"start_line": 0, "message": []}, r"finding 1: line range"),
        (finding("fp", category=None), r"finding 1: 'category' must be a non-empty string"),
        (finding("fp", severity="critical"), r"finding 1: unknown severity 'critical'"),
    ],
)
def test_malformed_golden_refuses_the_import(
    tmp_path: Path, fake_bin: FakeBin, bad: dict, message: str
) -> None:
    pr = PR("acme/widget", 7, findings=[finding(), bad])
    w = world(tmp_path, fake_bin, pr)

    with pytest.raises(ReviewBenchError, match=message) as err:
        w.run()

    assert pr.key in str(err.value)
    assert not w.set_dir.exists()
    # Refused before any history or license lookup.
    assert fake_bin.calls() == []


def test_malformed_subset_entry_is_refused(tmp_path: Path, fake_bin: FakeBin) -> None:
    pr = PR("acme/widget", 7)
    w = world(tmp_path, fake_bin, pr)
    entry = pr.entry() | {"head": "abc"}
    w.commit = commit(w.checkout, {TEST_SUBSET: json.dumps([entry])}, "bad")

    with pytest.raises(ReviewBenchError, match=r"entry 0: 'head' must be a full commit id"):
        w.run()


def test_hidden_prefixed_key_is_refused(tmp_path: Path, fake_bin: FakeBin) -> None:
    w = world(tmp_path, fake_bin, PR(f"{HIDDEN_ID_PREFIX}acme/widget", 7))

    with pytest.raises(ReviewBenchError, match=r"does not make a valid public task id"):
        w.run()


def test_failing_gh_stops_instead_of_skipping(tmp_path: Path, fake_bin: FakeBin) -> None:
    pr = PR("acme/widget", 7)
    w = world(tmp_path, fake_bin, pr)
    fake_bin.scenario("gh", stderr="HTTP 403: rate limit exceeded", exit=1)

    with pytest.raises(
        ReviewBenchError, match=rf"{pr.key}: gh api repos/acme/widget/pulls/7 failed: HTTP 403"
    ):
        w.run()
    assert not (w.set_dir / MANIFEST_FILE).exists()


def test_working_tree_edits_do_not_change_the_import(tmp_path: Path, fake_bin: FakeBin) -> None:
    pr = PR("acme/widget", 7)
    w = world(tmp_path, fake_bin, pr)
    (w.checkout / f"golden/{pr.key}.json").write_text("not json")

    w.run()

    assert len(gt(w, pr)["items"]) == 1


def test_existing_set_must_load_before_reimport(tmp_path: Path, fake_bin: FakeBin) -> None:
    pr = PR("acme/widget", 7)
    w = world(tmp_path, fake_bin, pr)
    w.run()
    edit = w.set_dir / pr.task_id / "task.json"
    edit.write_text(edit.read_text().replace(pr.title, "Edited by hand"))

    with pytest.raises(TaskError, match="content hash"):
        w.run()


def test_cli_imports_the_set(tmp_path: Path, fake_bin: FakeBin, capsys) -> None:
    pr = PR("acme/widget", 7)
    w = world(tmp_path, fake_bin, pr)
    argv = [
        "tasks",
        "import",
        "reviewbench",
        "--checkout",
        str(w.checkout),
        "--commit",
        w.commit,
        "--tasks",
        str(w.set_dir),
        "--archives",
        str(w.archives),
        "--mirror-base",
        w.mirrors,
    ]

    assert main(argv) == 0
    assert "reviewbench: new version 1, 1 tasks" in capsys.readouterr().out
    fake_bin.scenario("gh", stderr="HTTP 500", exit=1)
    (w.set_dir / MANIFEST_FILE).unlink()
    assert main(argv) == 1
    assert "gh api repos/acme/widget/pulls/7 failed" in capsys.readouterr().err


def changed_golden(w: World, pr: PR) -> None:
    """Publish a new ReviewBench commit in which the PR's ground truth differs."""
    pr.findings = [finding(message="A different finding.")]
    w.publish(note="changed finding")


def test_invalid_new_version_leaves_the_installed_one(tmp_path: Path, fake_bin: FakeBin) -> None:
    pr = PR("acme/widget", 7)
    w = world(tmp_path, fake_bin, pr)
    w.run()
    before = snapshot(w.set_dir, w.archives)

    with pytest.raises(ReviewBenchError, match=r"'subset' must be a relative path"):
        w.run(subset="./" + TEST_SUBSET)

    assert snapshot(w.set_dir, w.archives) == before
    assert leftovers(w) == []
    load_task_set(w.set_dir, "reviewbench")


def test_failed_manifest_write_leaves_the_installed_one(
    tmp_path: Path, fake_bin: FakeBin, monkeypatch: pytest.MonkeyPatch
) -> None:
    pr = PR("acme/widget", 7)
    w = world(tmp_path, fake_bin, pr)
    w.run()
    before = snapshot(w.set_dir, w.archives)
    changed_golden(w, pr)
    real = reviewbench.atomic_write

    def failing(path: Path, data) -> None:
        if Path(path).name == MANIFEST_FILE:
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(reviewbench, "atomic_write", failing)
    with pytest.raises(OSError, match="disk full"):
        w.run()

    assert snapshot(w.set_dir, w.archives) == before
    load_task_set(w.set_dir, "reviewbench")
    monkeypatch.setattr(reviewbench, "atomic_write", real)
    assert w.run().version == 2
    assert gt(w, pr)["items"][0]["message"] == "A different finding."


def test_rerun_after_a_crash_mid_swap_ends_consistent(
    tmp_path: Path, fake_bin: FakeBin, monkeypatch: pytest.MonkeyPatch
) -> None:
    pr = PR("acme/widget", 7)
    w = world(tmp_path, fake_bin, pr)
    w.run()
    before = snapshot(w.set_dir)
    changed_golden(w, pr)
    real = os.rename

    def killed(src, dst) -> None:
        if ".import-" in str(src):
            raise KeyboardInterrupt  # the process dies between the two renames
        real(src, dst)

    monkeypatch.setattr(os, "rename", killed)
    with pytest.raises(KeyboardInterrupt):
        w.run()
    monkeypatch.setattr(os, "rename", real)
    assert not w.set_dir.exists()

    reviewbench._recover(w.set_dir)  # what the next run does first
    assert snapshot(w.set_dir) == before
    assert w.run().version == 2
    assert leftovers(w) == []
    load_task_set(w.set_dir, "reviewbench")


def test_failed_license_lookup_stops_and_keeps_the_set(tmp_path: Path, fake_bin: FakeBin) -> None:
    pr = PR("acme/widget", 7)
    w = world(tmp_path, fake_bin, pr)
    w.run()
    before = snapshot(w.set_dir, w.archives)

    for failure in ({"stderr": "HTTP 500: server error", "exit": 1}, {"stdout": "{}"}):
        fake_bin.scenario("gh", replies=[{"match": "/license", **failure}, *pr.replies()])
        with pytest.raises(LicenseLookupError, match="acme/widget"):
            w.run()
        assert snapshot(w.set_dir, w.archives) == before


def test_replace_refs_do_not_change_the_pinned_commit(tmp_path: Path, fake_bin: FakeBin) -> None:
    pr = PR("acme/widget", 7)
    w = world(tmp_path, fake_bin, pr)
    pinned = w.commit
    pr.findings = [finding(message="From a different commit")]
    other = w.publish(note="other")
    git(w.checkout, "replace", pinned, other)

    w.run(commit=pinned)

    assert gt(w, pr)["items"][0]["message"] == "The loop bound is off by one."
    assert manifest(w)["source"]["commit"] == pinned


def test_pin_must_be_a_commit(tmp_path: Path, fake_bin: FakeBin) -> None:
    w = world(tmp_path, fake_bin, PR("acme/widget", 7))
    tree = git(w.checkout, "rev-parse", f"{w.commit}^{{tree}}")

    with pytest.raises(ReviewBenchError, match=rf"{tree} is not a commit"):
        w.run(commit=tree)
    with pytest.raises(ReviewBenchError, match=r"is not a full commit id"):
        w.run(commit="main")
