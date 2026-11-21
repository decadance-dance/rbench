"""A trial's scrubbed repository and frozen PR (spec trial-isolation, Scrubbed repository and Local
PR lookups only; spec review-tasks, Source archive; design D3, D4).

`prepare_repository` builds the repository only from the task's archive and copies it into the
agent container with the PR description:

    /rbench/repo      the work tree on `reviewed`, with branches `base` and `reviewed`
    /rbench/pr.json   {"number", "title", "body"}: what the `gh` stub (images/agent/gh) answers from
    /rbench/output/   where the stub writes the body of each `gh pr comment`

The archive is copied to a private file first, and only those bytes are hashed and read, as a git
bundle. They are unpacked on the host into a fresh object store with a fixed `config` and `HEAD`
(`git init --template=`, no reflog, no FETCH_HEAD, no work tree), copied with `docker compose cp
-a`, never mounted (`-a` makes the agent's user own it), and checked out inside the agent: a host
file system may be case-insensitive and drop a path, Linux in the agent keeps every one, and
`git status` there must find the work tree equal to `reviewed`. The same inspection runs on the
host copy and inside the agent, and both must find the scrubbed repository and agree, or the trial
fails with an infrastructure error before the engine starts. The object count goes to the trial's
`repository.json`; the audit recounts with `count_objects` after the engine.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from rbench.atomic import atomic_write
from rbench.ledger.trials import RAW_DIR
from rbench.tasks.archive import ArchiveError
from rbench.tasks.loader import Task
from rbench.tasks.release import verify_archive
from rbench.trial.compose import TrialProject
from rbench.trial.images import TrialError

TRIAL_ROOT = "/rbench"
REPO_DIR = f"{TRIAL_ROOT}/repo"
PR_FILE = "pr.json"
OUTPUT_DIR = f"{TRIAL_ROOT}/output"
REPOSITORY_FILE = "repository.json"
COMMENTS_DIR = "pr-comments"
COMMENT = re.compile(r"comment-([1-9][0-9]*)\.md")
BRANCHES = {"refs/heads/base", "refs/heads/reviewed"}
BUNDLE_HEADERS = (b"# v2 git bundle\n", b"# v3 git bundle\n")
# All .git may hold: `logs` (reflog), `shallow`, `FETCH_HEAD`, `info` (grafts), `hooks`,
# `packed-refs`, `worktrees` and the rest are refused. `index` appears with the checkout.
GIT_DIR_ENTRIES = {"HEAD", "config", "index", "objects", "refs"}
HEAD = "ref: refs/heads/reviewed\n"
# limit: fixed bounds on what one archive may cost the host and the agent's disk; raise them, or
# make them settings, when a real task needs more.
UNPACK_TIMEOUT = 600  # seconds, per host git step
MAX_CHECKOUT_BYTES = 4 << 30

# `run(argv)` runs a command where the repository is and returns its stdout; it raises
# `RepositoryError` when the command fails.
Runner = Callable[[list[str]], str]


class RepositoryError(TrialError):
    """The trial's repository is not the scrubbed two-commit repository: an infrastructure error."""


@dataclass(frozen=True)
class RepositoryState:
    """What the audit compares: the two commits and the number of objects in the repository."""

    base: str
    reviewed: str
    objects: int


def git_config(sha256: bool) -> str:
    """The only `.git/config` a trial's repository may have: no remote, include or alternate."""
    config = "[core]\n\trepositoryformatversion = {}\n\tfilemode = true\n\tbare = false\n"
    if not sha256:
        return config.format(0)
    return config.format(1) + "[extensions]\n\tobjectformat = sha256\n"


