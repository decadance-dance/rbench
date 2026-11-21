"""The trial's scrubbed repository and the gh stub (spec trial-isolation, Scrubbed repository and
Local PR lookups only; design D4). Repositories are built here with git; docker is the fake."""

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from test_archive import commit, git

from rbench.tasks.archive import ArchiveError, build_archive
from rbench.tasks.loader import HIDDEN_ID_PREFIX, PUBLIC_CANARY_PREFIX, Task
from rbench.trial import (
    REPOSITORY_FILE,
    RepositoryError,
    RepositoryState,
    TrialProject,
    copy_comments,
    inspect_repository,
    prepare_repository,
    repository,
)
from rbench.trial.repository import (
    TRIAL_ROOT,
    _git_env,
    _host_runner,
    _take_comments,
    _unpack,
)

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "images" / "agent" / "gh"
AGENT = "rbench-agent@sha256:" + "a" * 64
PROXY = "rbench-proxy@sha256:" + "b" * 64
HIDDEN_ID = f"{HIDDEN_ID_PREFIX}secret"


def source_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """A source with history before the base, a two-commit PR and a follow-up after review."""
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    earlier = commit(repo, {"app.py": "x = 0\n"}, "earlier history")
    base = commit(repo, {"app.py": "x = 1\n", "lib.py": "y = 1\n"}, "base")
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, {"app.py": "x = 2\n"}, "first change")
    head = commit(repo, {"app.py": "x = 3\n", "new.py": "z = 1\n"}, "second change")
    commit(repo, {"app.py": "x = 4\n"}, "follow-up after review")
    return repo, earlier, base, head


@pytest.fixture
def archived(tmp_path: Path) -> tuple[Task, Path]:
    repo, _, base, head = source_repo(tmp_path)
    # The hidden set skips the license lookup; the task itself is a public one.
    archive = build_archive("acme/widget", str(repo), base, head, tmp_path / "a.bundle", "hidden")
    return make_task(tmp_path, base, head, archive.sha256), archive.path


def make_task(tmp_path: Path, base: str, head: str, archive_sha256: str) -> Task:
    return Task(
        id="t-1",
        set="ours",
        repo="acme/widget",
        pr=7,
        base=base,
        head=head,
        archive_sha256=archive_sha256,
        language="python",
        title="Make x three",
        description="Sets x to 3 and adds new.py.",
        canary=PUBLIC_CANARY_PREFIX + "0" * 32,
        ground_truth=(),
        path=tmp_path / "task",
    )


def unpacked(tmp_path: Path, archive: Path, name: str = "copy") -> Path:
    repo = tmp_path / name
    _unpack(archive, repo, False, "test")
    return repo


def git_bytes(repo: Path, *args: str, input: bytes = b"") -> bytes:
    env = _git_env() | {
        "GIT_AUTHOR_NAME": "dev",
        "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "dev",
        "GIT_COMMITTER_EMAIL": "dev@example.com",
    }
    argv = ["git", "-C", str(repo), *args]
    return subprocess.run(argv, env=env, input=input, capture_output=True, check=True).stdout


def plumbed_archive(
    tmp_path: Path, base_files: dict[bytes, bytes], head_files: dict[bytes, bytes]
) -> tuple[Task, Path]:
    """An archive of trees no file system has to hold: names and contents as raw bytes."""
    repo = tmp_path / "plumbed"
    repo.mkdir()
    git_bytes(repo, "init", "-q")

    def commit_of(files: dict[bytes, bytes], *parent: str) -> str:
        entries = b""
        for name, data in sorted(files.items()):
            blob = git_bytes(repo, "hash-object", "-w", "--stdin", input=data).strip()
            entries += b"100644 blob " + blob + b"\t" + name + b"\0"
        tree = git_bytes(repo, "mktree", "-z", input=entries).decode().strip()
        parents = [arg for p in parent for arg in ("-p", p)]
        return git_bytes(repo, "commit-tree", tree, *parents, "-m", "c").decode().strip()

    base = commit_of(base_files)
    head = commit_of(head_files, base)
    out = tmp_path / "plumbed.bundle"
    archive = build_archive("acme/widget", str(repo), base, head, out, "hidden")
    return make_task(tmp_path, base, head, archive.sha256), archive.path


