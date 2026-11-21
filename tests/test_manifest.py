"""Task-set manifests, content hashes and the core list (spec review-tasks: Task set versions,
Core task subset; design D11).

Every task here is synthetic and built in tmp_path.
"""

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from test_hidden import config as hidden_config_for
from test_tasks import HID, HIDDEN, item, load_hidden, write_task

from rbench.tasks import (
    CORE_LIST_FILE,
    GROUND_TRUTH_FILE,
    HIDDEN_CONFIG_FILE,
    HIDDEN_DIR_ENV,
    MANIFEST_FILE,
    TASK_FILE,
    ChangelogEntry,
    HiddenConfig,
    Manifest,
    ManifestTask,
    TaskError,
    check_repository_tasks,
    check_version_history,
    load_committed_manifest,
    load_core_list,
    load_hidden_task_set,
    load_hidden_tasks,
    load_task_set,
    load_tasks,
    merge_base,
    select_core,
    task_content_sha256,
)
from rbench.tasks.archive import SourceArchive
from rbench.tasks.manifest import manifest_task

ROOT = Path(__file__).resolve().parent.parent
REPO_TASKS = ROOT / "tasks"


def hashes(set_dir: Path, hidden: bool = False) -> dict[str, str]:
    tasks = load_hidden(set_dir) if hidden else load_tasks(set_dir)
    return {task.id: task_content_sha256(task) for task in tasks}


def hidden_set(set_dir: Path) -> HiddenConfig:
    """The configuration of a synthetic hidden set directory, as `hidden_config` issues it."""
    (set_dir / HIDDEN_CONFIG_FILE).write_text(f'canary_prefix = "{HIDDEN}"\n')
    return hidden_config_for(set_dir, set_dir.parent)


def assert_private(err: pytest.ExceptionInfo, *secrets: str) -> None:
    for exc in (err.value, err.value.__cause__, err.value.__context__):
        assert not any(secret in str(exc) for secret in secrets), str(exc)


def write_manifest(set_dir: Path, task_set: str = "ours", changelog=None, **overrides) -> Path:
    """A version 1 manifest of every task in `set_dir`, as an import would record it."""
    current = hashes(set_dir, hidden=task_set == "hidden")
    data = {
        "set": task_set,
        "version": 1,
        "tasks": {task_id: {"sha256": sha} for task_id, sha in current.items()},
        "changelog": changelog or [{"version": 1, "note": "Initial import.", "changed": current}],
        **overrides,
    }
    path = set_dir / MANIFEST_FILE
    path.write_text(json.dumps(data, indent=2))
    return path


def edit(path: Path, change) -> None:
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def two_task_set(tmp_path: Path) -> Path:
    set_dir = tmp_path / "ours"
    write_task(set_dir, "t-1")
    write_task(set_dir, "t-2")
    write_manifest(set_dir)
    return set_dir


@pytest.mark.parametrize(
    ("name", "change"),
    [
        ("repo", lambda d: d.update(repo="example/other")),
        ("pr", lambda d: d.update(pr=2)),
        ("head", lambda d: d.update(head="d" * 40)),
        ("language", lambda d: d.update(language="Go")),
        ("title", lambda d: d.update(title="Fix the loop bound")),
        ("description", lambda d: d.update(description="Fixes #1.")),
        ("archive_sha256", lambda d: d.update(archive_sha256="e" * 64)),
    ],
)
def test_hash_covers_metadata_description_and_archive_hash(tmp_path: Path, name, change) -> None:
    task_dir = write_task(tmp_path, "t-1")
    before = hashes(tmp_path)["t-1"]
    edit(task_dir / TASK_FILE, change)
    assert hashes(tmp_path)["t-1"] != before, name


@pytest.mark.parametrize(
    "change",
    [
        lambda gt: gt["items"][0].update(message="Off-by-one in the upper bound."),
        lambda gt: gt["items"][0].update(severity="should"),
        lambda gt: gt["items"][0].update(end_line=6),
        lambda gt: gt["items"].append(item(id="i2")),
        lambda gt: gt.update(canary=gt["canary"][:-1] + "f"),
    ],
)
def test_hash_covers_ground_truth(tmp_path: Path, change) -> None:
    task_dir = write_task(tmp_path, "t-1")
    before = hashes(tmp_path)["t-1"]
    edit(task_dir / GROUND_TRUTH_FILE, change)
    assert hashes(tmp_path)["t-1"] != before


