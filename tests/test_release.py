"""Published source archives of the public sets and `rbench tasks fetch` (spec review-tasks, Source
archive; design D11). Every `gh` call goes to the fake `gh`; every task is synthetic.
"""

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FakeBin
from test_manifest import edit, hashes, write_manifest
from test_tasks import HID, write_task

from rbench.cli import main
from rbench.tasks import GROUND_TRUTH_FILE, MANIFEST_FILE, load_tasks
from rbench.tasks.archive import ArchiveError
from rbench.tasks.loader import TaskError
from rbench.tasks.release import (
    RELEASE_REPO,
    default_archive_dir,
    fetch_archives,
    publish_release,
    verify_archive,
)

CONTENT = {"t-1": "archive of t-1\n", "t-2": "archive of t-2\n"}
RELEASES = f"repos/{RELEASE_REPO}/releases"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def name(task_id: str) -> str:
    return f"{sha(CONTENT[task_id])}.bundle"


def public_set(root: Path, task_set: str = "ours", content: dict = CONTENT) -> Path:
    set_dir = root / task_set
    for task_id, text in content.items():
        write_task(set_dir, task_id, task_set=task_set, archive_sha256=sha(text))
    write_manifest(set_dir, task_set)
    return set_dir


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repo: Path, message: str = "tasks") -> str:
    if not (repo / ".git").exists():
        git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def committed_set(tmp_path: Path, task_set: str = "ours", content: dict = CONTENT) -> Path:
    set_dir = public_set(tmp_path / "repo" / "tasks", task_set, content)
    commit(tmp_path / "repo")
    return set_dir


def local_archives(archive_dir: Path, content: dict = CONTENT) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    for text in content.values():
        (archive_dir / f"{sha(text)}.bundle").write_text(text)
    return archive_dir


def gh_calls(fake_bin: FakeBin) -> list[dict]:
    return [c for c in fake_bin.calls() if c["tool"] == "gh"]


def gh_args(fake_bin: FakeBin) -> list[list[str]]:
    return [c["argv"] for c in gh_calls(fake_bin)]


def creates(fake_bin: FakeBin) -> list[dict]:
    return [c for c in gh_calls(fake_bin) if c["argv"][:2] == ["release", "create"]]


@pytest.mark.parametrize("task_set", ["ours", "reviewbench"])
def test_release_is_one_per_set_version_and_carries_every_archive(
    tmp_path: Path, fake_bin: FakeBin, task_set: str
) -> None:
    set_dir = committed_set(tmp_path, task_set)
    head = git(tmp_path / "repo", "rev-parse", "HEAD")
    archives = local_archives(tmp_path / "archives")

    tag = publish_release(set_dir, task_set, archives)

    assert tag == f"tasks-{task_set}-v1"
    [create] = creates(fake_bin)
    argv = create["argv"]
    assets = sorted(name(t) for t in CONTENT)
    assert argv[: 3 + len(assets)] == ["release", "create", tag, *assets]
    assert argv[argv.index("--repo") + 1] == RELEASE_REPO
    assert argv[argv.index("--target") + 1] == head
    assert "--latest=false" in argv
    assert gh_args(fake_bin)[0] == ["api", f"repos/{RELEASE_REPO}/commits/{head}", "--jq", ".sha"]


def test_assets_reach_gh_as_basenames_from_the_archive_directory(
    tmp_path: Path, fake_bin: FakeBin
) -> None:
    # gh expands an asset argument as a glob and splits it at '#'.
    set_dir = committed_set(tmp_path)
    archives = local_archives(tmp_path / "cache[1]#label" / "archives")
    local_archives(tmp_path / "cache1#label" / "archives", {"t-1": "decoy\n"})

    publish_release(set_dir, "ours", archives)

    [create] = creates(fake_bin)
    assert [a for a in create["argv"] if a.endswith(".bundle")] == sorted(name(t) for t in CONTENT)
    assert not any("/" in a or "#" in a or "[" in a for a in create["argv"][:5])
    assert Path(create["cwd"]).resolve() == archives.resolve()


def test_tasks_sharing_an_archive_share_one_asset(tmp_path: Path, fake_bin: FakeBin) -> None:
    same = {"t-1": "one archive\n", "t-2": "one archive\n"}
    set_dir = committed_set(tmp_path, content=same)
    archives = local_archives(tmp_path / "archives", same)

    publish_release(set_dir, "ours", archives)

    [create] = creates(fake_bin)
    assert [a for a in create["argv"] if a.endswith(".bundle")] == [
        f"{sha('one archive\n')}.bundle"
    ]


