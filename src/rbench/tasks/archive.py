"""The source archive of an import (spec review-tasks, Source archive; design D4, D11).

`build_archive` fetches a PR's base and head commits once and writes a git bundle of the scrubbed
repository (spec trial-isolation, Scrubbed repository):

    base      a commit of the PR's base tree, no parent
    reviewed  a commit of the PR's head tree on top of `base`

The bundle holds those two branches and the objects they reach, nothing else: no remotes, tags,
reflog or earlier history, as none of these travel in a bundle. Both commits have a fixed author,
committer, date and message, and every delta is recomputed from those two commits, so the same
base and head give the same commit ids and, with the same git version, the same bundle bytes,
however the source packed its objects.

Git runs with no system or user configuration and no inherited `GIT_*` variables, so the
operator's settings (URL rewrites, identities, hooks) cannot change what is fetched or built.
"""

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rbench.tasks.license import LicenseError, read_license
from rbench.tasks.loader import COMMIT, TASK_SETS

# The sets whose archives are published, so their source license must allow redistribution.
LICENSED_SETS = ("reviewbench", "ours")

IDENTITY = "author"
EMAIL = "author@example.com"
DATE = "@946684800 +0000"  # 2000-01-01T00:00:00Z


def archive_name(sha256: str) -> str:
    """The name of the archive with this SHA-256, in an archive directory and as a release asset."""
    return f"{sha256}.bundle"


class ArchiveError(ValueError):
    """An import that cannot build its archive; the message names the repository."""


@dataclass(frozen=True)
class SourceArchive:
    """A built archive: the bundle, its SHA-256 and the license read at the reviewed head.

    `license` is the SPDX id and `license_file` the path of the license file, which the `reviewed`
    tree keeps; a hidden task skips the check, so both are None for it.
    """

    path: Path
    sha256: str
    license: str | None
    license_file: str | None


def build_archive(
    repo: str, source: str, base: str, head: str, out: Path, task_set: str
) -> SourceArchive:
    """Fetch `base` and `head` of `repo` from `source` (a git URL or path) and bundle them to `out`.

    For the `reviewbench` and `ours` sets the license of `repo` at `head` is checked first, through
    `gh` (`read_license`); a refused license raises `LicenseError` and writes nothing. Any other
    failure raises `ArchiveError`. `out` is replaced only by a complete bundle.
    """
    if task_set not in TASK_SETS:
        raise ArchiveError(f"{repo}: unknown task set {task_set!r}")
    for name, commit in (("base", base), ("head", head)):
        if not COMMIT.fullmatch(commit):
            raise ArchiveError(f"{repo}: {name} {commit!r} is not a full commit id")
    if source.startswith("-"):
        raise ArchiveError(f"{repo}: source {source!r} looks like an option, not a repository")
    if len(base) != len(head):
        raise ArchiveError(f"{repo}: base and head use different object formats")

    found = read_license(repo, head) if task_set in LICENSED_SETS else None

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=f".{out.name}.") as tmp:
        git = _Git(repo, Path(tmp) / "repo")
        object_format = "sha256" if len(head) == 64 else "sha1"
        git("init", "--quiet", "--bare", f"--object-format={object_format}")
        git("fetch", "--quiet", "--no-tags", "--depth=1", source, base, head)
        for name, commit in (("base", base), ("head", head)):
            if git("cat-file", "-t", commit, check=False) != "commit":
                raise ArchiveError(f"{repo}: {name} {commit} is not a commit in {source}")

        if found and git("cat-file", "-t", f"{head}:{found.path}", check=False) != "blob":
            raise LicenseError(f"{repo}: license file {found.path} is not in the reviewed tree")

        base_commit = git("commit-tree", f"{base}^{{tree}}", "-m", "base")
        reviewed = git("commit-tree", f"{head}^{{tree}}", "-p", base_commit, "-m", "reviewed")
        git("update-ref", "refs/heads/base", base_commit)
        git("update-ref", "refs/heads/reviewed", reviewed)
        # Recompute every delta from the two commits alone, single-threaded, so the bundle bytes do
        # not depend on how the source happened to pack its objects.
        git("repack", "-a", "-d", "-f", "-q", "--window=10", "--depth=50")
        bundle = Path(tmp) / "source.bundle"
        git("bundle", "create", "--quiet", str(bundle), "refs/heads/base", "refs/heads/reviewed")
        with bundle.open("rb") as f:
            sha256 = hashlib.file_digest(f, "sha256").hexdigest()
        os.replace(bundle, out)
    if found is None:
        return SourceArchive(out, sha256, None, None)
    return SourceArchive(out, sha256, found.id, found.path)


class _Git:
    def __init__(self, repo: str, git_dir: Path) -> None:
        self.repo = repo
        self.dir = git_dir
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env |= {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "pack.threads",
            "GIT_CONFIG_VALUE_0": "1",
            "GIT_AUTHOR_NAME": IDENTITY,
            "GIT_AUTHOR_EMAIL": EMAIL,
            "GIT_AUTHOR_DATE": DATE,
            "GIT_COMMITTER_NAME": IDENTITY,
            "GIT_COMMITTER_EMAIL": EMAIL,
            "GIT_COMMITTER_DATE": DATE,
        }

    def __call__(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", f"--git-dir={self.dir}", *args],
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0 and check:
            stderr = result.stderr.decode(errors="replace").strip()
            raise ArchiveError(f"{self.repo}: git {args[0]} failed: {stderr}")
        return result.stdout.decode(errors="replace").strip()