@pytest.fixture
def staged(monkeypatch, tmp_path: Path) -> Path:
    """Stand-in for `docker compose cp -a`: keeps what would go into the agent."""
    captured = tmp_path / "captured"

    def copy(self, src: Path, dest: str) -> None:
        assert dest == "/rbench"
        shutil.copytree(src, captured, symlinks=True)

    monkeypatch.setattr(TrialProject, "copy_to_agent", copy)
    return captured


@pytest.fixture
def local_agent(monkeypatch, staged: Path) -> Path:
    """The agent stood in by this machine: commands run here, on the staged copy of /rbench."""

    def exec_agent(self, argv: list[str], stdin: str | None = None):
        argv = [arg.replace(TRIAL_ROOT, str(staged)) for arg in argv]
        return subprocess.run(
            argv,
            check=False,
            env=_git_env(),
            input=stdin,
            capture_output=True,
            text=True,
            errors="replace",
        )

    monkeypatch.setattr(TrialProject, "exec_agent", exec_agent)
    return staged


def test_trial_gets_the_two_commit_repository_and_pr(fake_bin, archived, tmp_path, local_agent):
    staged = local_agent
    task, archive = archived
    project = TrialProject.create(tmp_path / "trial", "rbench-t1", AGENT, PROXY)

    state = prepare_repository(project, task, archive, tmp_path / "trial")

    repo = staged / "repo"
    assert sorted(p.name for p in staged.iterdir()) == ["output", "pr.json", "repo"]
    assert list((staged / "output").iterdir()) == []
    assert json.loads((staged / "pr.json").read_text()) == {
        "number": 7,
        "title": "Make x three",
        "body": "Sets x to 3 and adds new.py.",
    }
    assert sorted(p.name for p in (repo / ".git").iterdir()) == [
        "HEAD",
        "config",
        "index",
        "objects",
        "refs",
    ]
    assert git(repo, "log", "--format=%s", "reviewed") == "reviewed\nbase"
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "app.py").read_text() == "x = 3\n"
    assert git(repo, "remote") == "" and git(repo, "tag") == ""
    assert (repo / ".git/config").read_text() == repository.git_config(False)
    reachable = len(git(repo, "rev-list", "--objects", "--no-object-names", "--all").splitlines())
    assert state == RepositoryState(
        git(repo, "rev-parse", "base"), git(repo, "rev-parse", "reviewed"), reachable
    )
    recorded = json.loads((tmp_path / "trial" / REPOSITORY_FILE).read_text())
    assert recorded == {"base": state.base, "reviewed": state.reviewed, "objects": reachable}


def test_altered_archive_never_reaches_the_trial(fake_bin, archived, tmp_path, staged):
    task, archive = archived
    project = TrialProject.create(tmp_path / "trial", "rbench-t1", AGENT, PROXY)

    with pytest.raises(ArchiveError, match="^task t-1: source archive SHA-256 does not match"):
        prepare_repository(project, replace(task, archive_sha256="0" * 64), archive, tmp_path)

    assert not staged.exists() and fake_bin.calls() == []


def bundle(tmp_path: Path, refs: dict[str, str], repo: Path) -> Path:
    for name, commit_id in refs.items():
        git(repo, "update-ref", name, commit_id)
    out = tmp_path / "bad.bundle"
    git(repo, "bundle", "create", "-q", str(out), *refs)
    return out