def test_older_version_targets_the_commit_of_its_own_manifest(
    tmp_path: Path, fake_bin: FakeBin
) -> None:
    repo = tmp_path / "repo"
    set_dir = committed_set(tmp_path)
    v1 = git(repo, "rev-parse", "HEAD")
    edit(set_dir / "t-2" / GROUND_TRUTH_FILE, lambda gt: gt["items"][0].update(severity="nit"))
    v2_hash = hashes(set_dir)["t-2"]

    def bump(m: dict) -> None:
        m["version"] = 2
        m["tasks"]["t-2"]["sha256"] = v2_hash
        m["changelog"].append({"version": 2, "note": "Fix t-2.", "changed": {"t-2": v2_hash}})

    edit(set_dir / MANIFEST_FILE, bump)
    v2 = commit(repo, "v2")
    (repo / "later.txt").write_text("a later commit that does not touch the set\n")
    commit(repo, "later")
    archives = local_archives(tmp_path / "archives")

    git(repo, "checkout", "-q", v1)
    assert publish_release(set_dir, "ours", archives) == "tasks-ours-v1"
    git(repo, "checkout", "-q", "-")
    assert publish_release(set_dir, "ours", archives) == "tasks-ours-v2"

    targets = [
        (c["argv"][2], c["argv"][c["argv"].index("--target") + 1]) for c in creates(fake_bin)
    ]
    assert targets == [("tasks-ours-v1", v1), ("tasks-ours-v2", v2)]


def test_set_and_manifest_paths_are_literal_for_git(tmp_path: Path, fake_bin: FakeBin) -> None:
    # As a glob, `tasks[1]` also matches `tasks1`.
    repo = tmp_path / "repo"
    set_dir = public_set(repo / "tasks[1]")
    first = commit(repo, "bracketed set")
    public_set(repo / "tasks1", content={"t-1": "another archive\n"})
    commit(repo, "plain set")
    (repo / "tasks1" / "ours" / "untracked.txt").write_text("not part of the released set\n")
    archives = local_archives(tmp_path / "archives")

    publish_release(set_dir, "ours", archives)

    [create] = creates(fake_bin)
    assert create["argv"][create["argv"].index("--target") + 1] == first


def test_release_refuses_a_target_commit_without_the_whole_set(
    tmp_path: Path, fake_bin: FakeBin
) -> None:
    repo = tmp_path / "repo"
    set_dir = public_set(repo / "tasks")
    git(repo, "init", "-q")
    git(repo, "add", str(set_dir / MANIFEST_FILE))
    git(repo, "commit", "-q", "-m", "manifest only")
    manifest_only = git(repo, "rev-parse", "HEAD")
    commit(repo, "task directories")
    archives = local_archives(tmp_path / "archives")

    with pytest.raises(TaskError, match=f"at commit {manifest_only}, which last changed its"):
        publish_release(set_dir, "ours", archives)
    assert gh_args(fake_bin) == []


def test_release_refuses_a_manifest_commit_missing_on_github(
    tmp_path: Path, fake_bin: FakeBin
) -> None:
    set_dir = committed_set(tmp_path)
    archives = local_archives(tmp_path / "archives")
    fake_bin.scenario("gh", stderr="HTTP 422: No commit found for SHA", exit=1)

    with pytest.raises(ArchiveError, match="which holds the manifest, is not on .*; push it first"):
        publish_release(set_dir, "ours", archives)
    assert creates(fake_bin) == []


def test_release_refuses_an_uncommitted_set(tmp_path: Path, fake_bin: FakeBin) -> None:
    set_dir = committed_set(tmp_path)
    archives = local_archives(tmp_path / "archives")
    (set_dir / "t-1" / "notes.txt").write_text("not committed\n")

    with pytest.raises(TaskError, match="has uncommitted changes"):
        publish_release(set_dir, "ours", archives)
    assert gh_args(fake_bin) == []


@pytest.mark.parametrize(
    ("listing", "message"),
    [
        (RELEASES, "already has a release or draft with this tag.*gh release delete tasks-ours-v1"),
        (
            f"repos/{RELEASE_REPO}/git/matching-refs/tags/tasks-ours-v1",
            "already has the tag tasks-ours-v1 without a release",
        ),
    ],
)
def test_release_refuses_an_existing_release_draft_or_tag(
    tmp_path: Path, fake_bin: FakeBin, listing: str, message: str
) -> None:
    set_dir = committed_set(tmp_path)
    archives = local_archives(tmp_path / "archives")
    found = "tasks-ours-v1\n" if listing == RELEASES else "refs/tags/tasks-ours-v1\n"
    fake_bin.scenario("gh", stdout_for={listing: f"tasks-ours-v10\n{found}"})

    with pytest.raises(ArchiveError, match=message):
        publish_release(set_dir, "ours", archives)
    assert creates(fake_bin) == []


