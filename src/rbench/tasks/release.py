"""Published source archives of the public task sets (spec review-tasks, Source archive; design D11).

Each version of a public set (`reviewbench`, `ours`) has one GitHub release, tagged
`tasks-<set>-v<version>`, whose assets are the source archives of its tasks. An asset is named
`<archive sha256>.bundle`, so a task's `archive_sha256` names the asset to download and the file
to verify; tasks that share an archive share one asset. Hidden archives are never published: they
stay next to the hidden tasks.

`rbench tasks release` creates a version's release once: it targets the commit that last changed
the set's manifest (which must be on GitHub), and it refuses a tag that already has a release, a
draft or a git tag, so a published version never changes. A draft left by an interrupted upload
is deleted by hand (`gh release delete <tag>`) before publishing again.

`rbench tasks fetch` downloads the archives of a set into a local, content-addressed directory
(default `$XDG_CACHE_HOME/rbench/archives`): every download is verified against the SHA-256 that
the task records, which the set's manifest pins, before it is renamed into place, so the
directory never holds a partial or unverified archive. `verify_archive` is the check a trial runs
before it builds its repository from an archive.
"""

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from rbench.tasks.archive import LICENSED_SETS, ArchiveError, archive_name
from rbench.tasks.loader import REPO, Task, TaskError
from rbench.tasks.manifest import MANIFEST_FILE, _git, _repo_path, load_task_set

# The repository whose releases carry the public archives; a fork reruns the same archives.
RELEASE_REPO = "decadance-dance/rbench"


def release_tag(task_set: str, version: int) -> str:
    return f"tasks-{task_set}-v{version}"


def default_archive_dir() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", ""))
    # The XDG spec says a relative path is invalid and must be ignored.
    if not cache.is_absolute():
        cache = Path.home() / ".cache"
    return cache / "rbench" / "archives"


def _label(task: Task) -> str:
    # A hidden task's id must not reach an error message.
    return "a task in set 'hidden'" if task.set == "hidden" else f"task {task.id}"


def _sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def verify_archive(task: Task, path: Path) -> Path:
    """Return `path` once its SHA-256 is the one `task` records; otherwise the trial must not start.

    Raise `ArchiveError` naming the task when the archive is missing, unreadable or altered.
    """
    path = Path(path)
    try:
        actual = _sha256(path)
    except OSError as e:
        raise ArchiveError(
            f"{_label(task)}: cannot read its source archive: {e.strerror}"
        ) from None
    if actual != task.archive_sha256:
        raise ArchiveError(
            f"{_label(task)}: source archive SHA-256 does not match the one the task records;"
            " the archive was altered or is not this task's"
        )
    return path


def _public_set(set_dir: Path, task_set: str, repo: str) -> tuple[int, list[Task]]:
    if task_set not in LICENSED_SETS:
        raise TaskError(
            f"only the public sets {', '.join(LICENSED_SETS)} have released archives,"
            f" not {task_set!r}"
        )
    if not REPO.fullmatch(repo):
        raise TaskError(f"{repo!r} is not an owner/name repository")
    # load_task_set refuses a hidden task and checks every task against the manifest, which pins
    # each archive_sha256.
    manifest, tasks = load_task_set(set_dir, task_set)
    return manifest.version, tasks


def _gh(args: list[str], what: str, **kwargs) -> str:
    """Run `gh`; return its stdout, or raise `ArchiveError` starting with `what`."""
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    try:
        result = subprocess.run(["gh", *args], stdin=subprocess.DEVNULL, check=False, **kwargs)
    except OSError as e:
        raise ArchiveError(f"{what}: cannot run gh: {e}") from None
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode(errors="replace").strip()
        raise ArchiveError(f"{what}: gh failed: {stderr}")
    return (result.stdout or b"").decode(errors="replace")


