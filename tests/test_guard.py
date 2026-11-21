"""Publication guard (spec review-tasks: Publication guard).

Every hidden directory, id, canary and prefix here is synthetic and built in tmp_path; hidden ids
are built at runtime from HIDDEN_ID_PREFIX, since the guard rejects them as literals.
"""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_hidden import git_repo, hidden_dir
from test_tasks import HIDDEN, write_task

from rbench.tasks import GROUND_TRUTH_FILE, HIDDEN_DIR_ENV, HIDDEN_ID_PREFIX
from rbench.tasks.guard import main, public_findings

HID = f"{HIDDEN_ID_PREFIX}leak1"
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.test"]


@pytest.fixture(autouse=True)
def no_outer_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.delenv(HIDDEN_DIR_ENV, raising=False)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *GIT_ID, "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def tracked(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
        git(repo, "add", "-f", name)


def run_public(repo: Path, monkeypatch, capsys) -> tuple[int, str]:
    monkeypatch.chdir(repo)
    code = main(["public"])
    out = capsys.readouterr()
    assert out.out == ""
    return code, out.err


# Public check


def test_public_check_names_the_file_with_a_hidden_id_but_not_the_id(tmp_path, monkeypatch, capsys):
    repo = git_repo(tmp_path / "repo")
    tracked(repo, {"README.md": "ok\n", "docs/notes.md": f"intro\nsee tasks/{HID}/task.json\n"})
    code, err = run_public(repo, monkeypatch, capsys)
    assert code == 1
    assert "docs/notes.md:2: a task id with the hidden prefix" in err
    assert HID not in err and "README.md" not in err


@pytest.mark.parametrize("text", [f'"{HID}"', f"id={HIDDEN_ID_PREFIX}7", f"({HIDDEN_ID_PREFIX}a)"])
def test_public_check_flags_every_hidden_id_form(tmp_path, text):
    repo = git_repo(tmp_path / "repo")
    tracked(repo, {"t.py": text})
    assert public_findings(repo) == ["t.py:1: a task id with the hidden prefix"]


def test_public_check_flags_a_hidden_path_and_a_symlink_to_one(tmp_path):
    repo = git_repo(tmp_path / "repo")
    tracked(repo, {f"tasks/{HID}/task.json": "{}"})
    (repo / "link").symlink_to(f"tasks/{HID}")
    git(repo, "add", "link")
    assert public_findings(repo) == [
        "link:1: a task id with the hidden prefix",
        f"tasks/{HID}/task.json: the path holds a task id with the hidden prefix",
    ]


def test_public_check_flags_tracked_run_output(tmp_path, monkeypatch, capsys):
    repo = git_repo(tmp_path / "repo")
    tracked(repo, {"runs/r1/ledger.json": "{}"})  # forced past the /runs/ ignore rule
    code, err = run_public(repo, monkeypatch, capsys)
    assert code == 1
    assert "runs/r1/ledger.json: unpublished run output" in err


def test_public_check_passes_prose_globs_and_placeholders(tmp_path, monkeypatch, capsys):
    repo = git_repo(tmp_path / "repo")
    prose = (
        "A high-level, push-ready branch-switch.\n"
        f"Hidden ids look like `{HIDDEN_ID_PREFIX}*` or {HIDDEN_ID_PREFIX}<n>, never real ones.\n"
        f"with-{HIDDEN_ID_PREFIX}x and OH-1 and runs/ in prose\n"
    )
    tracked(repo, {"docs/a.md": prose, "src/runs/keep.py": "", "tests/runs.py": ""})
    assert run_public(repo, monkeypatch, capsys) == (0, "")


# Pre-push check


@pytest.fixture
def pushing(tmp_path, monkeypatch):
    """A public checkout with a pushed main, and a hidden directory holding one synthetic task."""
    hidden = hidden_dir(tmp_path)
    write_task(hidden, HID, "hidden")
    canary = json.loads((hidden / HID / GROUND_TRUTH_FILE).read_text())["canary"]
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = git_repo(tmp_path / "public")
    git(repo, "checkout", "-q", "-b", "main")
    tracked(repo, {"a.txt": "base\n"})
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    monkeypatch.chdir(repo)
    monkeypatch.setenv(HIDDEN_DIR_ENV, str(hidden))
    return repo, hidden, canary