@pytest.mark.parametrize(
    "refs, error",
    [
        # base keeps its earlier history
        (lambda e, b, h: {"refs/heads/base": b, "refs/heads/reviewed": h}, "4 commits"),
        (lambda e, b, h: {"refs/heads/base": e, "refs/heads/reviewed": e}, "1 commits"),
        (
            lambda e, b, h: {"refs/heads/base": e, "refs/heads/reviewed": b, "refs/tags/v1": b},
            "3 refs",
        ),
        (
            lambda e, b, h: {
                "refs/heads/base": e,
                "refs/heads/reviewed": b,
                "refs/remotes/origin/main": b,
            },
            "3 refs",
        ),
    ],
)
def test_archive_with_extra_history_fails_before_the_engine(
    fake_bin, tmp_path, staged, refs, error
):
    repo, earlier, base, head = source_repo(tmp_path)
    bad = bundle(tmp_path, refs(earlier, base, head), repo)
    task = make_task(tmp_path, base, head, hashlib.sha256(bad.read_bytes()).hexdigest())
    project = TrialProject.create(tmp_path / "trial", "rbench-t1", AGENT, PROXY)

    with pytest.raises(RepositoryError, match=f"^task t-1: the repository is not .*{error}"):
        prepare_repository(project, task, bad, tmp_path / "trial")

    assert not staged.exists() and fake_bin.calls() == []
    assert not (tmp_path / "trial" / REPOSITORY_FILE).exists()


def write_reflog(repo: Path) -> None:
    (repo / ".git" / "logs").mkdir()
    (repo / ".git" / "logs" / "HEAD").write_text("0 1 dev <dev@example.com> 0 +0000\tfetch\n")


def add_object(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=b"x", check=True)


STATE = "unexpected git state: 1 entries in .git"
MUTATIONS = {
    "remote": ("unexpected git config", lambda r: git(r, "remote", "add", "o", "https://e.com/r")),
    # An include that defines no remote still changes how git behaves.
    "include": ("unexpected git config", lambda r: git(r, "config", "include.path", "/elsewhere")),
    "reflog": (STATE, write_reflog),
    "fetch-head": (STATE, lambda r: (r / ".git/FETCH_HEAD").write_text("x\n")),
    "shallow": (STATE, lambda r: (r / ".git/shallow").write_text(git(r, "rev-parse", "base"))),
    "head": (
        "HEAD is not on reviewed",
        lambda r: (r / ".git/HEAD").write_text("ref: refs/heads/base\n"),
    ),
    "tag": ("3 refs", lambda r: git(r, "tag", "v1", "base")),
    "alternate": (
        "alternate",
        lambda r: (r / ".git/objects/info/alternates").write_text(
            str(r.parent / "source/.git/objects")
        ),
    ),
    "empty-alternates": (
        "alternate",
        lambda r: (r / ".git/objects/info/alternates").write_text(""),
    ),
    "object": (r"\d+ objects, \d+ reachable", add_object),
}


@pytest.mark.parametrize("case", MUTATIONS)
def test_inspection_refuses_anything_but_the_scrubbed_repository(archived, tmp_path, case):
    _, archive = archived
    repo = unpacked(tmp_path, archive)
    inspect_repository(_host_runner("t"), str(repo), "task t-1")
    error, mutate = MUTATIONS[case]

    mutate(repo)

    with pytest.raises(RepositoryError, match=f"^task t-1: the repository is not .*{error}"):
        inspect_repository(_host_runner("t"), str(repo), "task t-1")


def test_unreadable_git_output_is_a_refusal():
    def run(argv: list[str]) -> str:
        metadata = {
            ".git": "HEAD\nconfig\nobjects\nrefs\n",
            ".git/config": repository.git_config(False),
            ".git/HEAD": "ref: refs/heads/reviewed\n",
            ".git/objects/info": "",
        }
        if argv[0] in ("ls", "cat"):
            return metadata[argv[-1].removeprefix("/rbench/repo/")]
        return "refs/heads/base\n"  # no object id

    with pytest.raises(RepositoryError, match=r"not the scrubbed one: unreadable git output"):
        inspect_repository(run, "/rbench/repo", "task t-1")


