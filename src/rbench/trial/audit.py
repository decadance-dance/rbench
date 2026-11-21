"""The contamination audit after each trial (spec trial-isolation, Contamination audit; design D3,
D4, D5).

After the engine, the executor (#55) copies the proxy log, the shim log and the transcripts out of
the trial (`TrialProject.copy_proxy_log`, `collect`), calls `audit_trial` while the agent still
runs, so that it can recount the repository's objects, and then `record_audit`. A trial is
contaminated, and excluded from scores, when:
- the proxy log shows a successful connection outside the allowlist: a line for anything but
  `CONNECT <allowlisted host>:443` on which squid connected upstream (an address, not `-`) or
  answered without an error status;
- the canary of any loaded task, public or hidden, appears in any file of the trial directory;
- the repository's object count differs from the one recorded before the engine (D4);
- a transcript session does not bind to exactly one origin launch in the shim log (D5).

Evidence the audit cannot read fails closed, as contamination: a malformed proxy log record or shim
log record, an unreadable or non-regular file of the trial directory, a proxy log, shim log or
transcript root that is not a regular file (or directory) inside the trial directory reached
without a symlink, and an object count it cannot read. Every other proxy line outside the
allowlist is a refused attempt: it does not contaminate, and `audit.json` lists it for run health
(spec benchmark-report). The trial's own `status.json` and `audit.json` are not evidence.

Proxy diagnostics name only the destination (the CONNECT authority, or a URL's scheme, host and
port), never a path, query or user part. Reasons and refused attempts are untrusted text that goes
to run health, so every known hidden value (a hidden task's id, its canary and the hidden canary
prefix) is redacted from them, and a hidden task's canary is reported by its set only (spec
review-tasks); lone surrogates are escaped so the record is always valid UTF-8.

Memory is bounded per file: the canary scan maps the file and decodes JSON escapes (`\\uXXXX`,
`\\n`, ...) in windows of `SCAN_WINDOW` bytes, so an escaped spelling of a canary is found in a
string of any length without decoding the string whole.
"""

import ipaddress
import mmap
import os
import re
import stat
from collections.abc import Callable, Collection, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlsplit

from rbench.atomic import atomic_write
from rbench.ledger.trials import (
    AUDIT_FILE,
    STATUS_FILE,
    LedgerError,
    dump_json,
    read_status,
    set_state,
)
from rbench.tasks.loader import Task
from rbench.trial.images import IMAGES_DIR
from rbench.trial.repository import (
    REPO_DIR,
    RepositoryError,
    RepositoryState,
    Runner,
    count_objects,
)
from rbench.trial.sessions import read_shim_log, unbound_sessions

ALLOWLIST = IMAGES_DIR / "proxy" / "allowlist"
REDACTED = "<redacted>"
# limit: what one trial's evidence may add to its audit record; the rest is counted, not listed.
MAX_LISTED = 100
# Squid caps a URL at 8 KiB; a longer line is not one of its records.
MAX_PROXY_LINE = 64 << 10
CANARY_SUFFIX_LENGTH = 32  # rbench.tasks.loader.CANARY_SUFFIX
# The squid log record (images/proxy/squid.conf): time, client, result/status, method, target,
# upstream address. A method is an HTTP token; squid escapes a target to printable ASCII.
_TIME = re.compile(r"[0-9]+\.[0-9]{3}")
_RESULT = re.compile(r"[A-Z_]+/[0-9]{3}")
_METHOD = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_TARGET = re.compile(r"[!-~]+")
# A JSON escape, with the low half of a surrogate pair; at most 12 bytes.
_ESCAPE = re.compile(rb'\\(?:u([0-9a-fA-F]{4})(?:\\u([dD][c-fC-F][0-9a-fA-F]{2}))?|(["\\/bfnrt]))')
_ESCAPE_MAX = 12
_SIMPLE_ESCAPES = {b'"': b'"', b"\\": b"\\", b"/": b"/", b"b": b"\b", b"f": b"\f", b"n": b"\n"}
_SIMPLE_ESCAPES |= {b"r": b"\r", b"t": b"\t"}
SCAN_WINDOW = 256 << 10


@dataclass(frozen=True)
class Audit:
    """Why a trial is contaminated (empty when it is not), and its distinct refused connection
    attempts in order of first appearance, each `{"method", "target", "result", "count"}` where
    `target` is the destination only, with
    the number of further refused attempts not listed."""

    reasons: tuple[str, ...] = ()
    refused: tuple[dict, ...] = ()
    refused_omitted: int = 0

    @property
    def contaminated(self) -> bool:
        return bool(self.reasons)


