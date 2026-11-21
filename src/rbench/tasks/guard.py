"""Publication guard (spec review-tasks: Publication guard).

`public` is the check `bin/check` runs, in CI too. In every tracked path and in every tracked file's
staged and work-tree content it looks for a task id with the hidden id prefix, and it flags anything
tracked under `runs/`. With `RBENCH_HIDDEN_DIR` set, as on the operator's machine, it also looks
for the real hidden task ids, their canaries and the hidden canary prefix, which live only in the
hidden directory; a hidden directory it cannot read fails the check. It names the file by a redacted
path and a line, never the matched value, since CI logs are public.

`pre-push` is the git hook `bin/pre-push`. It reads the same values from the hidden directory and
rejects a push when a commit new to the destination adds one of them to a file, a path or its
message, or when a pushed ref name or annotated tag carries one. It may name the offending hidden id
on stderr, the operator's terminal, but never a canary or the prefix, and it writes no file.

Content is searched as raw bytes (UTF-8 and binary files) and as UTF-16 and UTF-32 in both byte
orders, with or without a byte order mark.

    python -m rbench.tasks.guard public
    python -m rbench.tasks.guard pre-push <remote> <url>   # ref lines on stdin, as git gives them
"""

import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from rbench.tasks.hidden import hidden_config, load_hidden_tasks
from rbench.tasks.loader import HIDDEN_DIR_ENV, HIDDEN_ID_PREFIX, TaskError

RUNS_DIR = "runs/"
REDACTED = "<redacted>"
GITLINK = "160000"
# (bytes before, bytes after) each ASCII character in raw bytes, UTF-16 and UTF-32.
ENCODINGS = {
    "utf-8": (b"", b""),
    "utf-16-le": (b"", b"\0"),
    "utf-16-be": (b"\0", b""),
    "utf-32-le": (b"", b"\0\0\0"),
    "utf-32-be": (b"\0\0\0", b""),
}


def _hidden_id_pattern(before: bytes, after: bytes) -> re.Pattern[bytes]:
    """The hidden prefix and then what the loader's id rule accepts after it, in one encoding.

    A letter or digit before the prefix makes it the end of another word ("high-level"). The bare
    prefix and placeholders such as the prefix and `*` or `<n>` pass; a prose word that is also a
    valid id is flagged on purpose.
    """
    b, a = re.escape(before), re.escape(after)
    prefix = b"".join(b + re.escape(bytes([c])) + a for c in HIDDEN_ID_PREFIX.encode())
    word = b + rb"[A-Za-z0-9]" + a
    char = rb"(?:" + b + rb"[a-z0-9_.-]" + a + rb")"
    return re.compile(rb"(?<!" + word + rb")" + prefix + char + rb"+")


HIDDEN_IDS = [(_hidden_id_pattern(*pad), pad[0] + b"\n" + pad[1]) for pad in ENCODINGS.values()]
HIDDEN_ID = HIDDEN_IDS[0][0]


class GuardError(Exception):
    """Git failed, or the hidden directory could not be read: the guard cannot vouch."""


@dataclass(frozen=True)
class Needle:
    """A value from the hidden directory, in every encoding, and how the hook names it."""

    forms: tuple[bytes, ...]
    label: str

    @classmethod
    def of(cls, value: str, label: str) -> "Needle":
        return cls(tuple(value.encode(e) for e in ENCODINGS), label)

    def found_in(self, data: bytes) -> bool:
        return any(form in data for form in self.forms)


