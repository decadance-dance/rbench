"""A trial's scrubbed repository and frozen PR (spec trial-isolation, Scrubbed repository and Local
PR lookups only; spec review-tasks, Source archive; design D3, D4).

`prepare_repository` builds the repository only from the task's archive, after checking its
SHA-256, and copies it into the agent container with the PR description:

    /rbench/repo      the work tree on `reviewed`, with branches `base` and `reviewed`
    /rbench/pr.json   {"number", "title", "body"}: what the `gh` stub (images/agent/gh) answers from
    /rbench/output/   where the stub writes the body of each `gh pr comment`

It is unpacked on the host into a fresh repository (`git init --template=`, no reflog, no
FETCH_HEAD) and copied with `docker compose cp -a`, never mounted; `-a` makes the agent's user own
it. The same inspection then runs on the host copy and inside the container, and both must find
the scrubbed repository and agree, or the trial fails with an infrastructure error before the
engine starts. The object count goes to the trial's `repository.json` for the audit, which
recounts with `inspect_repository` after the engine.
"""

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from rbench.atomic import atomic_write
from rbench.ledger.trials import RAW_DIR
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
# All a fresh `git init --template=` leaves in .git after a fetch and a checkout. `logs` (reflog),
# `shallow`, `FETCH_HEAD`, `info` (grafts) and the rest are refused.
GIT_DIR_ENTRIES = {"HEAD", "config", "index", "objects", "refs"}

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


def inspect_repository(run: Runner, repo: str, where: str) -> RepositoryState:
    """Check that `repo` is the scrubbed repository and return its state; raise `RepositoryError`.

    Exactly the branches `base` and `reviewed`, `reviewed`'s only parent `base`, `base` with none,
    HEAD on `reviewed`, no remote, no reflog or other git state, no alternates, and every object
    reachable from the two commits. Errors name `where`, never a ref or a path from the task.
    """

    def git(*argv: str) -> list[str]:
        return run(["git", "-C", repo, *argv]).splitlines()

    def refuse(reason: str) -> RepositoryError:
        return RepositoryError(f"{where}: the repository is not the scrubbed one: {reason}")

    # After the engine this output is untrusted: anything unreadable is a refusal too.
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
        if git("symbolic-ref", "HEAD") != ["refs/heads/reviewed"]:
            raise refuse("HEAD is not on reviewed")
        if git("remote"):
            raise refuse("it has a remote")
        extra = sorted(set(run(["ls", "-A", f"{repo}/.git"]).split()) - GIT_DIR_ENTRIES)
        if extra:
            # A count, not names: after the engine, .git holds whatever it wrote there.
            raise refuse(f"unexpected git state: {len(extra)} entries in .git")
        counts = dict(line.split(": ", 1) for line in git("count-objects", "-v"))
        if "alternate" in counts or counts.get("garbage") != "0":
            raise refuse("alternate or garbage object stores")
        objects = int(counts["count"]) + int(counts["in-pack"])
        reachable = len(git("rev-list", "--objects", "--all"))
        if objects != reachable:
            raise refuse(f"{objects} objects, {reachable} reachable from base and reviewed")
    except (ValueError, KeyError) as e:
        raise refuse(f"unreadable git output ({type(e).__name__})") from None
    return RepositoryState(base, reviewed, objects)


def prepare_repository(
    project: TrialProject, task: Task, archive: Path, trial_dir: Path
) -> RepositoryState:
    """Put the task's repository and PR into the started agent; record the state for the audit.

    The archive is checked against the task's SHA-256 first (`ArchiveError` names the task). Any
    other failure is a `TrialError`, raised before the engine may start.
    """
    where = "a task in set 'hidden'" if task.set == "hidden" else f"task {task.id}"
    verify_archive(task, archive)
    with tempfile.TemporaryDirectory(prefix="rbench-trial-") as tmp:
        stage = Path(tmp) / "rbench"
        _unpack(Path(archive), stage / "repo", len(task.head) == 64, where)
        expected = inspect_repository(_host_runner(where), str(stage / "repo"), where)
        pr = {"number": task.pr, "title": task.title, "body": task.description}
        (stage / PR_FILE).write_text(json.dumps(pr, indent=2, sort_keys=True) + "\n")
        (stage / "output").mkdir()
        project.copy_to_agent(stage, TRIAL_ROOT)
    inside = f"{where}, in the trial"
    found = inspect_repository(agent_runner(project, inside), REPO_DIR, inside)
    if found != expected:
        raise RepositoryError(f"{where}: the repository in the trial differs from its archive")
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
                check=False,
            )
        except OSError as e:
            raise RepositoryError(f"{where}: cannot run {argv[0]}: {e}") from e
        return _stdout(result, argv, where)

    return run


def _unpack(bundle: Path, repo: Path, sha256: bool, where: str) -> None:
    """A fresh repository holding every ref of `bundle`, checked out on `reviewed`."""
    run = _host_runner(where)
    object_format = "sha256" if sha256 else "sha1"
    run(["git", "init", "--quiet", "--template=", f"--object-format={object_format}", str(repo)])
    no_reflog = ["git", "-C", str(repo), "-c", "core.logAllRefUpdates=false"]
    # Every ref, not just the two branches, so an archive with more fails the inspection.
    fetch = ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head", str(bundle.resolve())]
    run([*no_reflog, *fetch, "refs/*:refs/*"])
    run([*no_reflog, "checkout", "--quiet", "reviewed"])