def test_trial_copy_must_match_the_archive(monkeypatch, archived, tmp_path, local_agent):
    task, archive = archived
    # The agent holds a scrubbed repository, but another one: the follow-up as `reviewed`.
    source = tmp_path / "source"
    other = build_archive(
        "acme/widget", str(source), task.base, git(source, "rev-parse", "feature"),
        tmp_path / "other.bundle", "hidden",
    )  # fmt: skip
    project = TrialProject.create(tmp_path / "trial", "rbench-t1", AGENT, PROXY)
    copy = TrialProject.copy_to_agent

    def copy_other(self, src: Path, dest: str) -> None:
        shutil.rmtree(src / "repo")
        _unpack(other.path, src / "repo", False, "test")
        copy(self, src, dest)

    monkeypatch.setattr(TrialProject, "copy_to_agent", copy_other)

    with pytest.raises(RepositoryError, match="^task t-1: the repository in the trial differs"):
        prepare_repository(project, task, archive, tmp_path / "trial")
    assert not (tmp_path / "trial" / REPOSITORY_FILE).exists()


def test_failing_check_in_the_agent_is_an_infrastructure_error(
    fake_bin, archived, tmp_path, staged
):
    task, archive = archived
    fake_bin.scenario("docker", stderr="fatal: not a git repository\n", exit=128)
    project = TrialProject.create(tmp_path / "trial", "rbench-t1", AGENT, PROXY)

    with pytest.raises(RepositoryError, match=r"^task t-1, in the trial: .* exited 128: fatal"):
        prepare_repository(project, task, archive, tmp_path / "trial")


def test_errors_name_no_hidden_task(fake_bin, archived, tmp_path, staged):
    task, archive = archived
    hidden = replace(task, id=HIDDEN_ID, set="hidden")
    project = TrialProject.create(tmp_path / "trial", "rbench-t1", AGENT, PROXY)

    with pytest.raises(RepositoryError) as e:
        prepare_repository(project, hidden, archive, tmp_path / "trial")

    assert str(e.value).startswith("a task in set 'hidden', in the trial: ")
    assert HIDDEN_ID not in str(e.value)


def test_archive_must_be_the_verified_bundle_bytes(fake_bin, archived, tmp_path, local_agent):
    # Review M1: a `.git` pointer file hashes like any file, and git fetch follows it to a
    # repository that can change after the check.
    task, archive = archived
    linked = unpacked(tmp_path, archive, "linked")  # holds base and reviewed, and can be amended
    pointer = tmp_path / "pointer"
    pointer.write_text(f"gitdir: {linked / '.git'}\n")
    project = TrialProject.create(tmp_path / "trial", "rbench-t1", AGENT, PROXY)
    as_pointer = replace(task, archive_sha256=hashlib.sha256(pointer.read_bytes()).hexdigest())

    with pytest.raises(ArchiveError, match="^task t-1: its source archive is not a git bundle"):
        prepare_repository(project, as_pointer, pointer, tmp_path / "trial")
    with pytest.raises(ArchiveError, match="^task t-1: cannot read its source archive"):
        prepare_repository(project, task, tmp_path / "source", tmp_path / "trial")

    assert not local_agent.exists() and fake_bin.calls() == []


def test_work_tree_holds_every_path_of_reviewed(fake_bin, tmp_path, local_agent):
    # Review M2: `Foo` and `foo` are two paths; a case-insensitive checkout keeps one.
    task, archive = plumbed_archive(
        tmp_path, {b"a.py": b"a\n"}, {b"a.py": b"a\n", b"Foo": b"upper\n", b"foo": b"lower\n"}
    )
    project = TrialProject.create(tmp_path / "trial", "rbench-t1", AGENT, PROXY)
    (tmp_path / "case").write_text("")
    case_insensitive = (tmp_path / "CASE").exists()

    if case_insensitive:
        with pytest.raises(RepositoryError, match="in the trial: the work tree differs from"):
            prepare_repository(project, task, archive, tmp_path / "trial")
    else:
        prepare_repository(project, task, archive, tmp_path / "trial")
        repo = local_agent / "repo"
        assert (repo / "Foo").read_text() == "upper\n" and (repo / "foo").read_text() == "lower\n"