def read_allowlist(path: Path = ALLOWLIST) -> frozenset[str]:
    """The hosts of the proxy's allowlist file: one per line, `#` comments."""
    lines = (line.split("#", 1)[0].strip() for line in path.read_text("utf-8").splitlines())
    return frozenset(line.lower() for line in lines if line)


def audit_trial(
    trial_dir: Path,
    tasks: Iterable[Task],
    *,
    proxy_log: Path,
    shim_log: Path,
    transcripts: Path,
    repository: RepositoryState,
    run: Runner,
    allowlist: Collection[str] | None = None,
) -> Audit:
    """Audit one trial after its engine ended.

    `tasks` are all loaded tasks of every set, not only the trial's; `repository` is the state
    `prepare_repository` recorded, and `run` runs commands in the trial's agent container.
    """
    tasks = tuple(tasks)
    redact = _redactor(tasks)
    hosts = read_allowlist() if allowlist is None else allowlist
    reasons: list[str] = []
    refused: list[dict] = []
    omitted = 0
    if problem := _evidence(trial_dir, proxy_log, "the proxy log"):
        reasons.append(problem)
    else:
        reasons, refused, omitted = audit_proxy_log(proxy_log, hosts)
    reasons += _canaries(Path(trial_dir), tasks)
    try:
        objects = count_objects(run, REPO_DIR, "the trial's repository")
    except RepositoryError as e:
        reasons.append(f"the repository's object count cannot be read after the engine: {e}")
    else:
        if objects != repository.objects:
            reasons.append(
                f"the repository's object count changed from {repository.objects} to {objects}"
            )
    problems = [
        _evidence(trial_dir, shim_log, "the shim log"),
        _evidence(trial_dir, transcripts, "the transcript root", directory=True),
    ]
    if any(problems):
        reasons += [p for p in problems if p]
    else:
        try:
            reasons += unbound_sessions(read_shim_log(shim_log), transcripts)
        # ValueError: SessionError, or bytes that are not UTF-8; RecursionError: deep JSON.
        except (OSError, ValueError, RecursionError) as e:
            reasons.append(f"the shim log or a transcript cannot be read: {e}")
    # Errors name files by host path; the record names them within the trial directory.
    within = f"{Path(trial_dir)}{os.sep}"
    reasons = [_safe(redact(reason.replace(within, ""))) for reason in reasons]
    refused = [
        {k: _safe(redact(v)) if isinstance(v, str) else v for k, v in r.items()} for r in refused
    ]
    return Audit(tuple(reasons), tuple(refused), omitted)


def _safe(text: str) -> str:
    """`text` as valid UTF-8: a lone surrogate from decoded JSON is escaped."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _evidence(trial_dir: Path, path: Path, what: str, directory: bool = False) -> str | None:
    """Why `path` cannot be read as evidence, or None: it must be a regular file (a directory when
    `directory`) inside the trial directory, reached without a symlink. A missing directory is an
    empty one: `collect` creates the transcript root only when it copies a transcript."""
    root, target = Path(os.path.abspath(trial_dir)), Path(os.path.abspath(path))
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        parts = ()
    if not parts:
        return f"{what} is not inside the trial directory, so it cannot be audited"
    current = root
    for part in parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return None if directory else f"{what} is missing"
        except OSError as e:
            return f"{what} cannot be read: {e.strerror or e}"
        if stat.S_ISLNK(mode):
            return f"{what} is reached through a symlink, so it cannot be audited"
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        kind = "directory" if directory else "regular file"
        return f"{what} is not a {kind}, so it cannot be audited"
    return None


def _open_regular(path: Path) -> BinaryIO:
    """`path` opened for reading, refused unless it is a regular file and not a symlink: a FIFO
    must not block the audit, and a link must not lead it out of the trial directory."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    f = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        f.close()
        raise OSError(f"{path.name} is not a regular file")
    return f