def test_hash_ignores_formatting_and_key_order(tmp_path: Path) -> None:
    task_dir = write_task(tmp_path, "t-1")
    before = hashes(tmp_path)["t-1"]
    for name in (TASK_FILE, GROUND_TRUTH_FILE):
        data = json.loads((task_dir / name).read_text())
        reordered = dict(reversed(list(data.items())))
        (task_dir / name).write_text(json.dumps(reordered, indent=4) + "\n")
    assert hashes(tmp_path)["t-1"] == before


def test_set_matching_its_manifest_loads(tmp_path: Path) -> None:
    set_dir = two_task_set(tmp_path)
    edit(set_dir / MANIFEST_FILE, lambda m: m["tasks"]["t-1"].update(license="MIT"))
    manifest, tasks = load_task_set(set_dir, "ours")
    assert [t.id for t in tasks] == ["t-1", "t-2"]
    assert manifest.version == 1
    assert manifest.tasks["t-1"].license == "MIT"
    assert manifest.tasks["t-2"].license is None
    assert manifest.changelog[0].changed.keys() == {"t-1", "t-2"}


def test_ground_truth_fixed_without_new_version_fails_naming_task(tmp_path: Path) -> None:
    set_dir = two_task_set(tmp_path)
    edit(set_dir / "t-2" / GROUND_TRUTH_FILE, lambda gt: gt["items"][0].update(severity="nit"))
    with pytest.raises(TaskError, match=r"task .*t-2: content hash .* new set version"):
        load_task_set(set_dir, "ours")


def test_new_hash_without_changelog_entry_fails(tmp_path: Path) -> None:
    set_dir = two_task_set(tmp_path)
    edit(set_dir / "t-2" / GROUND_TRUTH_FILE, lambda gt: gt["items"][0].update(severity="nit"))
    new = hashes(set_dir)["t-2"]
    edit(set_dir / MANIFEST_FILE, lambda m: m["tasks"]["t-2"].update(sha256=new))
    with pytest.raises(TaskError, match=r"task 't-2': its hash differs from changelog version 1"):
        load_task_set(set_dir, "ours")


def test_new_version_with_one_changed_task(tmp_path: Path) -> None:
    set_dir = tmp_path / "ours"
    for task_id in ("t-1", "t-2", "t-3"):
        write_task(set_dir, task_id)
    v1 = hashes(set_dir)
    write_manifest(set_dir)

    edit(set_dir / "t-2" / GROUND_TRUTH_FILE, lambda gt: gt["items"][0].update(severity="nit"))
    v2 = hashes(set_dir)
    assert {k for k in v1 if v1[k] != v2[k]} == {"t-2"}

    def bump(m: dict) -> None:
        m["version"] = 2
        m["tasks"]["t-2"]["sha256"] = v2["t-2"]
        m["changelog"].append({"version": 2, "note": "Fix t-2.", "changed": {"t-2": v2["t-2"]}})

    edit(set_dir / MANIFEST_FILE, bump)
    manifest, _ = load_task_set(set_dir, "ours")
    assert manifest.version == 2
    assert list(manifest.changelog[-1].changed) == ["t-2"]
    assert {k: v.sha256 for k, v in manifest.tasks.items()} == v2


def test_removed_task_is_recorded_as_null(tmp_path: Path) -> None:
    set_dir = two_task_set(tmp_path)

    def remove(m: dict) -> None:
        m["version"] = 2
        del m["tasks"]["t-2"]
        m["changelog"].append({"version": 2, "note": "Drop t-2.", "changed": {"t-2": None}})

    edit(set_dir / MANIFEST_FILE, remove)
    (set_dir / "t-2" / TASK_FILE).unlink()
    (set_dir / "t-2" / GROUND_TRUTH_FILE).unlink()
    (set_dir / "t-2").rmdir()
    manifest, tasks = load_task_set(set_dir, "ours")
    assert [t.id for t in tasks] == ["t-1"] and list(manifest.tasks) == ["t-1"]


