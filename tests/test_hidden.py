"""Hidden tasks (spec review-tasks: Hidden tasks stay private).

Every hidden directory, prefix and task here is synthetic and built in tmp_path.
"""

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest
from test_tasks import HIDDEN, write_task

from rbench.tasks import (
    GROUND_TRUTH_FILE,
    HIDDEN_CONFIG_FILE,
    HIDDEN_DIR_ENV,
    HIDDEN_ID_PREFIX,
    PUBLIC_CANARY_PREFIX,
    TASK_FILE,
    HiddenConfig,
    TaskError,
    check_hidden_results_dir,
    hidden_config,
    load_hidden_tasks,
    load_task,
    load_task_sets,
    load_tasks,
)

# Built at runtime: the publication guard rejects tracked files holding hidden-prefixed task ids.
HID = f"{HIDDEN_ID_PREFIX}1"
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_outer_repo(tmp_path, monkeypatch):
    # Git must not find a repository above tmp_path.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def git_repo(path: Path, project: str | None = "rbench") -> Path:
    """A git checkout; with `project`, one whose pyproject.toml names that project."""
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".gitignore").write_text("/runs/\n")
    if project:
        (path / "pyproject.toml").write_text(f'[project]\nname = "{project}"\n')
    return path


def hidden_dir(tmp_path: Path, config: str | None = None) -> Path:
    path = tmp_path / "private"
    path.mkdir()
    if config is None:
        config = f'canary_prefix = "{HIDDEN}"\n'
    (path / HIDDEN_CONFIG_FILE).write_text(config)
    return path


def config(path: Path, tmp_path: Path) -> HiddenConfig:
    """The configuration as rbench run from a public checkout, installed outside any checkout."""
    public = tmp_path / "public"
    if not public.exists():
        git_repo(public)
    return hidden_config(
        {HIDDEN_DIR_ENV: str(path)}, cwd=public, package_dir=tmp_path / "venv" / "rbench"
    )


def assert_private(error: pytest.ExceptionInfo, *secrets: str) -> None:
    """Neither the message nor anything chained to it names a secret."""
    for exc in (error.value, error.value.__cause__, error.value.__context__):
        for secret in (HIDDEN, HID, *secrets):
            assert secret not in str(exc)


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
    assert_private(e, str(tmp_path))


def test_missing_config_file_refuses_and_says_how(tmp_path):
    path = hidden_dir(tmp_path)
    (path / HIDDEN_CONFIG_FILE).unlink()
    with pytest.raises(TaskError, match=f"{HIDDEN_CONFIG_FILE} does not exist") as e:
        config(path, tmp_path)
    assert_how_to_configure(str(e.value))
    assert_private(e, str(tmp_path))


def test_config_reads_directory_and_prefix_and_keeps_both_out_of_repr(tmp_path):
    path = hidden_dir(tmp_path)
    loaded = config(path, tmp_path)
    assert (loaded.path, loaded.canary_prefix) == (path.resolve(), HIDDEN)
    assert HIDDEN not in repr(loaded) and str(tmp_path) not in repr(loaded)


def test_relative_and_home_paths_resolve(tmp_path, monkeypatch):
    path = hidden_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert config(Path("private"), tmp_path).path == path.resolve()
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config(Path("~/private"), tmp_path).path == path.resolve()


def test_hidden_directory_may_be_its_own_git_repository(tmp_path):
    path = hidden_dir(tmp_path)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    assert config(path, tmp_path).path == path.resolve()


def overlap(hidden: Path, cwd: Path, package_dir: Path):
    hidden.mkdir(parents=True, exist_ok=True)
    (hidden / HIDDEN_CONFIG_FILE).write_text(f'canary_prefix = "{HIDDEN}"\n')
    with pytest.raises(TaskError, match="overlaps the git checkout") as e:
        hidden_config({HIDDEN_DIR_ENV: str(hidden)}, cwd=cwd, package_dir=package_dir)
    assert_private(e, str(hidden))


