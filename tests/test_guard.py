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
ZERO = "0" * 40
WIDE = ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"]


@pytest.fixture(autouse=True)
def no_outer_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.delenv(HIDDEN_DIR_ENV, raising=False)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *GIT_ID, "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def tracked(repo: Path, files: dict[str, str | bytes]) -> None:
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        data = content.encode() if isinstance(content, str) else content
        (repo / name).write_bytes(data)
        git(repo, "add", "-f", name)


def run_public(repo: Path, monkeypatch, capsys) -> tuple[int, str]:
    monkeypatch.chdir(repo)
    code = main(["public"])
    out = capsys.readouterr()
    assert out.out == ""
    return code, out.err


def synthetic_hidden(tmp_path: Path, task_id: str = HID) -> tuple[Path, str]:
    """A hidden directory holding one synthetic task, and that task's canary."""
    hidden = hidden_dir(tmp_path)
    write_task(hidden, task_id, "hidden")
    return hidden, json.loads((hidden / task_id / GROUND_TRUTH_FILE).read_text())["canary"]


# Public check


def test_public_check_names_the_file_with_a_hidden_id_but_not_the_id(tmp_path, monkeypatch, capsys):
    repo = git_repo(tmp_path / "repo")
    tracked(repo, {"README.md": "ok\n", "docs/notes.md": f"intro\nsee tasks/{HID}/task.json\n"})
    code, err = run_public(repo, monkeypatch, capsys)
    assert code == 1
    assert "docs/notes.md:2: a task id with the hidden prefix" in err
    assert HID not in err and "README.md" not in err


@pytest.mark.parametrize(
    "suffix", ["7", "a", "_review36", ".x", "-y", "index", "leak1.", "a_b-c.d"]
)
def test_public_check_flags_what_the_loader_accepts_after_the_prefix(tmp_path, suffix):
    repo = git_repo(tmp_path / "repo")
    tracked(repo, {"t.py": f'x = ("{HIDDEN_ID_PREFIX}{suffix}")'})
    assert public_findings(repo) == ["t.py:1: a task id with the hidden prefix"]


@pytest.mark.parametrize("before", ["run_", "tasks.", "with-", "/", "é"])
def test_public_check_flags_an_id_after_punctuation(tmp_path, before):
    repo = git_repo(tmp_path / "repo")
    tracked(repo, {"t.txt": f"{before}{HID}"})
    assert public_findings(repo) == ["t.txt:1: a task id with the hidden prefix"]


def test_public_check_flags_a_punctuation_id_the_loader_accepts(tmp_path):
    task_id = f"{HIDDEN_ID_PREFIX}_review36"
    hidden, _ = synthetic_hidden(tmp_path, task_id)
    assert (hidden / task_id).is_dir()
    repo = git_repo(tmp_path / "repo")
    tracked(repo, {"id.txt": task_id, f"tasks/{task_id}/task.json": "{}"})
    assert public_findings(repo) == [
        "id.txt:1: a task id with the hidden prefix",
        "tasks/<redacted>/task.json: the path holds a task id or value from the hidden directory",
    ]


@pytest.mark.parametrize("encoding", WIDE)
def test_public_check_scans_utf16_and_utf32(tmp_path, encoding):
    repo = git_repo(tmp_path / "repo")
    tracked(repo, {"wide.txt": f"line one\nsee {HID}\n".encode(encoding)})
    assert public_findings(repo) == ["wide.txt:2: a task id with the hidden prefix"]


def test_public_check_redacts_hidden_paths_and_symlinks(tmp_path, monkeypatch, capsys):
    repo = git_repo(tmp_path / "repo")
    tracked(repo, {f"tasks/{HID}/task.json": "{}", f"runs/{HID}/ledger.json": "{}"})
    (repo / "link").symlink_to(f"tasks/{HID}")
    git(repo, "add", "link")
    code, err = run_public(repo, monkeypatch, capsys)
    assert code == 1
    assert err.splitlines() == [
        "publication guard: link:1: a task id with the hidden prefix",
        (
            "publication guard: runs/<redacted>/ledger.json: unpublished run output;"
            " results go out via rbench publish"
        ),
        (
            "publication guard: tasks/<redacted>/task.json: the path holds a task id or value"
            " from the hidden directory"
        ),
    ]
    assert HID not in err