def test_hidden_set_is_checked_against_its_manifest(tmp_path: Path) -> None:
    set_dir = tmp_path / "hidden"
    write_task(set_dir, HID, task_set="hidden")
    write_manifest(set_dir, task_set="hidden")
    manifest, tasks = load_hidden_task_set(hidden_set(set_dir))
    assert manifest.set == "hidden" and [t.id for t in tasks] == [HID]
    edit(set_dir / HID / GROUND_TRUTH_FILE, lambda gt: gt["items"][0].update(message="Changed."))
    with pytest.raises(TaskError) as err:
        load_hidden_task_set(hidden_set(set_dir))
    message = str(err.value)
    assert "task 1 of 1" in message and "content hash does not match" in message
    assert f"manifest {HIDDEN_DIR_ENV}/{MANIFEST_FILE}" in message
    assert_private(err, HID, HIDDEN, str(tmp_path))


def test_hidden_manifest_errors_name_no_id_or_value(tmp_path: Path) -> None:
    set_dir = tmp_path / "hidden"
    task_dir = write_task(set_dir, HID, task_set="hidden")
    path = write_manifest(set_dir, task_set="hidden")
    secret = json.loads((task_dir / GROUND_TRUTH_FILE).read_text())["canary"]
    good = path.read_text()
    for change in (
        lambda m: m["tasks"][HID].update(sha256=secret),
        lambda m: m["tasks"][HID].update(license=7),
        lambda m: m["tasks"][HID].update({secret: 1}),
        lambda m: m["changelog"][0]["changed"].update({HID: secret}),
        lambda m: m["changelog"][0]["changed"].update({secret: None}),
        lambda m: m["tasks"][HID].update(sha256="0" * 64),
        lambda m: m.update(set=secret),
        lambda m: m.update(version=secret),
    ):
        path.write_text(good)
        edit(path, change)
        with pytest.raises(TaskError) as err:
            load_hidden_task_set(hidden_set(set_dir))
        assert_private(err, HID, secret, str(tmp_path))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda m: m.update(version=2),
            r"'version' is 2, but the last changelog entry is version 1",
        ),
        (lambda m: m["changelog"][0].update(version=2), r"changelog entry 0: 'version' must be 1"),
        (lambda m: m["changelog"][0].update(changed={}), r"'changed' must name at least one task"),
        (lambda m: m["changelog"][0].update(note=" "), r"'note' must be a non-empty string"),
        (lambda m: m.update(changelog=[]), r"'changelog' must be a non-empty list"),
        (lambda m: m["changelog"][0]["changed"].pop("t-2"), r"'t-2': no changelog entry names it"),
        (lambda m: m["tasks"].pop("t-2"), r"'t-2': changelog version 1 records it, but 'tasks'"),
        (lambda m: m.update(set="reviewbench"), r"'set' must be 'ours', the set it is loaded as"),
        (lambda m: m.update(version="1"), r"'version' must be a positive integer"),
        (lambda m: m.update(extra=1), r"1 unknown keys"),
        (lambda m: m["tasks"]["t-1"].update(sha256="A" * 64), r"'sha256' must be 64 lowercase hex"),
        (lambda m: m["tasks"]["t-1"].update(license=""), r"'license' must be a non-empty string"),
        (lambda m: m["tasks"]["t-1"].update(owner="x"), r"task 't-1': 1 unknown keys"),
        (lambda m: m["tasks"].update({"Bad id": m["tasks"]["t-1"]}), r"'tasks' entry 2: invalid"),
        (lambda m: m["tasks"].update({HID: m["tasks"]["t-1"]}), r"'tasks' entry 2: the 'h-' id"),
        (
            lambda m: m["changelog"][0]["changed"].update({HID: None}),
            r"changelog entry 0: 'changed' entry 2: the 'h-' id prefix is reserved",
        ),
        (
            lambda m: m["changelog"][0]["changed"].update({"Bad id": None}),
            r"changelog entry 0: 'changed' entry 2: invalid task id",
        ),
    ],
)
def test_malformed_manifest_is_rejected(tmp_path: Path, change, message) -> None:
    set_dir = two_task_set(tmp_path)
    edit(set_dir / MANIFEST_FILE, change)
    with pytest.raises(TaskError, match=message) as err:
        load_task_set(set_dir, "ours")
    assert "Bad id" not in str(err.value) and HID not in str(err.value)