def test_directory_inside_the_checkout_rbench_runs_from_refuses(tmp_path):
    # A non-editable install: the package lives in an environment outside every checkout.
    public = git_repo(tmp_path / "public")
    venv = tmp_path / "venv" / "site-packages" / "rbench" / "tasks"
    venv.mkdir(parents=True)
    overlap(public / "private", cwd=public, package_dir=venv)
    overlap(public / "src" / "private", cwd=public / "src", package_dir=venv)


def test_directory_inside_the_checkout_rbench_is_installed_from_refuses(tmp_path):
    public = git_repo(tmp_path / "public")
    package = public / ".venv" / "site-packages" / "rbench" / "tasks"
    package.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    overlap(public / "private", cwd=elsewhere, package_dir=package)


def test_directory_holding_the_checkout_refuses(tmp_path):
    public = git_repo(tmp_path / "outer" / "public")
    overlap(tmp_path / "outer", cwd=public, package_dir=tmp_path / "venv")


def test_default_package_dir_is_this_checkout(tmp_path):
    with pytest.raises(TaskError, match="overlaps the git checkout"):
        hidden_config({HIDDEN_DIR_ENV: str(REPO / "src")}, cwd=tmp_path)


def test_checkout_case_alias_refuses(tmp_path):
    public = git_repo(tmp_path / "public")
    if not (tmp_path / "PUBLIC").exists():
        pytest.skip("case-sensitive filesystem")
    overlap(tmp_path / "PUBLIC" / "private", cwd=public, package_dir=tmp_path / "venv")


