"""Publication guard (spec review-tasks: Publication guard).

`public` is the public check `bin/check` runs, in CI too. It has no hidden directory, so it looks
for what needs none: a tracked path or file content with a task id carrying the hidden id prefix,
and tracked run output under `runs/`. It names the file and line, never the id, since CI logs are
public. The hidden canary prefix is known only in the hidden directory, so the pre-push check
covers it.

`pre-push` is the git hook `bin/pre-push`. It reads the real hidden task ids, their canaries and the
canary prefix from the directory `RBENCH_HIDDEN_DIR` names and rejects a push whose commits add any
of them. It names the offending id on stderr, the operator's terminal, and writes no file.

    python -m rbench.tasks.guard public
    python -m rbench.tasks.guard pre-push <remote> <url>   # ref lines on stdin, as git gives them
"""

import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from rbench.tasks.hidden import hidden_config, load_hidden_tasks
from rbench.tasks.loader import HIDDEN_DIR_ENV, HIDDEN_ID_PREFIX, TaskError

# The hidden prefix, then the id form the loader accepts; an id character before it means the
# prefix ends another word ("high-level"), not an id.
HIDDEN_ID = re.compile(
    rb"(?<![A-Za-z0-9_.-])" + re.escape(HIDDEN_ID_PREFIX.encode()) + rb"[a-z0-9][a-z0-9_.-]*"
)
RUNS_DIR = "runs/"


class GuardError(Exception):
    """Git failed, or the hidden directory could not be read: the guard cannot vouch."""


def _git(cwd: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(cwd), *args], check=False, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise GuardError(f"git {args[0]} failed in {cwd}: {detail}")
    return result.stdout


def public_findings(root: Path) -> list[str]:
    """Every tracked file of the checkout at `root` that must not be published, with the reason."""
    findings = []
    for name in sorted(os.fsdecode(n) for n in _git(root, "ls-files", "-z").split(b"\0")):
        if not name:
            continue
        if name.startswith(RUNS_DIR):
            findings.append(f"{name}: unpublished run output; results go out via rbench publish")
            continue
        if HIDDEN_ID.search(os.fsencode(name)):
            findings.append(f"{name}: the path holds a task id with the hidden prefix")
            continue
        path = root / name
        if path.is_symlink():
            data = os.fsencode(os.readlink(path))
        elif path.is_file():
            data = path.read_bytes()
        else:
            continue  # deleted in the work tree, or a submodule
        for number, line in enumerate(data.splitlines(), 1):
            if HIDDEN_ID.search(line):
                findings.append(f"{name}:{number}: a task id with the hidden prefix")
    return findings


def hidden_needles(environ: Mapping[str, str], cwd: Path) -> list[tuple[bytes, str]]:
    """What a push must not add, each with how the terminal names it; canaries before the prefix."""
    config = hidden_config(environ, cwd=cwd)
    tasks = load_hidden_tasks(config)
    needles = [(t.canary.encode(), f"the canary of hidden task {t.id}") for t in tasks]
    needles += [(t.id.encode(), f"hidden task id {t.id}") for t in tasks]
    needles.append((config.canary_prefix.encode(), "the hidden canary prefix"))
    return needles


def _added_text(cwd: Path, commit: str) -> bytes:
    """The commit's message and the lines, paths and names its first-parent diff adds."""
    shown = _git(
        cwd,
        "show",
        "--text",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--diff-merges=first-parent",
        "--format=%B%x00",
        commit,
    )
    message, _, patch = shown.partition(b"\0")
    # Removed lines are already where they came from; "--- a/path" is repeated by "+++ b/path".
    added = [line for line in patch.splitlines() if not line.startswith(b"-")]
    return b"\n".join([message, *added])


def push_findings(
    ref_lines: Sequence[str], needles: list[tuple[bytes, str]], cwd: Path
) -> list[str]:
    """The hidden ids and canaries the pushed refs carry, from git's pre-push stdin lines."""
    findings = []
    for line in ref_lines:
        if not line.strip():
            continue
        local_ref, local_sha, remote_ref, _ = line.split()
        if set(local_sha) == {"0"}:
            continue  # a deletion pushes no content
        refs = f"{local_ref} {remote_ref}".encode()
        texts = [(f"the ref name {remote_ref}", refs)]
        # Every commit no remote-tracking ref has: also the ones a later commit reverts.
        commits = _git(cwd, "rev-list", local_sha, "--not", "--remotes").decode().split()
        texts += [(f"commit {c[:12]}", _added_text(cwd, c)) for c in commits]
        for where, text in texts:
            for needle, label in needles:
                if needle in text:
                    findings.append(f"{where} adds {label}")
                    break
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cwd = Path.cwd()
    try:
        if args == ["public"]:
            findings = public_findings(cwd)
            for finding in findings:
                print(f"publication guard: {finding}", file=sys.stderr)
            return 1 if findings else 0
        if args[:1] == ["pre-push"]:
            if not os.environ.get(HIDDEN_DIR_ENV, "").strip():
                print(
                    f"pre-push: {HIDDEN_DIR_ENV} is not set; hidden ids and canaries not checked",
                    file=sys.stderr,
                )
                return 0
            needles = hidden_needles(os.environ, cwd)
            findings = push_findings(sys.stdin.read().splitlines(), needles, cwd)
            for finding in findings:
                print(f"pre-push: rejected: {finding}", file=sys.stderr)
            return 1 if findings else 0
    except (GuardError, TaskError, OSError) as e:
        print(f"publication guard: {e}", file=sys.stderr)
        return 1
    print("usage: python -m rbench.tasks.guard public | pre-push <remote> <url>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
