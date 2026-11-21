"""Published source archives of the public sets and `rbench tasks fetch` (spec review-tasks, Source
archive; design D11). Every `gh` call goes to the fake `gh`; every task is synthetic.
"""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FakeBin
from test_manifest import write_manifest
from test_tasks import HID, write_task

from rbench.cli import main
from rbench.tasks import load_tasks
from rbench.tasks.archive import ArchiveError
from rbench.tasks.loader import TaskError
from rbench.tasks.release import (
    RELEASE_REPO,
    fetch_archives,
    publish_release,
    verify_archive,
)

CONTENT = {"t-1": "archive of t-1\n", "t-2": "archive of t-2\n"}


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


def local_archives(archive_dir: Path, content: dict = CONTENT) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    for text in content.values():
        (archive_dir / f"{sha(text)}.bundle").write_text(text)
    return archive_dir


def gh_args(fake_bin: FakeBin) -> list[list[str]]:
    return [c["argv"] for c in fake_bin.calls() if c["tool"] == "gh"]


@pytest.mark.parametrize("task_set", ["ours", "reviewbench"])
def test_release_is_one_per_set_version_and_carries_every_archive(
    tmp_path: Path, fake_bin: FakeBin, task_set: str
) -> None:
    set_dir = public_set(tmp_path, task_set)
    archives = local_archives(tmp_path / "archives")

    tag = publish_release(set_dir, task_set, archives)

    assert tag == f"tasks-{task_set}-v1"
    [argv] = gh_args(fake_bin)
    assets = sorted(str((archives / name(t)).resolve()) for t in CONTENT)
    assert argv[: 3 + len(assets)] == ["release", "create", tag, *assets]
    assert argv[argv.index("--repo") + 1] == RELEASE_REPO
    assert "--latest=false" in argv


def test_tasks_sharing_an_archive_share_one_asset(tmp_path: Path, fake_bin: FakeBin) -> None:
    same = {"t-1": "one archive\n", "t-2": "one archive\n"}
    set_dir = public_set(tmp_path, content=same)
    archives = local_archives(tmp_path / "archives", same)

    publish_release(set_dir, "ours", archives)

    [argv] = gh_args(fake_bin)
    assert [a for a in argv if a.endswith(".bundle")] == [
        str((archives / f"{sha('one archive\n')}.bundle").resolve())
    ]


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
    set_dir = public_set(tmp_path)
    archives = local_archives(tmp_path / "archives")
    (archives / name("t-2")).write_text("altered\n")

    with pytest.raises(ArchiveError, match="task t-2: source archive SHA-256 does not match"):
        publish_release(set_dir, "ours", archives)
    assert gh_args(fake_bin) == []


def test_release_that_exists_is_not_changed(tmp_path: Path, fake_bin: FakeBin) -> None:
    set_dir = public_set(tmp_path)
    archives = local_archives(tmp_path / "archives")
    fake_bin.scenario("gh", stderr="a release with the same tag name already exists", exit=1)

    with pytest.raises(ArchiveError, match="release tasks-ours-v1: gh failed: a release with"):
        publish_release(set_dir, "ours", archives)


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