def test_release_ignores_a_tag_that_only_starts_the_same(tmp_path: Path, fake_bin: FakeBin) -> None:
    set_dir = committed_set(tmp_path)
    archives = local_archives(tmp_path / "archives")
    fake_bin.scenario(
        "gh",
        stdout_for={
            RELEASES: "tasks-ours-v10\n",
            f"repos/{RELEASE_REPO}/git/matching-refs/tags/tasks-ours-v1": (
                "refs/tags/tasks-ours-v10\n"
            ),
        },
    )

    assert publish_release(set_dir, "ours", archives) == "tasks-ours-v1"


def test_release_refuses_the_hidden_set(tmp_path: Path, fake_bin: FakeBin) -> None:
    with pytest.raises(TaskError, match="only the public sets"):
        publish_release(tmp_path, "hidden", tmp_path)
    assert gh_args(fake_bin) == []


def test_release_refuses_a_hidden_task_in_a_public_set(tmp_path: Path, fake_bin: FakeBin) -> None:
    set_dir = tmp_path / "ours"
    write_task(set_dir, HID, task_set="hidden")

    with pytest.raises(TaskError, match="is a hidden task") as err:
        publish_release(set_dir, "ours", tmp_path)

    assert HID not in str(err.value)
    assert gh_args(fake_bin) == []


def test_release_refuses_an_altered_archive_naming_the_task(
    tmp_path: Path, fake_bin: FakeBin
) -> None:
    set_dir = committed_set(tmp_path)
    archives = local_archives(tmp_path / "archives")
    (archives / name("t-2")).write_text("altered\n")

    with pytest.raises(ArchiveError, match="task t-2: source archive SHA-256 does not match"):
        publish_release(set_dir, "ours", archives)
    assert gh_args(fake_bin) == []


def test_release_names_the_hash_only_archive_an_older_import_lacks(
    tmp_path: Path, fake_bin: FakeBin
) -> None:
    set_dir = committed_set(tmp_path)
    archives = local_archives(tmp_path / "archives")
    (archives / name("t-2")).rename(archives / f"t-2-{name('t-2')}")

    with pytest.raises(ArchiveError) as err:
        publish_release(set_dir, "ours", archives)
    assert str(err.value).startswith(f"task t-2: {archives} has no {name('t-2')};")
    assert "<task-id>-<sha256>.bundle" in str(err.value)
    assert gh_args(fake_bin) == []


def test_relative_xdg_cache_home_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", "cache")
    assert default_archive_dir() == tmp_path / "home" / ".cache" / "rbench" / "archives"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert default_archive_dir() == tmp_path / "xdg" / "rbench" / "archives"


@pytest.mark.parametrize("blocked", ["archive dir is a file", "archive path is a directory"])
def test_cli_fetch_reports_an_unusable_archive_directory(
    tmp_path: Path, fake_bin: FakeBin, capsys, blocked: str
) -> None:
    root = tmp_path / "tasks"
    public_set(root)
    archives = tmp_path / "archives"
    if blocked == "archive dir is a file":
        archives.write_text("not a directory\n")
        where = f"archive directory {archives}"
    else:
        (archives / name("t-1")).mkdir(parents=True)
        where = f"task t-1: archive directory {archives}"
        fake_bin.scenario("gh", stdout_for={name("t-1"): CONTENT["t-1"]})

    assert main(["tasks", "fetch", "--tasks-root", str(root), "--archive-dir", str(archives)]) == 1
    assert f"rbench tasks fetch: {where}: " in capsys.readouterr().err


def test_fetch_downloads_and_verifies_every_archive(tmp_path: Path, fake_bin: FakeBin) -> None:
    set_dir = public_set(tmp_path)
    archives = tmp_path / "cache" / "archives"
    fake_bin.scenario("gh", stdout_for={name(t): text for t, text in CONTENT.items()})

    assert fetch_archives(set_dir, "ours", archives) == ["t-1", "t-2"]

    assert sorted(p.name for p in archives.iterdir()) == sorted(name(t) for t in CONTENT)
    for task_id, text in CONTENT.items():
        assert (archives / name(task_id)).read_text() == text
    assert gh_args(fake_bin) == [
        ["release", "download", "tasks-ours-v1", "--repo", RELEASE_REPO]
        + ["--pattern", name(t), "--output", "-"]
        for t in ("t-1", "t-2")
    ]