def commit(repo: Path, files: dict[str, str], message: str = "change") -> None:
    tracked(repo, files)
    git(repo, "commit", "-q", "--allow-empty", "-m", message)


def pre_push(repo: Path, monkeypatch, capsys, branch: str = "main") -> tuple[int, str]:
    sha = git(repo, "rev-parse", "HEAD")
    remote_sha = git(repo, "rev-parse", "origin/main") if branch == "main" else "0" * 40
    line = f"refs/heads/{branch} {sha} refs/heads/{branch} {remote_sha}\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(line))
    code = main(["pre-push", "origin", "unused"])
    out = capsys.readouterr()
    assert out.out == ""
    return code, out.err


def test_pre_push_rejects_a_hidden_id_and_names_it(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    commit(repo, {"b.txt": f"see {HID}\n"})
    code, err = pre_push(repo, monkeypatch, capsys)
    assert code == 1
    assert f"adds hidden task id {HID}" in err


def test_pre_push_rejects_a_canary_and_the_prefix(pushing, monkeypatch, capsys):
    repo, _, canary = pushing
    commit(repo, {"b.txt": canary})
    commit(repo, {"c.txt": f"prefix {HIDDEN}"})
    code, err = pre_push(repo, monkeypatch, capsys)
    assert code == 1
    assert f"adds the canary of hidden task {HID}" in err
    assert "adds the hidden canary prefix" in err


def test_pre_push_rejects_an_id_a_later_commit_removes(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    commit(repo, {"b.txt": f"{HID}\n"})
    commit(repo, {"b.txt": "gone\n"})
    code, err = pre_push(repo, monkeypatch, capsys)
    assert code == 1
    assert err.count(HID) == 1


def test_pre_push_checks_messages_and_new_branch_names(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    git(repo, "checkout", "-q", "-b", f"feature/{HID}")
    commit(repo, {}, message=f"- fix {HID}")
    code, err = pre_push(repo, monkeypatch, capsys, branch=f"feature/{HID}")
    assert code == 1
    assert f"the ref name refs/heads/feature/{HID} adds hidden task id" in err
    assert "commit " in err


def test_pre_push_allows_a_clean_push_and_removed_lines(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    commit(repo, {"a.txt": f"{HID}\n"})
    git(repo, "push", "-q", "origin", "main")  # already public, before the hook existed
    commit(repo, {"a.txt": "edited\n", "d.txt": "high-level\n"})
    assert pre_push(repo, monkeypatch, capsys) == (0, "")


def test_pre_push_without_a_hidden_directory_notes_and_allows(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    monkeypatch.delenv(HIDDEN_DIR_ENV)
    commit(repo, {"b.txt": "x\n"})
    code, err = pre_push(repo, monkeypatch, capsys)
    assert code == 0
    assert f"{HIDDEN_DIR_ENV} is not set" in err


def test_pre_push_with_an_unreadable_hidden_directory_rejects(pushing, monkeypatch, capsys):
    repo, hidden, _ = pushing
    (hidden / "rbench-hidden.toml").unlink()
    commit(repo, {"b.txt": "x\n"})
    code, err = pre_push(repo, monkeypatch, capsys)
    assert code == 1
    assert "does not exist" in err and str(hidden) not in err


def test_installed_hook_blocks_a_real_push(pushing, tmp_path):
    repo, hidden, _ = pushing
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m rbench.tasks.guard pre-push "$@"\n')
    hook.chmod(0o755)
    commit(repo, {"b.txt": f"{HID}\n"})
    env = {**os.environ, HIDDEN_DIR_ENV: str(hidden)}
    pushed = subprocess.run(
        ["git", "-C", str(repo), "push", "origin", "main"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert pushed.returncode != 0
    assert f"adds hidden task id {HID}" in pushed.stderr
    remote_head = git(tmp_path / "remote.git", "rev-parse", "main")
    assert remote_head != git(repo, "rev-parse", "HEAD")