def test_public_check_flags_tracked_run_output(tmp_path, monkeypatch, capsys):
    repo = git_repo(tmp_path / "repo")
    tracked(repo, {"runs/r1/ledger.json": "{}"})  # forced past the /runs/ ignore rule
    code, err = run_public(repo, monkeypatch, capsys)
    assert code == 1
    assert "runs/r1/ledger.json: unpublished run output" in err


def test_public_check_scans_staged_content(tmp_path):
    repo = git_repo(tmp_path / "repo")
    tracked(repo, {"staged.txt": f"{HID}\n", "both.txt": f"{HID}\n"})
    (repo / "staged.txt").write_text("clean\n")
    (repo / "unstaged.txt").write_text("clean\n")
    git(repo, "add", "unstaged.txt")
    (repo / "unstaged.txt").write_text(f"{HID}\n")
    assert public_findings(repo) == [
        "both.txt:1: a task id with the hidden prefix",
        "staged.txt:1 (staged): a task id with the hidden prefix",
        "unstaged.txt:1: a task id with the hidden prefix",
    ]


def test_public_check_passes_prose_the_bare_prefix_and_placeholders(tmp_path, monkeypatch, capsys):
    repo = git_repo(tmp_path / "repo")
    p = HIDDEN_ID_PREFIX
    prose = (
        "A high-level, push-ready branch-switch; OH-1.\n"
        f"Hidden ids look like `{p}*`, {p}<n> or `{p}`, never real ones.\n"
        f"The prefix {p} alone, then {p}\tand runs/ in prose; it ends the file: {p}"
    )
    tracked(repo, {"docs/a.md": prose, "src/runs/keep.py": "", "tests/runs.py": ""})
    assert run_public(repo, monkeypatch, capsys) == (0, "")


def test_local_public_check_with_a_hidden_directory_scans_its_values(tmp_path, monkeypatch, capsys):
    hidden, canary = synthetic_hidden(tmp_path)
    repo = git_repo(tmp_path / "repo")
    tracked(
        repo,
        {
            "a.txt": f"x\n{canary}\n",
            "b.txt": f"bare {HIDDEN}\n".encode("utf-16"),
            f"c-{HIDDEN}.txt": "",
            "d.txt": "clean\n",
        },
    )
    monkeypatch.setenv(HIDDEN_DIR_ENV, str(hidden))
    code, err = run_public(repo, monkeypatch, capsys)
    assert code == 1
    assert err.splitlines() == [
        "publication guard: a.txt:2: a value from the hidden directory",
        "publication guard: b.txt:1: a value from the hidden directory",
        (
            "publication guard: c-<redacted>.txt: the path holds a task id or value from the"
            " hidden directory"
        ),
    ]
    assert HIDDEN not in err and canary not in err


def test_local_public_check_with_an_unreadable_hidden_directory_fails(
    tmp_path, monkeypatch, capsys
):
    hidden, _ = synthetic_hidden(tmp_path)
    (hidden / "rbench-hidden.toml").unlink()
    repo = git_repo(tmp_path / "repo")
    tracked(repo, {"a.txt": "clean\n"})
    monkeypatch.setenv(HIDDEN_DIR_ENV, str(hidden))
    code, err = run_public(repo, monkeypatch, capsys)
    assert code == 1
    assert "does not exist" in err and str(hidden) not in err


# Pre-push check


@pytest.fixture
def pushing(tmp_path, monkeypatch):
    """A public checkout with a pushed main, and a hidden directory holding one synthetic task."""
    hidden, canary = synthetic_hidden(tmp_path)
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


def commit(repo: Path, files: dict[str, str | bytes], message: str = "change") -> str:
    tracked(repo, files)
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def remote_head(repo: Path, ref: str = "refs/heads/main") -> str:
    """The destination's advertised value of `ref`, as git passes it to the hook."""
    out = git(repo, "ls-remote", "origin", ref)
    return out.split()[0] if out else ZERO