def test_fetch_refuses_a_mismatch_naming_the_task_and_leaves_nothing(
    tmp_path: Path, fake_bin: FakeBin
) -> None:
    set_dir = public_set(tmp_path)
    archives = tmp_path / "archives"
    fake_bin.scenario("gh", stdout_for={name("t-1"): "tampered\n"})

    with pytest.raises(ArchiveError, match="task t-1: source archive SHA-256 does not match"):
        fetch_archives(set_dir, "ours", archives)

    assert list(archives.iterdir()) == []


def test_fetch_failure_names_the_task_and_leaves_nothing(tmp_path: Path, fake_bin: FakeBin) -> None:
    set_dir = public_set(tmp_path)
    archives = tmp_path / "archives"
    fake_bin.scenario("gh", stdout="partial", stderr="release not found", exit=1)

    with pytest.raises(ArchiveError, match="task t-1: download from release tasks-ours-v1: gh"):
        fetch_archives(set_dir, "ours", archives)

    assert list(archives.iterdir()) == []


def test_fetch_keeps_a_matching_archive_and_replaces_an_altered_one(
    tmp_path: Path, fake_bin: FakeBin
) -> None:
    set_dir = public_set(tmp_path)
    archives = local_archives(tmp_path / "archives")
    (archives / name("t-2")).write_text("altered\n")
    fake_bin.scenario("gh", stdout_for={name("t-2"): CONTENT["t-2"]})

    assert fetch_archives(set_dir, "ours", archives) == ["t-2"]

    assert (archives / name("t-2")).read_text() == CONTENT["t-2"]
    assert len(gh_args(fake_bin)) == 1


def test_fetch_refuses_the_hidden_set(tmp_path: Path, fake_bin: FakeBin) -> None:
    with pytest.raises(TaskError, match="only the public sets"):
        fetch_archives(tmp_path, "hidden", tmp_path / "archives")
    assert gh_args(fake_bin) == []


def test_trial_archive_check_refuses_a_mismatch_naming_the_task(tmp_path: Path) -> None:
    [task] = load_tasks(public_set(tmp_path, content={"t-1": CONTENT["t-1"]}))
    good = local_archives(tmp_path / "archives", {"t-1": CONTENT["t-1"]}) / name("t-1")
    assert verify_archive(task, good) == good

    altered = tmp_path / "altered.bundle"
    altered.write_text(CONTENT["t-1"] + "x")
    with pytest.raises(ArchiveError, match="^task t-1: source archive SHA-256 does not match"):
        verify_archive(task, altered)
    with pytest.raises(ArchiveError, match="^task t-1: cannot read its source archive"):
        verify_archive(task, tmp_path / "missing.bundle")


def test_trial_archive_check_names_no_hidden_task(tmp_path: Path) -> None:
    [task] = load_tasks(public_set(tmp_path, content={"t-1": CONTENT["t-1"]}))
    hidden = replace(task, id=HID, set="hidden")
    altered = tmp_path / "altered.bundle"
    altered.write_text("altered\n")

    for path in (altered, tmp_path / HID):
        with pytest.raises(ArchiveError, match="^a task in set 'hidden': ") as err:
            verify_archive(hidden, path)
        assert HID not in str(err.value)


def test_cli_fetch_and_release(tmp_path: Path, fake_bin: FakeBin, capsys) -> None:
    root = tmp_path / "tasks"
    public_set(root)
    commit(tmp_path)
    archives = tmp_path / "archives"
    fake_bin.scenario("gh", stdout_for={name(t): text for t, text in CONTENT.items()})
    common = ["--tasks-root", str(root), "--archive-dir", str(archives)]

    assert main(["tasks", "fetch", *common]) == 0
    assert "ours: fetched 2 archives" in capsys.readouterr().out
    assert main(["tasks", "release", "ours", *common, "--repo", "example/fork"]) == 0
    assert "ours: released tasks-ours-v1 on example/fork" in capsys.readouterr().out

    (archives / name("t-1")).write_text("altered\n")
    fake_bin.scenario("gh", stdout="tampered\n")
    assert main(["tasks", "fetch", *common, "--set", "ours"]) == 1
    assert "task t-1: source archive SHA-256 does not match" in capsys.readouterr().err
    assert sorted(p.name for p in archives.iterdir()) == sorted(name(t) for t in CONTENT)


def test_cli_fetch_without_a_public_set(tmp_path: Path, capsys) -> None:
    assert main(["tasks", "fetch", "--tasks-root", str(tmp_path)]) == 1
    assert "no public task set under" in capsys.readouterr().err