@dataclass(frozen=True)
class Hidden:
    """The needles, canaries before ids before the prefix, and the values to redact."""

    needles: list[Needle]
    secrets: list[str]

    def redact(self, text: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        return text


def hidden_values(environ: Mapping[str, str], cwd: Path) -> Hidden:
    """Read the hidden directory; `TaskError` when it is not configured or cannot be read."""
    config = hidden_config(environ, cwd=cwd)
    tasks = load_hidden_tasks(config)
    needles = [Needle.of(t.canary, f"the canary of hidden task {t.id}") for t in tasks]
    needles += [Needle.of(t.id, f"hidden task id {t.id}") for t in tasks]
    needles.append(Needle.of(config.canary_prefix, "the hidden canary prefix"))
    return Hidden(
        needles, [t.canary for t in tasks] + [t.id for t in tasks] + [config.canary_prefix]
    )


def _git(cwd: Path, *args: str, input: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=False, capture_output=True, input=input
    )
    if result.returncode != 0:
        # Git's message may quote a URL, a path or a ref that holds a hidden value.
        raise GuardError(f"git {args[0]} failed with exit code {result.returncode}")
    return result.stdout


def _objects(cwd: Path, shas: Iterable[str]) -> dict[str, bytes]:
    """The content of each object, read in one `git cat-file --batch`."""
    shas = sorted(set(shas))
    out = _git(cwd, "cat-file", "--batch", input="".join(f"{s}\n" for s in shas).encode())
    objects, pos = {}, 0
    for sha in shas:
        end = out.index(b"\n", pos)
        header = out[pos:end].split()
        if len(header) != 3:
            raise GuardError(f"git cat-file cannot read object {sha}")
        size = int(header[2])
        objects[sha] = out[end + 1 : end + 1 + size]
        pos = end + 2 + size
    return objects


def _redact_path(name: str) -> str:
    return HIDDEN_ID.sub(REDACTED.encode(), os.fsencode(name)).decode(errors="replace")


def _hidden_id_lines(data: bytes, hidden: Hidden | None) -> list[tuple[int, str]]:
    """(line, what) for each hidden id form, and each hidden value when given, in `data`."""
    found = set()
    for pattern, newline in HIDDEN_IDS:
        for m in pattern.finditer(data):
            found.add((data.count(newline, 0, m.start()) + 1, "a task id with the hidden prefix"))
    for needle in hidden.needles if hidden else ():
        for form, newline in zip(needle.forms, (p[1] for p in HIDDEN_IDS), strict=True):
            start = data.find(form)
            while start != -1:
                found.add((data.count(newline, 0, start) + 1, "a value from the hidden directory"))
                start = data.find(form, start + 1)
    return sorted(found)


def public_findings(root: Path, hidden: Hidden | None = None) -> list[str]:
    """Every tracked file of the checkout at `root` that must not be published, with the reason.

    Both the staged content and the work-tree content of each file count.
    """
    entries = []
    for record in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if record:
            meta, _, name = record.partition(b"\t")
            mode, sha, _ = meta.decode().split()
            entries.append((os.fsdecode(name), mode, sha))
    staged = _objects(root, (sha for _, mode, sha in entries if mode != GITLINK))
    findings = set()
    for name, mode, sha in entries:
        shown = _redact_path(name)
        if hidden:
            shown = hidden.redact(shown)
        if name.startswith(RUNS_DIR):
            findings.add(f"{shown}: unpublished run output; results go out via rbench publish")
            continue
        if shown != name:
            findings.add(f"{shown}: the path holds a task id or value from the hidden directory")
            continue
        if mode == GITLINK:
            continue
        path = root / name
        work = None
        if path.is_symlink():
            work = os.fsencode(os.readlink(path))
        elif path.is_file():
            work = path.read_bytes()
        versions = [("", staged[sha])]
        if work is not None and work != staged[sha]:
            versions = [(" (staged)", staged[sha]), ("", work)]
        for where, data in versions:
            for line, what in _hidden_id_lines(data, hidden):
                findings.add(f"{name}:{line}{where}: {what}")
    return sorted(findings)


def _advertised(cwd: Path, remote: str) -> list[str]:
    """The objects the destination's refs point at that this repository has."""
    shas = {line.split()[0].decode() for line in _git(cwd, "ls-remote", remote).splitlines()}
    if not shas:
        return []
    check = _git(cwd, "cat-file", "--batch-check", input="".join(f"{s}\n" for s in shas).encode())
    return sorted(line.split()[0].decode() for line in check.splitlines() if b"missing" not in line)


@dataclass(frozen=True)
class Change:
    """A path a commit adds or modifies against one parent."""

    new_path: bool
    mode: str
    blob: str
    old_blob: str | None


def _changes(cwd: Path, parent: str | None, commit: str) -> dict[bytes, Change]:
    """{path: change} for each path `commit` adds or modifies against `parent`."""
    args = [parent, commit] if parent else ["--root", commit]
    fields = _git(cwd, "diff-tree", "-r", "-z", "-M", "--no-commit-id", *args).split(b"\0")
    changes, i = {}, 0
    while i < len(fields) and fields[i]:
        src_mode, dst_mode, src_sha, dst_sha, status = fields[i].decode().lstrip(":").split()
        paths = 2 if status[0] in "RC" else 1
        if status[0] != "D":
            old = None if status[0] == "A" or src_mode == GITLINK else src_sha
            changes[fields[i + paths]] = Change(status[0] in "ARC", dst_mode, dst_sha, old)
        i += 1 + paths
    return changes


HUNK = re.compile(rb"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _added_lines(cwd: Path, change: Change) -> set[int] | None:
    """The 1-based lines of the new blob a -U0 diff from the old one adds; None for all lines."""
    if change.old_blob is None:
        return None
    diff = _git(
        cwd, "diff", "-U0", "--text", "--no-color", "--no-ext-diff", change.old_blob, change.blob
    )
    added = set()
    for line in diff.splitlines():
        if m := HUNK.match(line):
            start, count = int(m[1]), 1 if m[2] is None else int(m[2])
            added.update(range(start, start + count))
    return added


def _commit_texts(cwd: Path, commit: str) -> list[tuple[str, bytes]]:
    """(where, text) for what `commit` adds: its message, new paths, and lines new to every parent."""
    header, _, message = _objects(cwd, [commit])[commit].partition(b"\n\n")
    parents = [
        line.split()[1].decode() for line in header.splitlines() if line.startswith(b"parent ")
    ]
    texts = [(f"commit {commit[:12]} message", message)]
    per_parent = [_changes(cwd, p, commit) for p in parents or [None]]
    # A path the commit leaves as one parent has it adds nothing.
    paths = sorted(set.intersection(*(set(c) for c in per_parent)))
    blobs = _objects(cwd, (c[p].blob for c in per_parent for p in paths if c[p].mode != GITLINK))
    for path in paths:
        name = os.fsdecode(path)
        changes = [c[path] for c in per_parent]
        if all(c.new_path for c in changes):
            texts.append((f"commit {commit[:12]} path {name}", path))
        if changes[0].mode == GITLINK:
            continue  # a submodule's commit is not in this repository
        lines = blobs[changes[0].blob].split(b"\n")
        added = set(range(1, len(lines) + 1))
        for change in changes:
            new = _added_lines(cwd, change)
            if new is not None:
                added &= new
        # Occurrences count, not values: a second copy of a published line is added too.
        text = b"\n".join(lines[n - 1] for n in sorted(added) if n <= len(lines))
        texts.append((f"commit {commit[:12]} file {name}", text))
    return texts


def push_findings(
    ref_lines: Sequence[str], destination: str, hidden: Hidden, cwd: Path
) -> list[str]:
    """What the pushed refs add from the hidden directory, from git's pre-push stdin lines.

    `destination` is the URL git pushes to, which may differ from the remote's fetch URL.
    """
    pushes = []
    for line in ref_lines:
        if line.strip():
            _, local_sha, remote_ref, remote_sha = line.split()
            # A deletion pushes no content, an unchanged ref nothing new.
            if set(local_sha) != {"0"} and local_sha != remote_sha:
                pushes.append((local_sha, remote_ref))
    if not pushes:
        return []
    # What the destination advertises is already there; a stale tracking ref proves nothing.
    exclude = [f"^{sha}" for sha in _advertised(cwd, destination)]
    findings = []
    for local_sha, remote_ref in pushes:
        ref = hidden.redact(remote_ref)
        texts = [(f"the ref name {ref}", remote_ref.encode())]
        target = local_sha
        while (kind := _git(cwd, "cat-file", "-t", target).strip().decode()) == "tag":
            raw = _objects(cwd, [target])[target]
            texts.append((f"the annotated tag {target[:12]} for {ref}", raw))
            target = raw.split(b"\n", 1)[0].split()[1].decode()
        if kind != "commit":
            findings.append(f"{ref} points at a {kind}; the guard scans only commits and tags")
            continue
        stdin = "\n".join([local_sha, *exclude]) + "\n"
        commits = _git(cwd, "rev-list", "--stdin", input=stdin.encode()).decode().split()
        for commit in commits:
            texts += _commit_texts(cwd, commit)
        for where, text in texts:
            for needle in hidden.needles:
                if needle.found_in(text):
                    findings.append(f"{hidden.redact(where)} adds {needle.label}")
                    break
    return findings


def _configured(environ: Mapping[str, str]) -> bool:
    return bool(environ.get(HIDDEN_DIR_ENV, "").strip())


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cwd = Path.cwd()
    try:
        if args == ["public"]:
            hidden = hidden_values(os.environ, cwd) if _configured(os.environ) else None
            findings = public_findings(cwd, hidden)
            for finding in findings:
                print(f"publication guard: {finding}", file=sys.stderr)
            return 1 if findings else 0
        if len(args) == 3 and args[0] == "pre-push":
            if not _configured(os.environ):
                print(
                    f"pre-push: {HIDDEN_DIR_ENV} is not set; hidden ids and canaries not checked",
                    file=sys.stderr,
                )
                return 0
            hidden = hidden_values(os.environ, cwd)
            findings = push_findings(sys.stdin.read().splitlines(), args[2], hidden, cwd)
            for finding in findings:
                print(f"pre-push: rejected: {finding}", file=sys.stderr)
            return 1 if findings else 0
    except (GuardError, TaskError) as e:
        print(f"publication guard: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"publication guard: cannot read a file: {e.strerror}", file=sys.stderr)
        return 1
    print("usage: python -m rbench.tasks.guard public | pre-push <remote> <url>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