def test_hidden_set_redaction_comes_from_the_caller(tmp_path: Path) -> None:
    set_dir = tmp_path / "hidden"
    task_dir = write_task(set_dir, HID, task_set="hidden")
    path = write_manifest(set_dir, task_set="hidden")
    gt = task_dir / GROUND_TRUTH_FILE
    secret = json.loads(gt.read_text())["canary"]
    original = path.read_text()
    # The file claims a public set to get its keys quoted; the caller's set decides.
    edit(path, lambda m: m.update(set="ours", tasks={secret: {"sha256": "invalid"}}))
    with pytest.raises(TaskError, match=r"'set' must be 'hidden'") as err:
        load_hidden_task_set(hidden_set(set_dir))
    assert secret not in str(err.value)
    path.write_text(original)
    # The loader's hidden mode names the entry and the field, never the value or the directory.
    edit(gt, lambda d: d["items"][0].update(severity=secret))
    match = rf"{HIDDEN_DIR_ENV} entry 0: ground_truth.json: item 0: 'severity'"
    with pytest.raises(TaskError, match=match) as err:
        load_hidden_task_set(hidden_set(set_dir))
    assert_private(err, HID, secret, str(tmp_path))
    assert err.value.__cause__ is None and err.value.__context__ is None


def test_hidden_set_with_shared_canaries_names_no_task(tmp_path: Path) -> None:
    set_dir = tmp_path / "hidden"
    first = write_task(set_dir, HID, task_set="hidden")
    second = write_task(set_dir, f"{HID}0", task_set="hidden")
    (second / GROUND_TRUTH_FILE).write_text((first / GROUND_TRUTH_FILE).read_text())
    with pytest.raises(TaskError, match=rf"entry 1 has the same canary as {HIDDEN_DIR_ENV}") as err:
        load_hidden_task_set(hidden_set(set_dir))
    assert_private(err, HID, str(tmp_path))


def test_hidden_set_loads_only_through_its_configuration(tmp_path: Path) -> None:
    set_dir = tmp_path / "hidden"
    write_task(set_dir, HID, task_set="hidden")
    write_manifest(set_dir, task_set="hidden")
    with pytest.raises(TaskError, match=r"only through load_hidden_task_set") as err:
        load_task_set(set_dir, "hidden")
    assert_private(err, str(tmp_path))
    with pytest.raises(TaskError, match=r"load only through hidden_config"):
        load_hidden_task_set(HiddenConfig(set_dir, HIDDEN))
    manifest, _ = load_hidden_task_set(hidden_set(set_dir))
    assert repr(manifest) == "Manifest(set='hidden', version=1)"


@pytest.mark.parametrize(
    "case", ["missing", "linked out", "malformed", "undecodable", "unreadable"]
)
def test_hidden_manifest_file_errors_name_no_path(tmp_path: Path, case: str) -> None:
    set_dir = tmp_path / "hidden"
    write_task(set_dir, HID, task_set="hidden")
    path = write_manifest(set_dir, task_set="hidden")
    config = hidden_set(set_dir)
    if case == "missing":
        path.unlink()
    elif case == "linked out":
        outside = tmp_path / "outside.json"
        outside.write_text(path.read_text())
        path.unlink()
        path.symlink_to(outside)
    elif case == "malformed":
        path.write_text(path.read_text()[:-5])
    elif case == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root reads any file")
        path.chmod(0)
    else:
        path.write_bytes(path.read_bytes() + b"\xff")
    with pytest.raises(TaskError) as err:
        load_hidden_task_set(config)
    assert_private(err, HID, str(tmp_path))
    assert err.value.__context__ is None


def test_hidden_committed_manifest_errors_name_no_path(tmp_path: Path) -> None:
    repo = tmp_path / "private"
    write_task(repo, HID, task_set="hidden")
    path = write_manifest(repo, task_set="hidden")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(TaskError, match=r"git cannot read it at that revision") as err:
        load_committed_manifest(repo, "HEAD", path, "hidden")
    assert_private(err, HID, str(tmp_path))
    assert err.value.__context__ is None


def test_hidden_manifest_rejects_public_ids(tmp_path: Path) -> None:
    set_dir = tmp_path / "hidden"
    write_task(set_dir, HID, task_set="hidden")
    path = write_manifest(set_dir, task_set="hidden")
    edit(path, lambda m: m["changelog"][0]["changed"].update({"t-1": None}))
    with pytest.raises(TaskError, match=r"'changed' entry 1: the 'h-' id prefix is reserved"):
        load_hidden_task_set(hidden_set(set_dir))