def _manifest_commit(set_dir: Path) -> str:
    """The commit that last changed the manifest of `set_dir`, holding the whole set as it is now.

    The set must be committed, and the set directory at that commit must equal the working tree,
    so the release's source holds every task the manifest lists.
    """
    found = _git(set_dir, ["rev-parse", "--show-toplevel"], f"{set_dir} is not in a git repository")
    root = Path(found.strip())
    # Literal pathspecs: a task root such as `tasks[1]` must not match `tasks1` as a glob.
    literal_set = f":(literal){_repo_path(root, set_dir)}"
    literal_manifest = f":(literal){_repo_path(root, set_dir / MANIFEST_FILE)}"
    status = _git(
        root,
        ["status", "--porcelain", "--untracked-files=all", "--", literal_set],
        f"cannot read the git status of {set_dir}",
    )
    if status.strip():
        raise TaskError(f"{set_dir} has uncommitted changes; commit the set before releasing it")
    commit = _git(
        root,
        ["log", "-1", "--format=%H", "HEAD", "--", literal_manifest],
        f"cannot find the commit of {set_dir / MANIFEST_FILE}",
    ).strip()
    if not commit:
        raise TaskError(f"{set_dir / MANIFEST_FILE} is not committed")
    differs = _git(
        root,
        ["diff", "--name-only", commit, "--", literal_set],
        f"cannot compare {set_dir} with commit {commit}",
    )
    if differs.strip():
        raise TaskError(
            f"{set_dir} at commit {commit}, which last changed its manifest, differs from the set"
            " being released; commit the tasks together with the manifest change"
        )
    return commit


def publish_release(
    set_dir: Path, task_set: str, archive_dir: Path, repo: str = RELEASE_REPO
) -> str:
    """Create the release of the current version of `task_set`, with every archive as an asset.

    Each archive is verified first. The release targets the commit that last changed the manifest,
    which must be on `repo`; an existing release, draft or tag of this version is refused. Return
    the tag.
    """
    set_dir = Path(set_dir)
    version, tasks = _public_set(set_dir, task_set, repo)
    if not tasks:
        raise TaskError(f"task set {task_set!r} has no tasks to release")
    archive_dir = Path(archive_dir)
    # Only hash-named basenames reach gh, run inside the archive directory: gh expands an asset
    # argument as a glob and splits it at '#', which a full path may contain.
    assets = sorted({_release_asset(t, archive_dir).name for t in tasks})
    tag = release_tag(task_set, version)
    target = _manifest_commit(set_dir)
    _gh(
        ["api", f"repos/{repo}/commits/{target}", "--jq", ".sha"],
        f"release {tag}: commit {target}, which holds the manifest, is not on {repo}; push it first",
    )
    # Drafts are listed only here, not by gh's own preflight, which checks published releases.
    releases = _gh(
        ["api", "--paginate", f"repos/{repo}/releases", "--jq", ".[].tag_name"],
        f"release {tag}: cannot list the releases of {repo}",
    )
    if tag in releases.split():
        raise ArchiveError(
            f"release {tag}: {repo} already has a release or draft with this tag; a published"
            " version never changes (a changed task needs a new set version), and a draft left by"
            f" an interrupted upload must be deleted by hand: gh release delete {tag} --repo {repo}"
        )
    refs = _gh(
        ["api", f"repos/{repo}/git/matching-refs/tags/{tag}", "--jq", ".[].ref"],
        f"release {tag}: cannot list the tags of {repo}",
    )
    if f"refs/tags/{tag}" in refs.split():
        raise ArchiveError(
            f"release {tag}: {repo} already has the tag {tag} without a release; a release would"
            " keep its commit instead of the manifest's. Delete the tag by hand if it is stale"
        )
    notes = (
        f"Source archives of the `{task_set}` task set, version {version}. Each asset is named by"
        " its SHA-256; `rbench tasks fetch` downloads and verifies them against the manifest."
    )
    _gh(
        ["release", "create", tag, *assets, "--repo", repo, "--target", target]
        + ["--title", tag, "--notes", notes, "--latest=false"],
        f"release {tag}",
        cwd=archive_dir,
    )
    return tag


