"""The contamination audit (spec trial-isolation, Contamination audit). Canaries are synthetic."""

import json
import os
import shutil
import tracemalloc
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote

import pytest

from rbench.ledger import AUDIT_FILE, LedgerError, read_status, set_state
from rbench.tasks.loader import HIDDEN_ID_PREFIX, PUBLIC_CANARY_PREFIX, Task
from rbench.trial import (
    ALLOWLIST,
    RepositoryState,
    audit_trial,
    read_allowlist,
    record_audit,
)
from rbench.trial import audit as audit_module
from rbench.trial.audit import MAX_LISTED

OWN = Task(
    id="t-own",
    set="ours",
    repo="acme/widget",
    pr=7,
    base="a" * 40,
    head="b" * 40,
    archive_sha256="c" * 64,
    language="python",
    title="Make x three",
    description="Sets x to 3.",
    canary=PUBLIC_CANARY_PREFIX + "1" * 32,
    ground_truth=(),
    path=Path("/tasks/ours/t-own"),
)
OTHER = replace(OWN, id="t-other", canary=PUBLIC_CANARY_PREFIX + "2" * 32)
HIDDEN_ID = f"{HIDDEN_ID_PREFIX}synthetic"
HIDDEN = replace(OWN, id=HIDDEN_ID, set="hidden", canary="synthetic-hidden-é-" + "3" * 32)
TASKS = [OWN, OTHER, HIDDEN]
STATE = RepositoryState("a" * 40, "b" * 40, objects=10)
ALLOWED = "1700000000.000 172.18.0.3 TCP_TUNNEL/200 CONNECT api.anthropic.com:443 160.79.104.10"
DENIED = "1700000001.000 172.18.0.3 TCP_DENIED/403 CONNECT github.com:443 -"
SESSION = "11111111-2222-3333-4444-555555555555"


def count_output(loose: int, packed: int, garbage: int = 0) -> str:
    lines = [f"count: {loose}", "size: 4", f"in-pack: {packed}", "packs: 1", "size-pack: 9"]
    return "\n".join([*lines, "prune-packable: 0", f"garbage: {garbage}", "size-garbage: 0"]) + "\n"