def test_invalid_unicode_names_task_and_file(tmp_path: Path) -> None:
    task_dir = write_task(tmp_path, "t-1")
    edit(task_dir / TASK_FILE, lambda d: d.update(description=chr(0xD800)))
    (task,) = load_tasks(tmp_path)
    with pytest.raises(TaskError, match=r"task .*t-1: task.json holds a string that is not valid"):
        task_content_sha256(task)


def test_manifest_and_directories_must_agree(tmp_path: Path) -> None:
    set_dir = two_task_set(tmp_path)
    write_task(set_dir, "t-3")
    with pytest.raises(TaskError, match=r"does not list tasks: t-3"):
        load_task_set(set_dir, "ours")

    set_dir = tmp_path / "other"
    write_task(set_dir, "t-1")
    write_manifest(set_dir)
    write_task(tmp_path / "scratch", "t-9")
    sha = hashes(tmp_path / "scratch")["t-9"]

    def add(m: dict) -> None:
        m["tasks"]["t-9"] = {"sha256": sha}
        m["changelog"][0]["changed"]["t-9"] = sha

    edit(set_dir / MANIFEST_FILE, add)
    with pytest.raises(TaskError, match=r"lists tasks with no directory: t-9"):
        load_task_set(set_dir, "ours")


def test_manifest_set_must_match_its_tasks(tmp_path: Path) -> None:
    set_dir = tmp_path / "rb"
    write_task(set_dir, "t-1", task_set="reviewbench", items=[item(source=None)])
    write_manifest(set_dir, task_set="ours")
    with pytest.raises(TaskError, match=r"t-1 is in set 'reviewbench', not 'ours'"):
        load_task_set(set_dir, "ours")


def test_missing_or_repeated_manifest_keys(tmp_path: Path) -> None:
    set_dir = tmp_path / "ours"
    write_task(set_dir, "t-1")
    with pytest.raises(TaskError, match=r"missing file manifest.json"):
        load_task_set(set_dir, "ours")
    path = write_manifest(set_dir)
    path.write_text(path.read_text().replace('"version": 1,', '"version": 1, "version": 1,'))
    with pytest.raises(TaskError, match=r"a key is repeated"):
        load_task_set(set_dir, "ours")


def core_setup(tmp_path: Path, ids: list[str]) -> tuple[Path, list]:
    write_task(tmp_path / "reviewbench", "rb-1", task_set="reviewbench", items=[item(source=None)])
    write_task(tmp_path / "reviewbench", "rb-2", task_set="reviewbench", items=[item(source=None)])
    write_task(tmp_path / "ours", "o-1")
    write_task(tmp_path / "ours", "o-2")
    tasks = load_tasks(tmp_path / "reviewbench", tmp_path / "ours")
    path = tmp_path / CORE_LIST_FILE
    path.write_text(json.dumps({"version": 3, "tasks": ids}))
    return path, tasks


def test_selecting_core_tasks_plans_exactly_those(tmp_path: Path) -> None:
    path, tasks = core_setup(tmp_path, ["rb-2", "o-1"])
    core = load_core_list(path)
    assert core.version == 3
    assert [t.id for t in select_core(core, tasks)] == ["o-1", "rb-2"]


@pytest.mark.parametrize(
    ("ids", "message"),
    [
        (["rb-1", "o-1", "o-9"], r"tasks not loaded: o-9"),
        (["rb-1", "rb-2"], r"must include tasks of both .*; none from ours"),
        (["o-1"], r"none from reviewbench"),
    ],
)
def test_core_list_against_loaded_tasks(tmp_path: Path, ids, message) -> None:
    path, tasks = core_setup(tmp_path, ids)
    with pytest.raises(TaskError, match=message):
        select_core(load_core_list(path), tasks)


def test_core_rejects_an_id_loaded_from_two_sets(tmp_path: Path) -> None:
    path, tasks = core_setup(tmp_path, ["rb-1", "o-1"])
    write_task(tmp_path / "other", "rb-1")
    tasks += load_tasks(tmp_path / "other")
    with pytest.raises(TaskError, match=r"task id 'rb-1' is loaded twice"):
        select_core(load_core_list(path), tasks)