def inspect_repository(run: Runner, repo: str, where: str) -> RepositoryState:
    """Check that `repo` is the scrubbed repository and return its state; raise `RepositoryError`.

    The metadata comes first, read as files before git interprets any of it: only the expected
    `.git` entries, the fixed `config` and `HEAD`, nothing in `objects/info` (alternates). Then
    exactly the branches `base` and `reviewed`, `reviewed`'s only parent `base`, `base` with none,
    and every object reachable from the two commits. Errors name `where`, never a ref or a path
    from the task.
    """

    def refuse(reason: str) -> RepositoryError:
        return RepositoryError(f"{where}: the repository is not the scrubbed one: {reason}")

    extra = set(run(["ls", "-A", f"{repo}/.git"]).split("\n")) - GIT_DIR_ENTRIES - {""}
    if extra:
        # A count, not names: after the engine, .git holds whatever it wrote there.
        raise refuse(f"unexpected git state: {len(extra)} entries in .git")
    if run(["cat", f"{repo}/.git/config"]) not in (git_config(False), git_config(True)):
        raise refuse("unexpected git config")
    if run(["cat", f"{repo}/.git/HEAD"]) != HEAD:
        raise refuse("HEAD is not on reviewed")
    if run(["ls", "-A", f"{repo}/.git/objects/info"]):
        raise refuse("alternates or other object metadata")

    def git(*argv: str) -> list[str]:
        return run(["git", "-C", repo, *argv]).split("\n")[:-1]

    # This output is untrusted once the engine has run: anything unreadable is a refusal too.
    try:
        refs = dict(
            line.split(" ", 1) for line in git("for-each-ref", "--format=%(refname) %(objectname)")
        )
        if refs.keys() != BRANCHES:
            raise refuse(f"{len(refs)} refs; expected only refs/heads/base and refs/heads/reviewed")
        base, reviewed = refs["refs/heads/base"], refs["refs/heads/reviewed"]
        commits = sorted(git("rev-list", "--all", "--parents"))
        if commits != sorted([base, f"{reviewed} {base}"]):
            raise refuse(f"{len(commits)} commits; expected base and reviewed on top of it")
        objects = count_objects(run, repo, where)
        # Object ids only: a path name may hold a line break.
        reachable = len(git("rev-list", "--objects", "--no-object-names", "--all"))
        if objects != reachable:
            raise refuse(f"{objects} objects, {reachable} reachable from base and reviewed")
    except (ValueError, KeyError) as e:
        raise refuse(f"unreadable git output ({type(e).__name__})") from None
    return RepositoryState(base, reviewed, objects)


def count_objects(run: Runner, repo: str, where: str) -> int:
    """The number of objects stored in `repo`, refused with alternate or garbage object stores."""
    lines = run(["git", "-C", repo, "count-objects", "-v"]).split("\n")[:-1]
    try:
        counts = dict(line.split(": ", 1) for line in lines)
        if "alternate" in counts or counts.get("garbage") != "0":
            raise RepositoryError(f"{where}: alternate or garbage object stores")
        return int(counts["count"]) + int(counts["in-pack"])
    except (ValueError, KeyError) as e:
        raise RepositoryError(
            f"{where}: unreadable git count-objects ({type(e).__name__})"
        ) from None


def prepare_repository(
    project: TrialProject, task: Task, archive: Path, trial_dir: Path
) -> RepositoryState:
    """Put the task's repository and PR into the started agent; record the state for the audit.

    The archive is checked against the task's SHA-256 and as a git bundle first (`ArchiveError`
    names the task). Any other failure is a `TrialError`, raised before the engine may start.
    """
    where = "a task in set 'hidden'" if task.set == "hidden" else f"task {task.id}"
    with tempfile.TemporaryDirectory(prefix="rbench-trial-") as tmp:
        bundle = _private_bundle(task, Path(archive), Path(tmp) / "source.bundle", where)
        stage = Path(tmp) / "rbench"
        _unpack(bundle, stage / "repo", len(task.head) == 64, where)
        expected = inspect_repository(_host_runner(where), str(stage / "repo"), where)
        _check_size(stage / "repo", where)
        pr = {"number": task.pr, "title": task.title, "body": task.description}
        (stage / PR_FILE).write_text(json.dumps(pr, indent=2, sort_keys=True) + "\n")
        (stage / "output").mkdir()
        project.copy_to_agent(stage, TRIAL_ROOT)
    inside = f"{where}, in the trial"
    agent = agent_runner(project, inside)
    found = inspect_repository(agent, REPO_DIR, inside)
    if found != expected:
        raise RepositoryError(f"{where}: the repository in the trial differs from its archive")
    agent(["git", "-C", REPO_DIR, "read-tree", "--reset", "-u", "refs/heads/reviewed"])
    status = ["status", "--porcelain", "-z", "--untracked-files=all", "--ignored"]
    if agent(["git", "-C", REPO_DIR, *status]):
        raise RepositoryError(f"{inside}: the work tree differs from reviewed after the checkout")
    data = json.dumps(asdict(found), indent=2, sort_keys=True) + "\n"
    atomic_write(Path(trial_dir) / REPOSITORY_FILE, data)
    return found