def test_path_names_do_not_change_the_object_count(tmp_path):
    # Review M4: `rev-list --objects` prints path names, and one may hold a carriage return.
    _, archive = plumbed_archive(tmp_path, {b"a": b"a\n"}, {b"a": b"b\n", b"x\ry": b"c\n"})
    repo = unpacked(tmp_path, archive)

    state = inspect_repository(_host_runner("t"), str(repo), "task t-1")

    assert state.objects == 7  # two commits, two trees, three blobs


def test_checkout_size_is_bounded(fake_bin, archived, tmp_path, local_agent, monkeypatch):
    task, archive = archived
    monkeypatch.setattr(repository, "MAX_CHECKOUT_BYTES", 10)
    project = TrialProject.create(tmp_path / "trial", "rbench-t1", AGENT, PROXY)

    with pytest.raises(RepositoryError, match=r"^task t-1: the reviewed tree holds \d+ bytes"):
        prepare_repository(project, task, archive, tmp_path / "trial")
    assert not local_agent.exists()


def test_unpack_time_is_bounded(fake_bin, archived, tmp_path, local_agent, monkeypatch):
    task, archive = archived
    monkeypatch.setattr(repository, "UNPACK_TIMEOUT", 1e-9)
    project = TrialProject.create(tmp_path / "trial", "rbench-t1", AGENT, PROXY)

    with pytest.raises(RepositoryError, match=r"^task t-1: git init .* ran over"):
        prepare_repository(project, task, archive, tmp_path / "trial")


def test_comments_are_copied_into_raw_output(fake_bin, tmp_path):
    project = TrialProject.create(tmp_path / "trial", "rbench-t1", AGENT, PROXY)

    assert copy_comments(project, tmp_path / "trial") == []

    [cp] = [c["argv"] for c in fake_bin.calls()]
    assert cp[5:7] == ["cp", "agent:/rbench/output"]
    assert Path(cp[7]).name == "output" and not Path(cp[7]).parent.exists()


def test_only_regular_comment_files_are_kept(tmp_path):
    copied, dest = tmp_path / "output", tmp_path / "raw"
    copied.mkdir()
    dest.mkdir()
    for n in (10, 2, 1):
        (copied / f"comment-{n}.md").write_text(f"body {n}\n")
    (tmp_path / "host-file").write_text("host\n")
    (copied / "comment-3.md").symlink_to(tmp_path / "host-file")
    (copied / "comment-4.md").mkdir()
    (copied / "notes.txt").write_text("x\n")

    kept = _take_comments(copied, dest)

    assert [p.name for p in kept] == ["comment-1.md", "comment-2.md", "comment-10.md"]
    assert sorted(p.name for p in dest.iterdir()) == [p.name for p in sorted(kept)]
    assert (dest / "comment-10.md").read_text() == "body 10\n"