def test_core_names_no_hidden_id_loaded_twice(tmp_path: Path) -> None:
    path, tasks = core_setup(tmp_path, ["rb-1", "o-1"])
    for name in ("h1", "h2"):
        write_task(tmp_path / name, HID, task_set="hidden")
        tasks += load_hidden(tmp_path / name)
    with pytest.raises(TaskError, match=r"two loaded hidden tasks share an id") as err:
        select_core(load_core_list(path), tasks)
    assert HID not in str(err.value)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"version": 1, "tasks": []}, r"'tasks' must be a non-empty list"),
        ({"version": 0, "tasks": ["o-1"]}, r"'version' must be a positive integer"),
        ({"tasks": ["o-1"]}, r"missing version"),
        ({"version": 1, "tasks": ["o-1", "o-1"]}, r"'o-1' is listed twice"),
        ({"version": 1, "tasks": [HID]}, r"hidden tasks cannot be core tasks"),
    ],
)
def test_malformed_core_list_is_rejected(tmp_path: Path, data, message) -> None:
    path = tmp_path / CORE_LIST_FILE
    path.write_text(json.dumps(data))
    with pytest.raises(TaskError, match=message):
        load_core_list(path)


def repo_base() -> str:
    """Where this branch left its base: the PR's base branch in CI, `origin/develop` locally."""
    branch = os.environ.get("GITHUB_BASE_REF")
    ref = os.environ.get("RBENCH_BASE_REF") or (f"origin/{branch}" if branch else "origin/develop")
    return merge_base(ROOT, ref)


def test_committed_task_sets_match_their_manifests() -> None:
    # In CI the merge base is resolved even before any set is committed, so a shallow checkout
    # fails now rather than letting the history check pass for want of a base later.
    if REPO_TASKS.is_dir() or os.environ.get("CI"):
        base = repo_base()
        if REPO_TASKS.is_dir():
            check_repository_tasks(REPO_TASKS, ROOT, base)


def test_repository_check_names_an_edited_task(tmp_path: Path) -> None:
    core_setup(tmp_path, ["rb-1", "o-1"])
    write_manifest(tmp_path / "reviewbench", task_set="reviewbench")
    write_manifest(tmp_path / "ours")
    assert len(check_repository_tasks(tmp_path)) == 4
    edit(tmp_path / "ours" / "o-2" / TASK_FILE, lambda d: d.update(title="Retitled"))
    with pytest.raises(TaskError, match=r"task .*o-2: content hash"):
        check_repository_tasks(tmp_path)
    (tmp_path / "ours" / MANIFEST_FILE).unlink()
    with pytest.raises(TaskError, match=r"missing file manifest.json"):
        check_repository_tasks(tmp_path)


def test_repository_check_accepts_only_public_set_directories(tmp_path: Path) -> None:
    write_task(tmp_path / "extra", "t-1")
    write_manifest(tmp_path / "extra")
    with pytest.raises(TaskError, match=r"holds only the public sets .*, not extra"):
        check_repository_tasks(tmp_path)


def git(repo: Path, *args: str) -> str:
    identity = [
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.test",
        "-c",
        "commit.gpgsign=false",
    ]
    done = subprocess.run(
        ["git", "-C", str(repo), *identity, *args], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def committed_repo(tmp_path: Path) -> tuple[Path, str]:
    """A git repo with a committed version 1 of the `ours` set; returns it and the commit."""
    repo = tmp_path / "repo"
    set_dir = repo / "tasks" / "ours"
    write_task(set_dir, "t-1")
    write_task(set_dir, "t-2")
    write_manifest(set_dir)
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "v1")
    return repo, git(repo, "rev-parse", "HEAD")


def fix_t2(repo: Path) -> str:
    set_dir = repo / "tasks" / "ours"
    edit(set_dir / "t-2" / GROUND_TRUTH_FILE, lambda gt: gt["items"][0].update(severity="nit"))
    return hashes(set_dir)["t-2"]


def test_edit_under_the_committed_version_fails(tmp_path: Path) -> None:
    repo, base = committed_repo(tmp_path)
    new = fix_t2(repo)

    def in_place(m: dict) -> None:
        m["tasks"]["t-2"]["sha256"] = new
        m["changelog"][0]["changed"]["t-2"] = new

    edit(repo / "tasks" / "ours" / MANIFEST_FILE, in_place)
    git(repo, "commit", "-q", "-am", "edit in place")
    with pytest.raises(TaskError, match=r"changelog entry 0 differs .* \(tasks t-2 changed\)"):
        check_repository_tasks(repo / "tasks", repo, base)