def record_audit(trial_dir: Path, audit: Audit) -> None:
    """Write `audit.json` for run health; a contaminated trial gets that state and its reasons.

    A contaminated trial's audit is final: recording it again is a no-op, and a different audit of
    it is refused (`LedgerError`), so `audit.json` and `status.json` always tell the same story.
    """
    trial_dir = Path(trial_dir)
    record = dump_json(
        {
            "contaminated": list(audit.reasons),
            "refused": list(audit.refused),
            "refused_omitted": audit.refused_omitted,
        }
    )
    if read_status(trial_dir).state == "contaminated":
        try:
            if (trial_dir / AUDIT_FILE).read_bytes() == record:
                return
        except OSError:
            pass
        raise LedgerError(f"{trial_dir}: the trial is contaminated; its audit stays as recorded")
    atomic_write(trial_dir / AUDIT_FILE, record)
    if audit.contaminated:
        set_state(trial_dir, "contaminated", "; ".join(audit.reasons))


def audit_proxy_log(log: Path, allowlist: Collection[str]) -> tuple[list[str], list[dict], int]:
    """Successful connections outside the allowlist, the distinct refused attempts and how many
    more were not listed, from the squid log. A record is validated whole before it is classified;
    a malformed one fails closed. Memory stays bounded: records stream, and at most `MAX_LISTED`
    reasons and distinct refused attempts are kept."""
    hosts = {host.lower() for host in allowlist}
    reasons: list[str] = []
    unlisted = 0
    refused: dict[tuple[str, str, str], int] = {}
    omitted = 0
    try:
        with _open_regular(log) as f:
            n = 0
            while line := f.readline(MAX_PROXY_LINE + 1):
                n += 1
                if len(line) > MAX_PROXY_LINE and not line.endswith(b"\n"):
                    while (rest := f.readline(MAX_PROXY_LINE)) and not rest.endswith(b"\n"):
                        pass
                    fields = None
                else:
                    fields = _proxy_record(line)
                if fields is None:
                    reason = (
                        f"proxy log line {n} is not a valid record, so it may hide a connection"
                    )
                else:
                    _, _, result, method, target, upstream = fields
                    host, _, port = target.rpartition(":")
                    if method == "CONNECT" and port == "443" and host.lower() in hosts:
                        continue
                    target = _destination(method, target)
                    if upstream == "-" and result[-3] in "45":
                        key = (method, target, result)
                        if key in refused:
                            refused[key] += 1
                        elif len(refused) < MAX_LISTED:
                            refused[key] = 1
                        else:
                            omitted += 1
                        continue
                    reason = (
                        f"the proxy log shows a connection outside the allowlist: {method} "
                        f"{target} ({result}, line {n})"
                    )
                if len(reasons) < MAX_LISTED:
                    reasons.append(reason)
                else:
                    unlisted += 1
    except OSError as e:
        reasons.append(f"the proxy log cannot be read: {e.strerror or e}")
    if unlisted:
        reasons.append(f"{unlisted} more proxy log lines outside the allowlist or not valid")
    listed = [
        {"method": m, "target": t, "result": r, "count": c} for (m, t, r), c in refused.items()
    ]
    return reasons, listed, omitted


def _destination(method: str, target: str) -> str:
    """Where a request went, without the path, query or user part that may carry anything: the
    CONNECT authority, or a URL's scheme, host and port, percent-decoded so redaction sees it."""
    if method == "CONNECT":
        return unquote(target)
    try:
        url = urlsplit(target)
    except ValueError:
        return "-"
    host = url.netloc.rpartition("@")[2]
    if host:
        return f"{url.scheme}://{unquote(host)}"
    return f"{url.scheme}:" if url.scheme else "-"


def _proxy_record(line: bytes) -> list[str] | None:
    """The six fields of a squid log record, or None when any is malformed."""
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return None
    fields = text.removesuffix("\n").split(" ")
    if len(fields) != 6:
        return None
    time, client, result, method, target, upstream = fields
    if not (
        _TIME.fullmatch(time)
        and _RESULT.fullmatch(result)
        and _METHOD.fullmatch(method)
        and _TARGET.fullmatch(target)
        and _address(client)
        and (upstream == "-" or _address(upstream))
    ):
        return None
    return fields


def _address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _redactor(tasks: tuple[Task, ...]) -> Callable[[str], str]:
    """Replaces every hidden task's id and canary, and the hidden canary prefix, in a text."""
    values = set()
    for task in tasks:
        if task.set == "hidden":
            values |= {task.id, task.canary, task.canary[:-CANARY_SUFFIX_LENGTH]}
    values.discard("")
    if not values:
        return lambda text: text
    by_length = sorted(values, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(v) for v in by_length), re.IGNORECASE)
    return lambda text: pattern.sub(REDACTED, text)