class Trial:
    """A trial directory after its engine: raw output, a proxy log, a shim log, transcripts."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "trial"
        self.raw = self.dir / "raw"
        self.raw.mkdir(parents=True)
        set_state(self.dir, "running")
        self.proxy_log = self.raw / "proxy.log"
        self.proxy_log.write_text(ALLOWED + "\n")
        self.shim_log = self.raw / "shim.jsonl"
        self.transcripts = self.raw / "transcripts"
        (self.raw / "engine.out").write_text("Looks fine.\n")
        self.objects = count_output(4, 6)
        self.log_session(SESSION)

    def log_session(self, session: str) -> None:
        """A shim launch whose claude process started `session`, and its transcript."""
        records = [
            {"seq": 1, "kind": "launch", "launch": 1, "cli": "claude", "argv": ["-p"]},
            {"seq": 2, "kind": "child", "launch": 1, "pid": 40, "start": 400},
            {"seq": 3, "kind": "hook", "session_id": session, "source": "startup"}
            | {"claude_pid": 40, "claude_start": 400},
            {"seq": 4, "kind": "exit", "launch": 1, "status": 0},
        ]
        self.shim_log.write_text("".join(json.dumps(r) + "\n" for r in records))
        self.transcript(session)

    def transcript(self, session: str) -> None:
        file = self.transcripts / f"home/node/.claude/projects/-rbench-repo/{session}.jsonl"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({"sessionId": session, "type": "user"}) + "\n")

    def audit(self, tasks=TASKS):
        def run(argv: list[str]) -> str:
            assert argv == ["git", "-C", "/rbench/repo", "count-objects", "-v"]
            return self.objects

        return audit_trial(
            self.dir,
            tasks,
            proxy_log=self.proxy_log,
            shim_log=self.shim_log,
            transcripts=self.transcripts,
            repository=STATE,
            run=run,
        )


@pytest.fixture
def trial(tmp_path: Path) -> Trial:
    return Trial(tmp_path)


def test_a_clean_trial_is_not_contaminated(trial):
    audit = trial.audit()

    assert audit.reasons == () and audit.refused == ()
    record_audit(trial.dir, audit)
    assert read_status(trial.dir).state == "running"
    assert json.loads((trial.dir / AUDIT_FILE).read_text()) == {
        "contaminated": [],
        "refused": [],
        "refused_omitted": 0,
    }


def test_the_allowlist_is_the_proxy_image_one():
    assert read_allowlist(ALLOWLIST) == {"api.anthropic.com", "chatgpt.com", "auth.openai.com"}


@pytest.mark.parametrize(
    "line",
    [
        "1700000002.000 172.18.0.3 TCP_TUNNEL/200 CONNECT example.com:443 93.184.215.14",
        "1700000002.000 172.18.0.3 TCP_MISS/200 GET http://example.com/ 93.184.215.14",
        # A listed host on another port or by plain HTTP is outside the allowlist too.
        "1700000002.000 172.18.0.3 TCP_TUNNEL/200 CONNECT api.anthropic.com:22 160.79.104.10",
        "1700000002.000 172.18.0.3 TCP_MISS/200 GET http://api.anthropic.com/ 160.79.104.10",
        # No upstream address, yet no error answer: fails closed.
        "1700000002.000 172.18.0.3 TCP_TUNNEL/200 CONNECT example.com:443 -",
        # An error answer after squid connected upstream is still a connection.
        "1700000002.000 172.18.0.3 TCP_MISS/503 CONNECT example.com:443 93.184.215.14",
    ],
)
def test_a_successful_connection_outside_the_allowlist_contaminates(trial, line):
    trial.proxy_log.write_text(f"{ALLOWED}\n{line}\n")

    audit = trial.audit()

    _, _, result, method, target, _ = line.split()
    target = target.removesuffix("/")  # the destination only
    reason = f"the proxy log shows a connection outside the allowlist: {method} {target} "
    assert audit.reasons == (reason + f"({result}, line 2)",)
    record_audit(trial.dir, audit)
    status = read_status(trial.dir)
    assert (status.state, status.reason) == ("contaminated", audit.reasons[0])


@pytest.mark.parametrize(
    "line",
    [
        b"CONNECT evil example",
        b"1700000002.000 172.18.0.3 TCP_DENIED/403bad CONNECT example.com:443 -",
        # Six fields, an allowlisted target, but no valid time, client or result.
        b"yesterday client TCP_TUNNEL CONNECT api.anthropic.com:443 160.79.104.10",
        b"1700000002.000 172.18.0.3 TCP_TUNNEL/200 CONNECT api.anthropic.com:443 upstream",
        b"1700000002.000 172.18.0.3 TCP_DENIED/403 GET http://example.com/\xff -",
        b"1700000002.000\t172.18.0.3 TCP_DENIED/403 GET http://example.com/ -",
        b"1700000002.000 172.18.0.3 TCP_DENIED/403 GET http://example.com/" + b"a" * (64 << 10),
    ],
)
def test_a_malformed_proxy_record_fails_closed(trial, line):
    trial.proxy_log.write_bytes(ALLOWED.encode() + b"\n" + line + b"\n" + DENIED.encode() + b"\n")

    audit = trial.audit()
    record_audit(trial.dir, audit)

    assert audit.reasons == ("proxy log line 2 is not a valid record, so it may hide a connection",)
    assert [r["target"] for r in audit.refused] == ["github.com:443"]
    assert read_status(trial.dir).state == "contaminated"


def test_a_missing_proxy_log_fails_closed(trial):
    trial.proxy_log.unlink()
    assert trial.audit().reasons == ("the proxy log is missing",)


def test_a_refused_attempt_does_not_contaminate_and_is_listed_for_run_health(trial):
    plain = "1700000003.000 172.18.0.3 TCP_DENIED/403 GET http://example.com/ -"
    garbage = "1700000004.000 172.18.0.3 NONE_NONE/400 NONE error:invalid-request -"
    trial.proxy_log.write_text(f"{ALLOWED}\n{DENIED}\n{plain}\n{garbage}\n")

    audit = trial.audit()
    record_audit(trial.dir, audit)

    refused = [
        {"method": "CONNECT", "target": "github.com:443", "result": "TCP_DENIED/403", "count": 1},
        {"method": "GET", "target": "http://example.com", "result": "TCP_DENIED/403", "count": 1},
        {
            "method": "NONE",
            "target": "error:",
            "result": "NONE_NONE/400",
            "count": 1,
        },
    ]
    assert not audit.contaminated
    assert read_status(trial.dir).state == "running"
    assert json.loads((trial.dir / AUDIT_FILE).read_text()) == {
        "contaminated": [],
        "refused": refused,
        "refused_omitted": 0,
    }


def test_refused_attempts_are_counted_and_bounded(trial):
    lines = [DENIED] * 3 + [
        f"1700000005.000 172.18.0.3 TCP_DENIED/403 CONNECT h{n}.example:443 -"
        for n in range(MAX_LISTED + 5)
    ]
    trial.proxy_log.write_text("\n".join(lines) + "\n")

    audit = trial.audit()

    assert not audit.contaminated
    assert len(audit.refused) == MAX_LISTED and audit.refused_omitted == 6
    assert audit.refused[0] == {
        "method": "CONNECT",
        "target": "github.com:443",
        "result": "TCP_DENIED/403",
        "count": 3,
    }


def test_any_tasks_canary_in_a_finding_contaminates(trial):
    findings = [{"file": "a.py", "start_line": 1, "end_line": 1, "message": OTHER.canary}]
    (trial.dir / "findings.json").write_text(json.dumps(findings))

    audit = trial.audit()

    assert audit.reasons == ("the canary of task t-other appears in findings.json",)
    record_audit(trial.dir, audit)
    status = read_status(trial.dir)
    assert (status.state, status.reason) == ("contaminated", audit.reasons[0])


def test_the_scan_covers_hidden_canaries_transcripts_and_json_escapes(trial):
    # The hidden canary as a JSON transcript stores it (ensure_ascii), the trial's own raw.
    trial.transcript(SESSION)
    file = trial.transcripts / f"home/node/.claude/projects/-rbench-repo/{SESSION}.jsonl"
    file.write_text(json.dumps({"sessionId": SESSION, "text": HIDDEN.canary}) + "\n")
    (trial.raw / "pr-comments").mkdir()
    (trial.raw / "pr-comments" / "comment-1.md").write_text(f"See {OWN.canary}.\n")

    audit = trial.audit()

    path = f"raw/transcripts/home/node/.claude/projects/-rbench-repo/{SESSION}.jsonl"
    assert audit.reasons == (
        "the canary of task t-own appears in raw/pr-comments/comment-1.md",
        f"the canary of a task in set 'hidden' appears in {path}",
    )


def test_a_canary_spelled_with_json_escapes_is_found(trial):
    # Valid JSON that decodes to the canary, though its bytes never spell it.
    escaped = f"\\u{ord(OTHER.canary[0]):04x}" + OTHER.canary[1:]
    (trial.dir / "findings.json").write_text('[{"message": "see ' + escaped + '"}]')
    (trial.raw / "notes.txt").write_text('not JSON "' + escaped + '" {')
    assert json.loads((trial.dir / "findings.json").read_text())[0]["message"].endswith(
        OTHER.canary
    )

    assert trial.audit().reasons == (
        "the canary of task t-other appears in findings.json",
        "the canary of task t-other appears in raw/notes.txt",
    )


def test_a_canary_scan_keeps_distinct_canaries_only(trial):
    (trial.raw / "big.bin").write_bytes((OTHER.canary.encode() + b"\0") * 250_000)
    tracemalloc.start()
    try:
        audit = trial.audit()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert audit.reasons == ("the canary of task t-other appears in raw/big.bin",)
    assert peak < 2 << 20


def test_hidden_values_never_reach_the_audit_or_status_file(trial):
    hidden = replace(HIDDEN, id=f"{HIDDEN_ID_PREFIX}ascii", canary="synthetic-hidden-" + "4" * 32)
    # A file named after the hidden task, holding its canary; a refused URL and a connection
    # carrying both; and a transcript session named after it, with no origin launch.
    (trial.raw / f"{hidden.id}-{hidden.canary}.md").write_text(hidden.canary)
    url = f"http://example.com/{hidden.id}/{hidden.canary}"
    trial.proxy_log.write_text(
        f"{ALLOWED}\n1700000003.000 172.18.0.3 TCP_DENIED/403 GET {url} -\n"
        f"1700000004.000 172.18.0.3 TCP_MISS/200 GET {url}/x 93.184.215.14\n"
    )
    trial.transcript(hidden.id)

    audit = trial.audit(tasks=[OWN, OTHER, hidden])
    record_audit(trial.dir, audit)

    assert len(audit.reasons) == 4 and len(audit.refused) == 1
    for file in (trial.dir / AUDIT_FILE, trial.dir / "status.json"):
        text = file.read_text()
        for value in (hidden.id, hidden.canary, "synthetic-hidden-"):
            assert value not in text, (file.name, value)
        assert "<redacted>" in text


def test_a_symlink_is_never_followed(trial, tmp_path, monkeypatch):
    # A valid, bound transcript outside the trial directory, linked in place of the real one.
    outside = tmp_path / "outside.jsonl"
    file = trial.transcripts / f"home/node/.claude/projects/-rbench-repo/{SESSION}.jsonl"
    file.replace(outside)
    file.symlink_to(outside)
    (trial.raw / "dir-link").symlink_to(tmp_path, target_is_directory=True)
    opened = []
    real_open = Path.open

    def guarded(self, *args, **kwargs):
        opened.append(self.resolve())
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)

    audit = trial.audit()

    assert outside.resolve() not in opened and opened
    link = f"raw/transcripts/home/node/.claude/projects/-rbench-repo/{SESSION}.jsonl"
    assert audit.reasons == (
        "raw/dir-link is not a regular file, so it cannot be audited",
        f"{link} is not a regular file, so it cannot be audited",
    )


def test_the_scan_needs_the_loaded_tasks(trial):
    with pytest.raises(ValueError, match="loaded tasks"):
        trial.audit(tasks=[])


def test_a_changed_object_count_contaminates(trial):
    trial.objects = count_output(1, 10)  # a commit made by the engine

    assert trial.audit().reasons == ("the repository's object count changed from 10 to 11",)


def test_an_unreadable_object_count_fails_closed(trial):
    trial.objects = count_output(4, 6, garbage=1)

    (reason,) = trial.audit().reasons
    assert reason == (
        "the repository's object count cannot be read after the engine: "
        "the trial's repository: alternate or garbage object stores"
    )


def test_a_session_missing_from_the_shim_log_contaminates(trial):
    trial.transcript("99999999-2222-3333-4444-555555555555")  # the real binary, started by path

    audit = trial.audit()

    session = "99999999-2222-3333-4444-555555555555"
    path = f"/home/node/.claude/projects/-rbench-repo/{session}.jsonl"
    assert audit.reasons == (
        f"Claude session {session} ({path}): no origin launch in the shim log",
    )
    record_audit(trial.dir, audit)
    assert read_status(trial.dir).state == "contaminated"


@pytest.mark.parametrize("content", [b"not json\n", b'{"seq": 1, "kind": "x\xff"}\n'])
def test_an_unreadable_shim_log_fails_closed(trial, content):
    trial.shim_log.write_bytes(content)
    (trial.raw / "engine.out").write_text(OTHER.canary)

    audit = trial.audit()
    record_audit(trial.dir, audit)

    assert audit.reasons[0] == "the canary of task t-other appears in raw/engine.out"
    assert audit.reasons[1].startswith("the shim log or a transcript cannot be read: ")
    assert str(trial.dir) not in audit.reasons[1]
    assert read_status(trial.dir).state == "contaminated"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files of any mode")
@pytest.mark.parametrize("where", ["file", "directory"])
def test_an_unreadable_trial_file_fails_closed(trial, where):
    (trial.raw / "engine.out").write_text(OTHER.canary)
    locked = trial.raw / "locked"
    if where == "directory":
        locked.mkdir()
        (locked / "inner.md").write_text("hidden from the scan")
    else:
        locked.write_text("unreadable")
    locked.chmod(0)
    try:
        audit = trial.audit()
        record_audit(trial.dir, audit)
    finally:
        locked.chmod(0o700)

    what = "listed" if where == "directory" else "read"
    assert audit.reasons == (
        "the canary of task t-other appears in raw/engine.out",
        f"raw/locked cannot be {what}: Permission denied",
    )
    assert read_status(trial.dir).state == "contaminated"


def test_a_contaminated_trials_audit_is_final(trial):
    (trial.raw / "engine.out").write_text(OTHER.canary)
    first = trial.audit()
    record_audit(trial.dir, first)
    files = [(trial.dir / name).read_bytes() for name in (AUDIT_FILE, "status.json")]

    record_audit(trial.dir, first)  # the same audit again changes nothing
    (trial.raw / "engine.out").write_text("Looks fine.\n")
    with pytest.raises(LedgerError, match="contaminated; its audit stays as recorded"):
        record_audit(trial.dir, trial.audit())

    assert [(trial.dir / name).read_bytes() for name in (AUDIT_FILE, "status.json")] == files


def test_every_reason_is_recorded(trial):
    trial.proxy_log.write_text(
        ALLOWED + "\n1700000002.000 172.18.0.3 TCP_TUNNEL/200 CONNECT x.example:443 1.2.3.4\n"
    )
    trial.objects = count_output(5, 6)
    (trial.raw / "engine.out").write_text(OTHER.canary)

    audit = trial.audit()
    record_audit(trial.dir, audit)

    assert len(audit.reasons) == 3
    assert read_status(trial.dir).reason == "; ".join(audit.reasons)
    assert json.loads((trial.dir / AUDIT_FILE).read_text())["contaminated"] == list(audit.reasons)


def test_proxy_diagnostics_name_the_destination_only(trial):
    hidden = replace(HIDDEN, id=f"{HIDDEN_ID_PREFIX}ascii", canary="synthetic-hidden-" + "5" * 32)
    # Squid keeps a percent-escaped spelling; a path, query or user part may carry anything.
    encoded = "".join(f"%{b:02X}" for b in f"{hidden.id}/{hidden.canary}".encode())
    host = "".join(f"%{b:02X}" for b in hidden.id.encode()) + ".example"
    url = f"http://user{encoded}@example.com/secret-path/{encoded}?q={encoded}"
    trial.proxy_log.write_text(
        f"{ALLOWED}\n1700000003.000 172.18.0.3 TCP_DENIED/403 GET {url} -\n"
        f"1700000004.000 172.18.0.3 TCP_MISS/200 GET {url}x 93.184.215.14\n"
        f"1700000005.000 172.18.0.3 TCP_TUNNEL/200 CONNECT {host}:443 93.184.215.14\n"
    )

    audit = trial.audit(tasks=[OWN, OTHER, hidden])
    record_audit(trial.dir, audit)

    assert audit.refused == (
        {"method": "GET", "target": "http://example.com", "result": "TCP_DENIED/403", "count": 1},
    )
    outside = "the proxy log shows a connection outside the allowlist:"
    assert audit.reasons == (
        f"{outside} GET http://example.com (TCP_MISS/200, line 3)",
        f"{outside} CONNECT <redacted>.example:443 (TCP_TUNNEL/200, line 4)",
    )
    for file in (trial.dir / AUDIT_FILE, trial.dir / "status.json"):
        text = file.read_text()
        for value in (hidden.id, hidden.canary, "synthetic-hidden-", "secret-path", "user"):
            assert value not in text and value not in unquote(text), (file.name, value)


def guard_opens(monkeypatch) -> list[Path]:
    """Every path the audit opens, by `Path.open` or `os.open`."""
    opened: list[Path] = []
    path_open, os_open = Path.open, os.open

    def guarded_path(self, *args, **kwargs):
        opened.append(Path(os.path.abspath(self)))
        return path_open(self, *args, **kwargs)

    def guarded_os(path, *args, **kwargs):
        opened.append(Path(os.path.abspath(path)))
        return os_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_path)
    monkeypatch.setattr(os, "open", guarded_os)
    return opened


@pytest.mark.parametrize("which", ["proxy log", "shim log", "transcript root"])
def test_a_linked_input_is_never_opened(trial, tmp_path, monkeypatch, which):
    outside = tmp_path / "outside"
    if which == "transcript root":
        trial.transcripts.rename(outside)
        trial.transcripts.symlink_to(outside, target_is_directory=True)
    else:
        path = trial.proxy_log if which == "proxy log" else trial.shim_log
        path.rename(outside)
        path.symlink_to(outside)
    opened = guard_opens(monkeypatch)

    audit = trial.audit()
    record_audit(trial.dir, audit)

    assert f"the {which} is reached through a symlink, so it cannot be audited" in audit.reasons
    assert not [p for p in opened if p.is_relative_to(outside)]
    assert read_status(trial.dir).state == "contaminated"


def test_a_proxy_log_fifo_does_not_block_the_audit(trial):
    trial.proxy_log.unlink()
    os.mkfifo(trial.proxy_log)

    audit = trial.audit()
    record_audit(trial.dir, audit)

    assert audit.reasons == (
        "the proxy log is not a regular file, so it cannot be audited",
        "raw/proxy.log is not a regular file, so it cannot be audited",
    )
    assert read_status(trial.dir).state == "contaminated"


def test_an_input_outside_the_trial_directory_is_refused(trial, tmp_path):
    outside = tmp_path / "proxy.log"
    outside.write_text(ALLOWED + "\n")
    trial.proxy_log = outside

    assert trial.audit().reasons == (
        "the proxy log is not inside the trial directory, so it cannot be audited",
    )


def test_a_missing_transcript_root_holds_no_transcripts(trial):
    shutil.rmtree(trial.transcripts)
    assert trial.audit().reasons == ()


@pytest.mark.parametrize(
    "record",
    [
        {"kind": "child", "launch": 1},  # no pid
        {"kind": "child", "launch": 1, "pid": [40], "start": 400},
        {"kind": "exit", "launch": 7, "status": 0},  # a launch never logged
        {"kind": "hook", "session_id": "s", "source": "startup", "claude_pid": "40"},
        {"kind": "launch", "launch": 2, "cli": "claude", "argv": [1]},
        {"kind": "unknown"},
        {"kind": ["launch"]},
    ],
)
def test_a_malformed_shim_record_fails_closed(trial, record):
    with trial.shim_log.open("a") as f:
        f.write(json.dumps({"seq": 9} | record) + "\n")

    audit = trial.audit()
    record_audit(trial.dir, audit)

    (reason,) = audit.reasons
    assert reason.startswith("the shim log or a transcript cannot be read: raw/shim.jsonl")
    assert read_status(trial.dir).state == "contaminated"


def test_a_lone_surrogate_in_a_transcript_is_recorded_safely(trial):
    rollout = trial.transcripts / "home/node/.codex/sessions/2026/11/21/rollout-x.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type": "session_meta", "payload": {"id": "\\ud800"}}\n')

    audit = trial.audit()
    record_audit(trial.dir, audit)

    path = "/home/node/.codex/sessions/2026/11/21/rollout-x.jsonl"
    assert audit.reasons == (f"Codex thread \\ud800 ({path}): no origin launch in the shim log",)
    status = read_status(trial.dir)
    assert (status.state, status.reason) == ("contaminated", audit.reasons[0])


def test_the_escaped_scan_is_bounded_in_memory(trial):
    # One JSON string of millions of escapes, with an escaped canary at its end.
    escaped = f"\\u{ord(OTHER.canary[0]):04x}" + OTHER.canary[1:]
    (trial.raw / "t.jsonl").write_text('{"m": "' + "\\n" * 2_000_000 + escaped + '"}\n')
    tracemalloc.start()
    try:
        audit = trial.audit()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert audit.reasons == ("the canary of task t-other appears in raw/t.jsonl",)
    assert peak < 4 << 20


def test_an_escape_across_a_window_boundary_is_decoded(trial, monkeypatch):
    monkeypatch.setattr(audit_module, "SCAN_WINDOW", 64)
    escaped = f"\\u{ord(OTHER.canary[0]):04x}" + OTHER.canary[1:]
    file = trial.raw / "t.json"
    for offset in range(90):
        # An escaped backslash takes the escape's backslash: `\\u0072` stays literal text.
        file.write_text('"' + "x" * offset + "\\" + escaped + '"')
        assert trial.audit().reasons == (), offset
        file.write_text('"' + "x" * offset + "\\n" + escaped + '"')
        assert trial.audit().reasons == ("the canary of task t-other appears in raw/t.json",), (
            offset
        )


def test_recomputing_a_verdict_ignores_the_audits_own_record(trial):
    # A refused host spelling a public canary: audit.json keeps it as the refused destination.
    line = f"1700000003.000 172.18.0.3 TCP_DENIED/403 CONNECT {OTHER.canary}.example:443 -"
    trial.proxy_log.write_text(f"{ALLOWED}\n{line}\n")
    first = trial.audit()
    record_audit(trial.dir, first)
    assert OTHER.canary in (trial.dir / AUDIT_FILE).read_text()

    again = trial.audit()

    assert again == first
    record_audit(trial.dir, again)