def test_append_only_error_gives_a_hidden_task_by_position(tmp_path: Path) -> None:
    changed = {"h-a": "1" * 64, "h-b": "2" * 64}
    entry = ChangelogEntry(1, "Initial import.", changed)
    tasks = {k: ManifestTask(v, None) for k, v in changed.items()}
    base = Manifest("hidden", 1, tasks, (entry,), tmp_path / MANIFEST_FILE)
    edited = replace(entry, changed={**changed, "h-b": "3" * 64})
    with pytest.raises(TaskError, match=r"\('changed' entries 1 changed\)") as err:
        check_version_history(base, replace(base, changelog=(edited,)))
    assert "h-b" not in str(err.value)
    with pytest.raises(TaskError, match=r"\(its note changed\)"):
        check_version_history(base, replace(base, changelog=(replace(entry, note="Other."),)))


def test_symlinked_manifest_is_rejected(tmp_path: Path) -> None:
    repo, base = committed_repo(tmp_path)
    path = repo / "tasks" / "ours" / MANIFEST_FILE
    new = fix_t2(repo)

    def rewrite(m: dict) -> None:
        m["tasks"]["t-2"]["sha256"] = new
        m["changelog"][0]["changed"]["t-2"] = new

    edit(path, rewrite)
    path.rename(repo / "moved-manifest.json")
    path.symlink_to("../../moved-manifest.json")
    with pytest.raises(TaskError, match=r"manifest.json is a symlink"):
        check_repository_tasks(repo / "tasks", repo, base)
    with pytest.raises(TaskError, match=r"is a symlink"):
        load_committed_manifest(repo, base, path, "ours")


@pytest.mark.parametrize("target", ["dir", "task file"])
def test_any_symlink_under_tasks_is_rejected(tmp_path: Path, target: str) -> None:
    repo, _ = committed_repo(tmp_path)
    tasks = repo / "tasks"
    if target == "dir":
        (tasks / "ours").rename(repo / "elsewhere")
        (tasks / "ours").symlink_to("../elsewhere")
    else:
        gt = tasks / "ours" / "t-1" / GROUND_TRUTH_FILE
        gt.rename(repo / "gt.json")
        gt.symlink_to("../../../gt.json")
    with pytest.raises(TaskError, match=r"is a symlink"):
        check_repository_tasks(tasks)


def test_removed_or_renamed_set_fails_naming_it(tmp_path: Path) -> None:
    repo, base = committed_repo(tmp_path)
    tasks = repo / "tasks"
    (tasks / "ours").rename(repo / "parked")
    with pytest.raises(TaskError, match=r"task sets ours exist at .* but not in"):
        check_repository_tasks(tasks, repo, base)
    (repo / "parked").rename(tasks / "reviewbench")
    with pytest.raises(TaskError, match=r"task sets ours exist at"):
        check_repository_tasks(tasks, repo, base)


@pytest.mark.parametrize("ref", ["--independent", "--all", "-p"])
def test_option_like_base_ref_is_a_ref(tmp_path: Path, ref: str) -> None:
    repo, _ = committed_repo(tmp_path)
    with pytest.raises(TaskError, match=r"cannot resolve the base revision"):
        merge_base(repo, ref)
    with pytest.raises(TaskError, match=r"cannot resolve the base revision"):
        check_repository_tasks(repo / "tasks", repo, ref)


def test_merge_base_of_a_full_clone(tmp_path: Path) -> None:
    repo, base = committed_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "topic")
    git(repo, "commit", "-q", "--allow-empty", "-m", "topic")
    assert merge_base(repo, base) == base


def test_shallow_clone_is_refused(tmp_path: Path) -> None:
    repo, base = committed_repo(tmp_path)
    clone = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo}", str(clone))
    assert git(clone, "rev-parse", "--is-shallow-repository") == "true"
    with pytest.raises(TaskError, match=r"is a shallow clone.*fetch the full history"):
        merge_base(clone, base)


def test_changed_hash_needs_a_higher_version(tmp_path: Path) -> None:
    # With both manifests valid, append-only entries already force a bump; this guards the rule
    # itself, for manifests built in code.
    repo, base = committed_repo(tmp_path)
    committed = load_committed_manifest(repo, base, repo / "tasks" / "ours" / MANIFEST_FILE, "ours")
    assert committed is not None
    changed = replace(committed, tasks={**committed.tasks, "t-2": ManifestTask("0" * 64, None)})
    with pytest.raises(TaskError, match=r"tasks changed since the committed version 1 \(t-2\)"):
        check_version_history(committed, changed)
    removed = replace(committed, tasks={"t-1": committed.tasks["t-1"]})
    with pytest.raises(TaskError, match=r"\(t-2\), but 'version' is not higher"):
        check_version_history(committed, removed)
    check_version_history(committed, replace(committed, version=1))