def test_no_known_checkout_refuses(tmp_path):
    path = hidden_dir(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(TaskError, match="cannot find the rbench checkout") as e:
        hidden_config({HIDDEN_DIR_ENV: str(path)}, cwd=elsewhere, package_dir=elsewhere)
    assert "run rbench from its checkout" in str(e.value)


@pytest.mark.parametrize("project", [None, "other", "[broken"])
def test_unrelated_checkout_does_not_count_as_the_public_one(tmp_path, project):
    # A non-editable install run from another checkout, the hidden directory inside rbench's.
    rbench = git_repo(tmp_path / "public")
    hidden = rbench / "private"
    hidden.mkdir()
    (hidden / HIDDEN_CONFIG_FILE).write_text(f'canary_prefix = "{HIDDEN}"\n')
    other = git_repo(tmp_path / "other", project=None if project == "[broken" else project)
    if project == "[broken":
        (other / "pyproject.toml").write_text("[broken")
    with pytest.raises(TaskError, match="cannot find the rbench checkout"):
        hidden_config({HIDDEN_DIR_ENV: str(hidden)}, cwd=other, package_dir=tmp_path / "venv")


def test_hidden_repository_as_working_directory_is_not_the_public_one(tmp_path):
    path = hidden_dir(tmp_path)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    rbench = git_repo(tmp_path / "public")
    package = rbench / "src" / "rbench" / "tasks"
    package.mkdir(parents=True)
    assert hidden_config({HIDDEN_DIR_ENV: str(path)}, cwd=path, package_dir=package).path == (
        path.resolve()
    )


def loop(path: Path) -> None:
    path.unlink(missing_ok=True)
    path.symlink_to(path.name)


def test_link_loops_refuse_without_naming_the_path(tmp_path):
    root = tmp_path / "looped"
    loop(root)
    with pytest.raises(TaskError, match="not a directory") as e:
        config(root, tmp_path)
    assert_private(e, str(tmp_path))
    path = hidden_dir(tmp_path)
    loop(path / HIDDEN_CONFIG_FILE)
    with pytest.raises(
        TaskError, match="resolves outside the hidden directory, or not at all"
    ) as e:
        config(path, tmp_path)
    assert_private(e, str(tmp_path))


@pytest.mark.parametrize("file", [TASK_FILE, GROUND_TRUTH_FILE])
def test_task_file_link_loop_refuses_without_naming_the_path(tmp_path, file):
    path = hidden_dir(tmp_path)
    loaded = config(path, tmp_path)
    loop(write_task(path, HID, task_set="hidden") / file)
    with pytest.raises(TaskError, match=f"{file} resolves outside the hidden directory") as e:
        load_hidden_tasks(loaded)
    assert_private(e, str(tmp_path))


@pytest.mark.parametrize(
    "text, message",
    [
        ("canary_prefix = ?\n", r"is not valid TOML \(at line 1, column 17\)"),
        (f"[{HIDDEN}]\n[{HIDDEN}]\n", r"is not valid TOML \(at line 2, column"),
        (b"\xff", "is not UTF-8"),
        ("", "exactly one key"),
        (f'canary_prefix = "{HIDDEN}"\nextra = 1\n', "exactly one key"),
        (f'"{HIDDEN}" = 1\n', "exactly one key"),
        ("canary_prefix = 1\n", "non-empty string"),
        (f'canary_prefix = "{PUBLIC_CANARY_PREFIX}x"\n', "differ from the public prefix"),
    ],
)
def test_bad_config_refuses_without_quoting_it(tmp_path, text, message):
    path = hidden_dir(tmp_path)
    file = path / HIDDEN_CONFIG_FILE
    file.write_bytes(text) if isinstance(text, bytes) else file.write_text(text)
    with pytest.raises(TaskError, match=message) as e:
        config(path, tmp_path)
    assert_private(e, str(tmp_path))
    assert e.value.__cause__ is None and e.value.__context__ is None


def outside_file(tmp_path: Path, name: str, content: str) -> Path:
    target = tmp_path / "outside" / name
    target.parent.mkdir(exist_ok=True)
    target.write_text(content)
    return target


def test_config_file_linked_out_of_the_directory_refuses(tmp_path):
    path = hidden_dir(tmp_path)
    target = outside_file(tmp_path, HIDDEN_CONFIG_FILE, f'canary_prefix = "{HIDDEN}"\n')
    (path / HIDDEN_CONFIG_FILE).unlink()
    (path / HIDDEN_CONFIG_FILE).symlink_to(target)
    with pytest.raises(TaskError, match="resolves outside the hidden directory"):
        config(path, tmp_path)


@pytest.mark.parametrize("link", ["task_dir", TASK_FILE, GROUND_TRUTH_FILE])
def test_task_input_linked_out_of_the_directory_refuses(tmp_path, link):
    path = hidden_dir(tmp_path)
    loaded = config(path, tmp_path)
    # A complete, valid task outside the directory, reached through a link.
    source = write_task(tmp_path / "outside", HID, task_set="hidden")
    if link == "task_dir":
        (path / HID).symlink_to(source, target_is_directory=True)
    else:
        write_task(path, HID, task_set="hidden")
        (path / HID / link).unlink()
        (path / HID / link).symlink_to(source / link)
    with pytest.raises(TaskError, match="resolves outside the hidden directory") as e:
        load_hidden_tasks(loaded)
    assert_private(e, str(tmp_path))


def test_link_inside_the_directory_loads(tmp_path):
    path = hidden_dir(tmp_path)
    source = write_task(path / ".store", HID, task_set="hidden")
    (path / HID).symlink_to(source, target_is_directory=True)
    assert [t.set for t in load_hidden_tasks(config(path, tmp_path))] == ["hidden"]


def test_inside_resolves_archives_and_refuses_links_out(tmp_path):
    path = hidden_dir(tmp_path)
    loaded = config(path, tmp_path)
    assert loaded.inside("archives/a.tar.gz") == path.resolve() / "archives" / "a.tar.gz"
    (path / "out").symlink_to(outside_file(tmp_path, "a.tar.gz", ""))
    for relative in ("out", "../outside/a.tar.gz", str(tmp_path / "outside" / "a.tar.gz")):
        with pytest.raises(TaskError, match="resolves outside it"):
            loaded.inside(relative)


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


def test_hidden_loads_only_through_an_issued_configuration(tmp_path):
    path = hidden_dir(tmp_path)
    write_task(path, HID, task_set="hidden")
    issued = config(path, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    write_task(elsewhere, HID, task_set="hidden")
    for forged in (HiddenConfig(elsewhere, HIDDEN), dataclasses.replace(issued, path=elsewhere)):
        with pytest.raises(TaskError, match="load only through hidden_config"):
            load_hidden_tasks(forged)
        with pytest.raises(TaskError, match="load only through hidden_config"):
            load_task_sets(hidden=forged)
    with pytest.raises(TypeError):
        load_tasks(elsewhere, hidden_canary_prefix=HIDDEN)


def test_hidden_task_in_a_public_set_refuses_without_naming_it(tmp_path):
    public = tmp_path / "ours"
    write_task(public, HID, task_set="hidden")
    path = hidden_dir(tmp_path)
    for hidden in (None, config(path, tmp_path)):
        with pytest.raises(
            TaskError, match=f"read only from the directory {HIDDEN_DIR_ENV}|load only"
        ) as e:
            load_task_sets(public, hidden=hidden)
        assert_private(e)


def test_hidden_directory_given_as_a_public_set_refuses(tmp_path):
    path = hidden_dir(tmp_path)
    write_task(path, HID, task_set="hidden")
    with pytest.raises(TaskError, match="argument 0, entry 0, is a hidden task") as e:
        load_task_sets(path)
    assert_private(e, str(tmp_path))
    with pytest.raises(TaskError, match="the task directory given is a hidden task") as e:
        load_task(path / HID)
    assert_private(e, str(tmp_path))


def test_public_task_in_the_hidden_directory_refuses(tmp_path):
    path = hidden_dir(tmp_path)
    write_task(path, "t-1")
    with pytest.raises(TaskError, match="holds only hidden tasks") as e:
        load_task_sets(hidden=config(path, tmp_path))
    assert_private(e, str(tmp_path))


def break_task(task_dir: Path, case: str) -> None:
    meta, gt = task_dir / TASK_FILE, task_dir / GROUND_TRUTH_FILE
    if case == "missing ground truth":
        gt.unlink()
        return
    data = json.loads((meta if case.startswith("meta") else gt).read_text())
    if case == "meta: pr":
        data["pr"] = HID
    elif case == "meta: id":
        data["id"] = f"{HID}x"
    elif case == "meta: unknown key":
        data[HIDDEN] = 1
    elif case == "item: file":
        data["items"][0]["file"] = f"/{HIDDEN}"
    elif case == "item: repeated":
        data["items"].append(data["items"][0])
    (meta if case.startswith("meta") else gt).write_text(json.dumps(data))


@pytest.mark.parametrize(
    "case",
    [
        "missing ground truth",
        "meta: pr",
        "meta: id",
        "meta: unknown key",
        "item: file",
        "item: repeated",
    ],
)
def test_malformed_hidden_task_errors_name_a_position_only(tmp_path, case):
    path = hidden_dir(tmp_path)
    break_task(write_task(path, HID, task_set="hidden"), case)
    with pytest.raises(TaskError, match=f"^{HIDDEN_DIR_ENV} entry 0") as e:
        load_hidden_tasks(config(path, tmp_path))
    assert_private(e, str(tmp_path), "i1")


def test_hidden_task_repr_names_nothing(tmp_path):
    path = hidden_dir(tmp_path)
    write_task(path, HID, task_set="hidden")
    (task,) = load_hidden_tasks(config(path, tmp_path))
    assert repr(task) == "Task(set='hidden')"


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


def test_results_dir_outside_any_work_tree_refuses(tmp_path):
    with pytest.raises(TaskError, match="path git ignores"):
        check_hidden_results_dir(tmp_path / "loose" / "r1")


def test_this_checkout_ignores_runs(monkeypatch):
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES")
    check_hidden_results_dir(REPO / "runs" / "r1")
