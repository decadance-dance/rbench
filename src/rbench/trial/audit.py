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

Evidence the audit cannot read (a proxy log line, the shim log, the object count) fails closed, as
contamination. Every other proxy line outside the allowlist is a refused attempt: it does not
contaminate, and `audit.json` lists it for run health (spec benchmark-report).
"""

import json
import mmap
import os
import re
import stat
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path

from rbench.atomic import atomic_write
from rbench.ledger.trials import AUDIT_FILE, dump_json, set_state
from rbench.tasks.loader import Task
from rbench.trial.images import IMAGES_DIR
from rbench.trial.repository import (
    REPO_DIR,
    RepositoryError,
    RepositoryState,
    Runner,
    count_objects,
)
from rbench.trial.sessions import SessionError, read_shim_log, unbound_sessions

ALLOWLIST = IMAGES_DIR / "proxy" / "allowlist"


@dataclass(frozen=True)
class Audit:
    """Why a trial is contaminated (empty when it is not), and its refused connection attempts
    in log order, each `{"method", "target", "result"}`."""

    reasons: tuple[str, ...] = ()
    refused: tuple[dict, ...] = ()

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
    hosts = read_allowlist() if allowlist is None else allowlist
    reasons, refused = audit_proxy_log(proxy_log, hosts)
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
    try:
        records = read_shim_log(shim_log)
    except (OSError, SessionError) as e:
        reasons.append(f"the shim log cannot be read: {e}")
    else:
        reasons += unbound_sessions(records, transcripts)
    return Audit(tuple(reasons), tuple(refused))


def record_audit(trial_dir: Path, audit: Audit) -> None:
    """Write `audit.json` for run health; a contaminated trial gets that state and its reasons."""
    record = {"contaminated": list(audit.reasons), "refused": list(audit.refused)}
    atomic_write(Path(trial_dir) / AUDIT_FILE, dump_json(record))
    if audit.contaminated:
        set_state(trial_dir, "contaminated", "; ".join(audit.reasons))


def audit_proxy_log(log: Path, allowlist: Collection[str]) -> tuple[list[str], list[dict]]:
    """Successful connections outside the allowlist, and refused attempts, from the squid log
    (`images/proxy/squid.conf`: time, client, result/status, method, target, upstream address)."""
    try:
        text = log.read_bytes().decode("utf-8", errors="replace")
    except OSError as e:
        return [f"the proxy log cannot be read: {e}"], []
    hosts = {host.lower() for host in allowlist}
    reasons, refused = [], []
    for n, line in enumerate(text.splitlines(), 1):
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 6 or fields[2].count("/") != 1:
            reasons.append(f"proxy log line {n} cannot be read, so it may hide a connection")
            continue
        _, _, result, method, target, upstream = fields
        host, _, port = target.rpartition(":")
        if method == "CONNECT" and port == "443" and host.lower() in hosts:
            continue
        if upstream == "-" and result.split("/")[1][:1] in ("4", "5"):
            refused.append({"method": method, "target": target, "result": result})
        else:
            reasons.append(
                f"the proxy log shows a connection outside the allowlist: {method} {target} "
                f"({result}, line {n})"
            )
    return reasons, refused


def _canaries(trial_dir: Path, tasks: Iterable[Task]) -> list[str]:
    """Every file of the trial directory that holds a task's canary, raw or JSON-escaped."""
    owners: dict[bytes, str] = {}
    for task in tasks:
        # A hidden task is named only by its set (spec review-tasks).
        who = "a task in set 'hidden'" if task.set == "hidden" else f"task {task.id}"
        for form in (task.canary, json.dumps(task.canary)[1:-1]):
            owners.setdefault(form.encode(), who)
    if not owners:
        raise ValueError("the canary scan needs the loaded tasks")
    pattern = re.compile(b"|".join(re.escape(c) for c in sorted(owners, key=len, reverse=True)))
    hits = []
    for path in _files(trial_dir):
        for who in sorted({owners[m] for m in _matches(pattern, path)}):
            hits.append(f"the canary of {who} appears in {path.relative_to(trial_dir).as_posix()}")
    return hits


def _files(root: Path) -> list[Path]:
    """Regular files below `root`; symlinks are not followed, so no host file is read."""
    found = []
    for directory, _, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if stat.S_ISREG(path.lstat().st_mode):
                found.append(path)
    return sorted(found)


def _matches(pattern: re.Pattern[bytes], path: Path) -> set[bytes]:
    with path.open("rb") as f:
        if os.fstat(f.fileno()).st_size == 0:
            return set()
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
            return set(pattern.findall(data))