def test_new_version_against_the_committed_one_passes(tmp_path: Path) -> None:
    repo, base = committed_repo(tmp_path)
    new = fix_t2(repo)

    def bump(m: dict) -> None:
        m["version"] = 2
        m["tasks"]["t-2"]["sha256"] = new
        m["changelog"].append({"version": 2, "note": "Fix t-2.", "changed": {"t-2": new}})

    edit(repo / "tasks" / "ours" / MANIFEST_FILE, bump)
    git(repo, "commit", "-q", "-am", "v2")
    check_repository_tasks(repo / "tasks", repo, base)
    check_repository_tasks(repo / "tasks", repo, git(repo, "rev-parse", "HEAD"))


def test_new_set_has_no_committed_manifest(tmp_path: Path) -> None:
    repo, base = committed_repo(tmp_path)
    write_task(
        repo / "tasks" / "reviewbench", "rb-1", task_set="reviewbench", items=[item(source=None)]
    )
    write_manifest(repo / "tasks" / "reviewbench", task_set="reviewbench")
    assert (
        load_committed_manifest(
            repo, base, repo / "tasks" / "reviewbench" / MANIFEST_FILE, "reviewbench"
        )
        is None
    )
    check_repository_tasks(repo / "tasks", repo, base)


@pytest.mark.parametrize("rev", ["0" * 40, "origin/nowhere", "-p", ""])
def test_unreadable_base_fails_closed(tmp_path: Path, rev: str) -> None:
    repo, _ = committed_repo(tmp_path)
    with pytest.raises(TaskError, match=r"base revision"):
        check_repository_tasks(repo / "tasks", repo, rev)


def test_base_outside_a_git_repository_fails_closed(tmp_path: Path) -> None:
    set_dir = two_task_set(tmp_path)
    with pytest.raises(TaskError, match=r"cannot resolve the base revision"):
        load_committed_manifest(tmp_path, "HEAD", set_dir / MANIFEST_FILE, "ours")


def archive(sha256: str = "c" * 64, license_id: str | None = "MIT") -> SourceArchive:
    """A built archive as `build_archive` returns it; `write_task` records `c` * 64."""
    return SourceArchive(Path("t.bundle"), sha256, license_id, "LICENSE" if license_id else None)


@pytest.mark.parametrize("task_set", ["reviewbench", "ours"])
def test_import_records_the_license_id_in_the_manifest(tmp_path: Path, task_set: str) -> None:
    set_dir = tmp_path / task_set
    write_task(set_dir, "t-1", task_set=task_set)
    (task,) = load_tasks(set_dir)
    entry = manifest_task(task, archive())
    assert entry == ManifestTask(task_content_sha256(task), "MIT")
    edit(
        write_manifest(set_dir, task_set),
        lambda d: d["tasks"]["t-1"].update(license=entry.license),
    )
    manifest, _ = load_task_set(set_dir, task_set)
    assert manifest.tasks["t-1"] == entry


def test_hidden_manifest_entry_has_no_license(tmp_path: Path) -> None:
    set_dir = tmp_path / "hidden"
    write_task(set_dir, HID, task_set="hidden")
    (task,) = load_hidden_tasks(hidden_set(set_dir))
    assert manifest_task(task, archive(license_id=None)).license is None
    with pytest.raises(TaskError, match=r"^a task in set 'hidden': the archive's SHA-256") as err:
        manifest_task(task, archive(sha256="d" * 64))
    assert_private(err, HID, HIDDEN, str(tmp_path))


@pytest.mark.parametrize(
    ("built", "message"),
    [
        (archive(sha256="d" * 64), "is not the one task.json records"),
        (archive(license_id=None), "without the source license check"),
    ],
)
def test_manifest_entry_needs_the_tasks_checked_archive(
    tmp_path: Path, built: SourceArchive, message: str
) -> None:
    write_task(tmp_path / "ours", "t-1")
    (task,) = load_tasks(tmp_path / "ours")
    with pytest.raises(TaskError, match=message):
        manifest_task(task, built)