def test_output_dir_replaced_by_a_symlink_yields_nothing(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    (host / "comment-1.md").write_text("host file\n")
    (tmp_path / "output").symlink_to(host)
    (tmp_path / "raw").mkdir()

    assert _take_comments(tmp_path / "output", tmp_path / "raw") == []
    assert (host / "comment-1.md").exists()


# The gh stub runs on the host's Node here, and in the image in the live test.
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@pytest.fixture
def trial_root(archived, tmp_path: Path) -> Path:
    _, archive = archived
    root = tmp_path / "rbench"
    root.mkdir()
    _unpack(archive, root / "repo", False, "test")
    pr = {"number": 7, "title": "Make x three", "body": "Sets x to 3."}
    (root / "pr.json").write_text(json.dumps(pr))
    return root


def gh(root: Path, *argv: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(STUB), *argv],
        env={"PATH": os.environ["PATH"], "RBENCH_TRIAL_ROOT": str(root)},
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


@needs_node
@pytest.mark.parametrize("selector", [[], ["7"], ["#7"], ["reviewed"]])
def test_gh_pr_diff_is_base_to_reviewed(trial_root, selector):
    result = gh(trial_root, "pr", "diff", *selector, "--color", "never")

    assert result.returncode == 0, result.stderr
    assert result.stdout == git(trial_root / "repo", "diff", "base", "reviewed") + "\n"
    assert "+x = 3" in result.stdout and "new.py" in result.stdout
    names = gh(trial_root, "pr", "diff", "--name-only", "--color=never").stdout
    assert names.split() == ["app.py", "new.py"]


@needs_node
def test_gh_pr_diff_is_byte_exact_with_a_file_named_base(tmp_path):
    # Review M3: a root file named `base` made the diff ambiguous, and a Latin-1 byte came out as
    # U+FFFD.
    _, archive = plumbed_archive(
        tmp_path, {b"base": b"one\n"}, {b"base": b"one\ncaf\xe9\n", b"reviewed": b"x\n"}
    )
    root = tmp_path / "rbench"
    _unpack(archive, root / "repo", False, "test")
    (root / "pr.json").write_text(json.dumps({"number": 7, "title": "t", "body": "b"}))
    env = {"PATH": os.environ["PATH"], "RBENCH_TRIAL_ROOT": str(root)}

    result = subprocess.run(
        ["node", str(STUB), "pr", "diff"], env=env, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    expected = git_bytes(root / "repo", "diff", "refs/heads/base", "refs/heads/reviewed", "--")
    assert result.stdout == expected and b"+caf\xe9\n" in expected


@needs_node
def test_gh_pr_view_reports_open_without_comments_or_reviews(trial_root):
    fields = "state,comments,reviews,title,body,number,isDraft,headRefName,baseRefName"
    result = gh(trial_root, "pr", "view", "7", "--json", fields)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "baseRefName": "base",
        "body": "Sets x to 3.",
        "comments": [],
        "headRefName": "reviewed",
        "isDraft": False,
        "number": 7,
        "reviews": [],
        "state": "OPEN",
        "title": "Make x three",
    }
    text = gh(trial_root, "pr", "view", "--comments").stdout
    assert text == "title:\tMake x three\nstate:\tOPEN\nnumber:\t7\n--\nSets x to 3.\n"


@needs_node
def test_gh_pr_comment_is_stored_and_sent_nowhere(trial_root, tmp_path):
    (tmp_path / "review.md").write_text("from a file\n")

    assert gh(trial_root, "pr", "comment", "7", "--body", "first").returncode == 0
    assert gh(trial_root, "pr", "comment", "-F", "-", stdin="from stdin\n").returncode == 0
    assert (
        gh(trial_root, "pr", "comment", "--body-file", str(tmp_path / "review.md")).returncode == 0
    )

    out = trial_root / "output"
    assert [(out / f"comment-{n}.md").read_text() for n in (1, 2, 3)] == [
        "first",
        "from stdin\n",
        "from a file\n",
    ]
    # No network module and no process but git: nothing can leave the trial.
    source = STUB.read_text()
    assert not re.search(r"require\(\"(?:node:)?(?:https?|http2|net|tls|dns|dgram)\"\)", source)
    assert re.findall(r"execFileSync\(\"(\w+)\"", source) == ["git"]


@needs_node
@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["pr", "list"],
        ["pr", "review", "--comment", "-b", "x"],
        ["api", "repos/acme/widget/pulls/7"],
        ["auth", "status"],
        ["pr", "view", "8"],
        ["pr", "diff", "feature"],
        ["pr", "view", "-R", "acme/widget"],
        ["pr", "view", "--json", "title", "--jq", ".title"],
        ["pr", "view", "--json", "url"],
        ["pr", "view", "--web"],
        ["pr", "diff", "--patch"],
        ["pr", "diff", "--color", "--patch"],
        ["pr", "diff", "--color=invalid"],
        ["pr", "comment", "--edit-last", "-b", "x"],
        ["pr", "comment"],
        ["pr", "comment", "-b", "x", "-F", "-"],
    ],
)
def test_gh_fails_every_other_lookup(trial_root, argv):
    result = gh(trial_root, *argv)

    assert result.returncode == 1
    assert result.stdout == "" and result.stderr.startswith("gh (rbench trial): ")
    assert not (trial_root / "output").exists()