def _canaries(trial_dir: Path, tasks: tuple[Task, ...]) -> list[str]:
    """Every file of the trial directory that holds a task's canary, in its raw bytes or in a
    decoded JSON string; and every file the audit cannot read."""
    owners: dict[str, str] = {}
    for task in tasks:
        # A hidden task is named only by its set (spec review-tasks).
        who = "a task in set 'hidden'" if task.set == "hidden" else f"task {task.id}"
        owners.setdefault(task.canary, who)
    if not owners:
        raise ValueError("the canary scan needs the loaded tasks")
    by_length = sorted(owners, key=len, reverse=True)
    raw = re.compile(b"|".join(re.escape(c.encode()) for c in by_length))
    longest = len(by_length[0].encode())
    problems: list[str] = []
    hits = []
    ledger = {Path(trial_dir) / STATUS_FILE, Path(trial_dir) / AUDIT_FILE}
    for path in _files(trial_dir, problems):
        if path in ledger:
            continue  # the audit's own record, written from earlier evidence
        label = path.relative_to(trial_dir).as_posix()
        try:
            found = _found(path, raw, longest)
        except (OSError, ValueError) as e:  # ValueError: mmap of a file that changed size
            problems.append(f"{label} cannot be read: {getattr(e, 'strerror', None) or e}")
            continue
        for who in sorted({owners[c] for c in found}):
            hits.append(f"the canary of {who} appears in {label}")
    return hits + problems


def _files(root: Path, problems: list[str]) -> list[Path]:
    """Regular files below `root`. Anything else, and what cannot be listed, is a problem: a
    symlink is not followed, so no file outside the trial directory is read."""

    def label(path: str | Path) -> str:
        return Path(path).relative_to(root).as_posix()

    def failed(e: OSError) -> None:
        problems.append(f"{label(e.filename or root)} cannot be listed: {e.strerror or e}")

    found = []
    for directory, dirs, names in os.walk(root, onerror=failed):
        for name in sorted(dirs + names):
            path = Path(directory) / name
            try:
                mode = path.lstat().st_mode
            except OSError as e:
                problems.append(f"{label(path)} cannot be read: {e.strerror or e}")
                continue
            if stat.S_ISREG(mode):
                found.append(path)
            elif not stat.S_ISDIR(mode):
                problems.append(f"{label(path)} is not a regular file, so it cannot be audited")
    return sorted(found)


def _found(path: Path, raw: re.Pattern[bytes], longest: int) -> set[str]:
    """The canaries in a file's bytes and in its bytes with JSON escapes decoded, kept as a set of
    distinct values while the matches stream by."""
    with _open_regular(path) as f:
        if os.fstat(f.fileno()).st_size == 0:
            return set()
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
            found = {m.group() for m in raw.finditer(data)}
            tail = b""
            for window in _unescaped(data):
                text = tail + window
                found.update(m.group() for m in raw.finditer(text))
                tail = text[-(longest - 1) :] if longest > 1 else b""
            return {c.decode("utf-8", "surrogatepass") for c in found}


def _unescaped(data: mmap.mmap) -> Iterator[bytes]:
    """`data` with every JSON escape decoded to UTF-8, in windows of about `SCAN_WINDOW` bytes.

    Decoding runs over the whole file, inside strings or not: that only adds matches. A window ends
    before any escape that may run past it, at a point the left-to-right scan also passes outside
    an escape, so the next window starts in step with it.
    """
    start, size = 0, len(data)
    while start < size:
        end = min(start + SCAN_WINDOW, size)
        stop = end if end == size else end - _ESCAPE_MAX
        out, pos = bytearray(), start
        for m in _ESCAPE.finditer(data, start, end):
            if m.start() >= stop:
                break
            out += data[pos : m.start()]
            out += _unescape(m)
            pos = m.end()
        if pos < stop:
            out += data[pos:stop]
            pos = stop
        yield bytes(out)
        start = pos


def _unescape(m: re.Match[bytes]) -> bytes:
    if m[3]:
        return _SIMPLE_ESCAPES[m[3]]
    high = int(m[1], 16)
    if m[2] and 0xD800 <= high < 0xDC00:
        return chr(0x10000 + ((high - 0xD800) << 10) + int(m[2], 16) - 0xDC00).encode()
    low = chr(int(m[2], 16)).encode("utf-8", "surrogatepass") if m[2] else b""
    return chr(high).encode("utf-8", "surrogatepass") + low