def agent_runner(project: TrialProject, where: str) -> Runner:
    """A `Runner` in the trial's agent container."""

    def run(argv: list[str]) -> str:
        return _stdout(project.exec_agent(argv), argv, where)

    return run


def copy_comments(project: TrialProject, trial_dir: Path) -> list[Path]:
    """Copy the bodies the engine posted with `gh pr comment` into the trial's raw output.

    They land in `raw/pr-comments/comment-<n>.md`, in posting order. The output directory is the
    engine's to write, so only regular files with that name are kept: a symlink there must not
    lead normalization to a host file.
    """
    dest = Path(trial_dir) / RAW_DIR / COMMENTS_DIR
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=trial_dir, prefix=".pr-comments-") as tmp:
        copied = Path(tmp) / "output"
        project.copy_from_agent(OUTPUT_DIR, copied)
        kept = _take_comments(copied, dest)
    return kept


def _take_comments(copied: Path, dest: Path) -> list[Path]:
    if copied.is_symlink() or not copied.is_dir():
        return []
    kept = []
    for entry in copied.iterdir():
        match = COMMENT.fullmatch(entry.name)
        if match and entry.is_file() and not entry.is_symlink():
            os.replace(entry, dest / entry.name)
            kept.append((int(match[1]), dest / entry.name))
    return [path for _, path in sorted(kept)]


def _stdout(result: subprocess.CompletedProcess[str], argv: list[str], where: str) -> str:
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1:] or ["no output"]
        command = " ".join(argv[:4])
        raise RepositoryError(f"{where}: {command} exited {result.returncode}: {detail[0]}")
    return result.stdout


def _git_env() -> dict[str, str]:
    # The operator's git configuration must not shape the repository the engine sees.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return env | {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _host_runner(where: str) -> Runner:
    def run(argv: list[str]) -> str:
        try:
            result = subprocess.run(
                argv,
                env=_git_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                timeout=UNPACK_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            command = " ".join(argv[:4])
            raise RepositoryError(f"{where}: {command} ran over {UNPACK_TIMEOUT}s") from None
        except OSError as e:
            raise RepositoryError(f"{where}: cannot run {argv[0]}: {e}") from e
        return _stdout(result, argv, where)

    return run


def _private_bundle(task: Task, archive: Path, dest: Path, where: str) -> Path:
    """Copy `archive` to `dest`, then check the copy's SHA-256 and that it is a git bundle.

    Only these bytes are read afterwards. A path git would also accept, such as a `.git` pointer
    file or a repository, could change after the check.
    """
    try:
        shutil.copyfile(archive, dest)
    except OSError as e:
        raise ArchiveError(f"{where}: cannot read its source archive: {e.strerror}") from None
    verify_archive(task, dest)
    with dest.open("rb") as f:
        if f.read(len(BUNDLE_HEADERS[0])) not in BUNDLE_HEADERS:
            raise ArchiveError(f"{where}: its source archive is not a git bundle")
    return dest


def _check_size(repo: Path, where: str) -> None:
    """Refuse a `reviewed` tree whose checkout would exceed `MAX_CHECKOUT_BYTES`."""
    out = _host_runner(where)(
        ["git", "-C", str(repo), "ls-tree", "-r", "-l", "-z", "refs/heads/reviewed"]
    )
    # `<mode> <type> <id> <size>\t<path>`, NUL-terminated; a submodule's size is `-`.
    sizes = (entry.split("\t", 1)[0].split()[-1] for entry in out.split("\0") if entry)
    total = sum(int(size) for size in sizes if size != "-")
    if total > MAX_CHECKOUT_BYTES:
        raise RepositoryError(
            f"{where}: the reviewed tree holds {total} bytes, over the {MAX_CHECKOUT_BYTES} limit"
        )


def _unpack(bundle: Path, repo: Path, sha256: bool, where: str) -> None:
    """A fresh object store holding every ref of `bundle`, with the fixed config and HEAD."""
    run = _host_runner(where)
    object_format = "sha256" if sha256 else "sha1"
    run(["git", "init", "--quiet", "--template=", f"--object-format={object_format}", str(repo)])
    # Every ref, not just the two branches, so an archive with more fails the inspection.
    fetch = ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head", str(bundle)]
    run(["git", "-C", str(repo), "-c", "core.logAllRefUpdates=false", *fetch, "refs/*:refs/*"])
    (repo / ".git" / "config").write_text(git_config(sha256))
    (repo / ".git" / "HEAD").write_text(HEAD)
