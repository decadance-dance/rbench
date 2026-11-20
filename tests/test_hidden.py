"""Hidden tasks (spec review-tasks: Hidden tasks stay private).

Every hidden directory, prefix and task here is synthetic and built in tmp_path.
"""

import subprocess
from pathlib import Path

import pytest
from test_tasks import HIDDEN, write_task

from rbench.tasks import (
    HIDDEN_CONFIG_FILE,
    HIDDEN_DIR_ENV,
    HIDDEN_ID_PREFIX,
    PUBLIC_CANARY_PREFIX,
    TaskError,
    check_hidden_results_dir,
    hidden_config,
    load_task_sets,
)
from rbench.tasks.hidden import PUBLIC_ROOT

# Built at runtime: the publication guard rejects tracked files holding hidden-prefixed task ids.
HID = f"{HIDDEN_ID_PREFIX}1"


def hidden_dir(tmp_path: Path, config: str | None = None) -> Path:
    path = tmp_path / "private"
    path.mkdir()
    if config is None:
        config = f'canary_prefix = "{HIDDEN}"\n'
    (path / HIDDEN_CONFIG_FILE).write_text(config)
    return path


def config(path: Path, tmp_path: Path):
    return hidden_config({HIDDEN_DIR_ENV: str(path)}, public_root=tmp_path / "repo")


def assert_how_to_configure(message: str) -> None:
    assert HIDDEN_DIR_ENV in message and HIDDEN_CONFIG_FILE in message


@pytest.mark.parametrize("environ", [{}, {HIDDEN_DIR_ENV: ""}, {HIDDEN_DIR_ENV: "  "}])
def test_hidden_selected_without_directory_refuses_and_says_how(environ):
    with pytest.raises(TaskError, match="no hidden directory is configured") as e:
        hidden_config(environ)
    assert_how_to_configure(str(e.value))


def test_missing_directory_refuses_and_says_how(tmp_path):
    with pytest.raises(TaskError, match="not a directory") as e:
        config(tmp_path / "nowhere", tmp_path)
    assert_how_to_configure(str(e.value))


def test_missing_config_file_refuses_and_says_how(tmp_path):
    path = hidden_dir(tmp_path)
    (path / HIDDEN_CONFIG_FILE).unlink()
    with pytest.raises(TaskError, match=f"has no {HIDDEN_CONFIG_FILE}") as e:
        config(path, tmp_path)
    assert_how_to_configure(str(e.value))


def test_config_reads_directory_and_prefix_and_keeps_prefix_out_of_repr(tmp_path):
    path = hidden_dir(tmp_path)
    loaded = config(path, tmp_path)
    assert (loaded.path, loaded.canary_prefix) == (path.resolve(), HIDDEN)
    assert HIDDEN not in repr(loaded)


def test_relative_and_home_paths_resolve(tmp_path, monkeypatch):
    path = hidden_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert config(Path("private"), tmp_path).path == path.resolve()
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config(Path("~/private"), tmp_path).path == path.resolve()


@pytest.mark.parametrize("inside", [True, False])
def test_directory_overlapping_public_repository_refuses(tmp_path, inside):
    repo = tmp_path / "repo"
    path = repo / "private" if inside else tmp_path
    path.mkdir(parents=True, exist_ok=True)
    (path / HIDDEN_CONFIG_FILE).write_text(f'canary_prefix = "{HIDDEN}"\n')
    with pytest.raises(TaskError, match="overlaps the public repository"):
        hidden_config({HIDDEN_DIR_ENV: str(path)}, public_root=repo)


def test_default_public_root_is_this_checkout():
    assert (PUBLIC_ROOT / "src" / "rbench" / "tasks" / "hidden.py").is_file()
    with pytest.raises(TaskError, match="overlaps the public repository"):
        hidden_config({HIDDEN_DIR_ENV: str(PUBLIC_ROOT / "src")})


@pytest.mark.parametrize(
    "text, message",
    [
        ("canary_prefix = ", "cannot read"),
        (b"\xff", "cannot read"),
        ("", "exactly one key"),
        (f'canary_prefix = "{HIDDEN}"\nextra = 1\n', "exactly one key"),
        (f'"{HIDDEN}" = 1\n', "exactly one key"),
        ("canary_prefix = 1\n", "non-empty string"),
        (f'canary_prefix = "{PUBLIC_CANARY_PREFIX}x"\n', "differ from the public prefix"),
    ],
)
def test_bad_config_refuses_without_quoting_the_prefix(tmp_path, text, message):
    path = hidden_dir(tmp_path)
    file = path / HIDDEN_CONFIG_FILE
    file.write_bytes(text) if isinstance(text, bytes) else file.write_text(text)
    with pytest.raises(TaskError, match=message) as e:
        config(path, tmp_path)
    # Nothing chained either: a traceback must not carry the file's text.
    assert HIDDEN not in str(e.value) and e.value.__cause__ is None
    assert e.value.__context__ is None or e.value.__suppress_context__


def test_loads_public_sets_and_hidden_tasks_from_the_hidden_directory(tmp_path):
    public = tmp_path / "ours"
    write_task(public, "t-1")
    path = hidden_dir(tmp_path)
    write_task(path, HID, task_set="hidden")
    tasks = load_task_sets(public, hidden=config(path, tmp_path))
    assert [(t.id, t.set, t.path.parent) for t in tasks] == [
        (HID, "hidden", path.resolve()),
        ("t-1", "ours", public),
    ]
    assert [t.id for t in load_task_sets(public)] == ["t-1"]


def test_hidden_task_in_a_public_set_refuses(tmp_path):
    public = tmp_path / "ours"
    write_task(public, HID, task_set="hidden")
    path = hidden_dir(tmp_path)
    for hidden in (None, config(path, tmp_path)):
        with pytest.raises(TaskError, match=f"read only from the directory {HIDDEN_DIR_ENV} names"):
            load_task_sets(public, hidden=hidden)


def test_hidden_directory_given_as_a_public_set_refuses(tmp_path):
    path = hidden_dir(tmp_path)
    write_task(path, HID, task_set="hidden")
    with pytest.raises(TaskError, match="outside the hidden directory"):
        load_task_sets(path)


def test_public_task_in_the_hidden_directory_refuses(tmp_path):
    path = hidden_dir(tmp_path)
    write_task(path, HID, task_set="hidden")
    write_task(path, "t-1")
    with pytest.raises(TaskError, match="holds only hidden tasks"):
        load_task_sets(hidden=config(path, tmp_path))


def git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".gitignore").write_text("/runs/\n")
    return path


def test_results_dir_ignored_by_git_passes(tmp_path):
    repo = git_repo(tmp_path / "repo")
    check_hidden_results_dir(repo / "runs" / "r1")
    (repo / "runs" / "r1").mkdir(parents=True)
    check_hidden_results_dir(repo / "runs" / "r1")


@pytest.mark.parametrize("where", ["out/r1", ".", "runs-copy/r1"])
def test_results_dir_not_ignored_by_git_refuses(tmp_path, where):
    repo = git_repo(tmp_path / "repo")
    with pytest.raises(TaskError, match="path git ignores"):
        check_hidden_results_dir(repo / where)


def test_results_dir_outside_any_work_tree_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(TaskError, match="path git ignores"):
        check_hidden_results_dir(tmp_path / "loose" / "r1")


def test_this_checkout_ignores_runs():
    check_hidden_results_dir(PUBLIC_ROOT / "runs" / "r1")