def _release_asset(task: Task, archive_dir: Path) -> Path:
    path = archive_dir / archive_name(task.archive_sha256)
    if not path.exists():
        raise ArchiveError(
            f"{_label(task)}: {archive_dir} has no {path.name}; an archive from an older import,"
            f" named <task-id>-<sha256>.bundle, must be imported again or renamed to {path.name}"
        )
    return verify_archive(task, path)


def fetch_archives(
    set_dir: Path, task_set: str, archive_dir: Path, repo: str = RELEASE_REPO
) -> list[str]:
    """Download the archives of `task_set` that `archive_dir` lacks; return those tasks' ids.

    An archive already present and matching is kept. A download is verified before it is renamed
    into place; a mismatch leaves nothing behind and raises `ArchiveError` naming the task.
    """
    version, tasks = _public_set(Path(set_dir), task_set, repo)
    tag = release_tag(task_set, version)
    archive_dir = Path(archive_dir)
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ArchiveError(f"archive directory {archive_dir}: {e.strerror}") from None
    fetched = []
    for task in tasks:
        try:
            if _fetch_one(task, tag, archive_dir, repo):
                fetched.append(task.id)
        except OSError as e:
            raise ArchiveError(
                f"{_label(task)}: archive directory {archive_dir}: {e.strerror}"
            ) from None
    return fetched


def _fetch_one(task: Task, tag: str, archive_dir: Path, repo: str) -> bool:
    """Download and install the archive of `task` unless a matching one is there; True if fetched."""
    dest = archive_dir / archive_name(task.archive_sha256)
    if dest.is_file() and _sha256(dest) == task.archive_sha256:
        return False
    fd, tmp = tempfile.mkstemp(dir=archive_dir, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            _gh(
                ["release", "download", tag, "--repo", repo]
                + ["--pattern", dest.name, "--output", "-"],
                f"{_label(task)}: download from release {tag}",
                stdout=f,
            )
            f.flush()
            os.fsync(f.fileno())
        verify_archive(task, Path(tmp))
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tasks-root",
        type=Path,
        default=Path("tasks"),
        help="holds one directory per public set (default: tasks)",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=None,
        help="where archives are kept, named by SHA-256 (default: $XDG_CACHE_HOME/rbench/archives)",
    )
    parser.add_argument(
        "--repo",
        default=RELEASE_REPO,
        help=f"GitHub repository whose releases carry the archives (default: {RELEASE_REPO})",
    )


def add_fetch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--set",
        dest="sets",
        action="append",
        choices=LICENSED_SETS,
        help="a public set to fetch; repeat for more (default: every public set under the root)",
    )
    _common_arguments(parser)


def add_release_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("set", choices=LICENSED_SETS, help="the public set to release")
    _common_arguments(parser)


def run_fetch(args: argparse.Namespace) -> int:
    archive_dir = args.archive_dir or default_archive_dir()
    sets = args.sets or [s for s in LICENSED_SETS if (args.tasks_root / s).is_dir()]
    if not sets:
        print(f"rbench tasks fetch: no public task set under {args.tasks_root}", file=sys.stderr)
        return 1
    try:
        for task_set in sets:
            fetched = fetch_archives(args.tasks_root / task_set, task_set, archive_dir, args.repo)
            print(f"{task_set}: fetched {len(fetched)} archives into {archive_dir}")
    except (TaskError, ArchiveError) as e:
        print(f"rbench tasks fetch: {e}", file=sys.stderr)
        return 1
    return 0


def run_release(args: argparse.Namespace) -> int:
    archive_dir = args.archive_dir or default_archive_dir()
    try:
        tag = publish_release(args.tasks_root / args.set, args.set, archive_dir, args.repo)
    except (TaskError, ArchiveError) as e:
        print(f"rbench tasks release: {e}", file=sys.stderr)
        return 1
    print(f"{args.set}: released {tag} on {args.repo}")
    return 0