def pre_push(monkeypatch, capsys, *lines: str) -> tuple[int, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(f"{line}\n" for line in lines)))
    code = main(["pre-push", "origin", "unused"])
    out = capsys.readouterr()
    assert out.out == ""
    return code, out.err


def push_main(repo: Path, monkeypatch, capsys) -> tuple[int, str]:
    sha = git(repo, "rev-parse", "HEAD")
    line = f"refs/heads/main {sha} refs/heads/main {remote_head(repo)}"
    return pre_push(monkeypatch, capsys, line)


def test_pre_push_rejects_a_hidden_id_and_names_it(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    commit(repo, {"b.txt": f"see {HID}\n"})
    code, err = push_main(repo, monkeypatch, capsys)
    assert code == 1
    assert f"file b.txt adds hidden task id {HID}" in err


def test_pre_push_rejects_a_canary_and_the_prefix_without_printing_them(
    pushing, monkeypatch, capsys
):
    repo, _, canary = pushing
    commit(repo, {"b.txt": canary})
    commit(repo, {"c.txt": f"prefix {HIDDEN}"})
    code, err = push_main(repo, monkeypatch, capsys)
    assert code == 1
    assert f"adds the canary of hidden task {HID}" in err
    assert "adds the hidden canary prefix" in err
    assert HIDDEN not in err


@pytest.mark.parametrize("encoding", WIDE)
def test_pre_push_scans_wide_encodings(pushing, monkeypatch, capsys, encoding):
    repo, _, canary = pushing
    commit(repo, {"b.txt": f"x\n{HID}\n".encode(encoding), "c.txt": canary.encode(encoding)})
    code, err = push_main(repo, monkeypatch, capsys)
    assert code == 1
    assert f"file b.txt adds hidden task id {HID}" in err
    assert f"file c.txt adds the canary of hidden task {HID}" in err


def test_pre_push_rejects_an_id_a_later_commit_removes(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    commit(repo, {"b.txt": f"{HID}\n"})
    commit(repo, {"b.txt": "gone\n"})
    code, err = push_main(repo, monkeypatch, capsys)
    assert code == 1
    assert err.count(HID) == 1


def test_pre_push_ignores_a_stale_tracking_ref_of_another_remote(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    tip = commit(repo, {"b.txt": f"{HID}\n"})
    git(repo, "update-ref", "refs/remotes/other/topic", tip)
    git(repo, "update-ref", "refs/remotes/origin/main", tip)
    code, err = push_main(repo, monkeypatch, capsys)
    assert code == 1
    assert f"adds hidden task id {HID}" in err


def test_pre_push_rescans_after_the_destination_rewinds(pushing, monkeypatch, capsys, tmp_path):
    repo, _, _ = pushing
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {"b.txt": f"{HID}\n"})
    git(repo, "push", "-q", "origin", "main")  # the tracking ref now holds the commit
    git(tmp_path / "remote.git", "update-ref", "refs/heads/main", base)
    code, err = push_main(repo, monkeypatch, capsys)
    assert code == 1
    assert f"adds hidden task id {HID}" in err


def test_pre_push_scans_a_new_branch_with_a_stale_tracking_ref(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    git(repo, "checkout", "-q", "-b", "topic")
    tip = commit(repo, {"b.txt": f"{HID}\n"})
    git(repo, "update-ref", "refs/remotes/origin/topic", tip)
    code, err = pre_push(monkeypatch, capsys, f"refs/heads/topic {tip} refs/heads/topic {ZERO}")
    assert code == 1
    assert f"adds hidden task id {HID}" in err


def test_pre_push_scans_a_divergent_force_update(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {"a.txt": "public\n"})
    git(repo, "push", "-q", "origin", "main")
    git(repo, "reset", "-q", "--hard", base)
    tip = commit(repo, {"b.txt": f"{HID}\n"})
    old = remote_head(repo)
    code, err = pre_push(monkeypatch, capsys, f"refs/heads/main {tip} refs/heads/main {old}")
    assert code == 1
    assert f"adds hidden task id {HID}" in err


def test_pre_push_checks_messages_and_new_branch_names(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    git(repo, "checkout", "-q", "-b", f"feature/{HID}")
    tip = commit(repo, {}, message=f"- fix {HID}")
    ref = f"refs/heads/feature/{HID}"
    code, err = pre_push(monkeypatch, capsys, f"{ref} {tip} {ref} {ZERO}")
    assert code == 1
    assert f"the ref name {ref} adds hidden task id {HID}" in err
    assert "message adds hidden task id" in err


def test_pre_push_redacts_a_canary_or_prefix_in_a_ref_name(pushing, monkeypatch, capsys):
    repo, _, canary = pushing
    tip = git(repo, "rev-parse", "HEAD")
    lines = [f"refs/heads/main {tip} refs/heads/{name} {ZERO}" for name in (canary, f"x{HIDDEN}")]
    code, err = pre_push(monkeypatch, capsys, *lines)
    assert code == 1
    assert "the ref name refs/heads/<redacted> adds the canary of hidden task" in err
    assert "the ref name refs/heads/x<redacted> adds the hidden canary prefix" in err
    assert HIDDEN not in err


def test_pre_push_ignores_removals_and_unchanged_context(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    commit(repo, {"a.txt": f"one\n{HID}\nthree\n", f"tasks/{HID}.txt": "x\n"})
    git(repo, "push", "-q", "origin", "main")  # already public, before the hook existed
    commit(repo, {"a.txt": f"one\n{HID}\nchanged\n", "d.txt": "high-level\n"})
    git(repo, "rm", "-q", f"tasks/{HID}.txt")
    commit(repo, {})
    commit(repo, {"a.txt": "edited\n"})
    assert push_main(repo, monkeypatch, capsys) == (0, "")


def test_pre_push_checks_new_paths_only(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    commit(repo, {f"tasks/{HID}/note.txt": "clean\n"})
    code, err = push_main(repo, monkeypatch, capsys)
    assert code == 1
    assert f"path tasks/{HID}/note.txt adds hidden task id {HID}" in err


def test_pre_push_scans_an_annotated_tag_message(pushing, monkeypatch, capsys):
    repo, _, canary = pushing
    git(repo, "tag", "-a", "v1", "-m", f"release {canary}")
    tag = git(repo, "rev-parse", "v1")
    code, err = pre_push(monkeypatch, capsys, f"refs/tags/v1 {tag} refs/tags/v1 {ZERO}")
    assert code == 1
    assert f"the annotated tag {tag[:12]} for refs/tags/v1 adds the canary of hidden task" in err
    assert canary not in err


def test_pre_push_allows_a_clean_push_and_a_deletion(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    commit(repo, {"a.txt": "edited\n", "d.txt": "high-level\n"})
    assert push_main(repo, monkeypatch, capsys) == (0, "")
    old = remote_head(repo)
    assert pre_push(monkeypatch, capsys, f"(delete) {ZERO} refs/heads/{HID} {old}") == (0, "")


def test_pre_push_without_a_hidden_directory_notes_and_allows(pushing, monkeypatch, capsys):
    repo, _, _ = pushing
    monkeypatch.delenv(HIDDEN_DIR_ENV)
    commit(repo, {"b.txt": f"{HID}\n"})
    code, err = push_main(repo, monkeypatch, capsys)
    assert code == 0
    assert f"{HIDDEN_DIR_ENV} is not set" in err


def test_pre_push_with_an_unreadable_hidden_directory_rejects(pushing, monkeypatch, capsys):
    repo, hidden, _ = pushing
    (hidden / "rbench-hidden.toml").unlink()
    commit(repo, {"b.txt": "x\n"})
    code, err = push_main(repo, monkeypatch, capsys)
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
    assert git(tmp_path / "remote.git", "rev-parse", "main") != git(repo, "rev-parse", "HEAD")
