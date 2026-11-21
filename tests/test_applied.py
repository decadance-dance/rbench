"""The requested and applied model and effort of each CLI session (spec engine-configurations,
Applied model and effort are recorded; design D5, D7), on synthetic shim logs and transcripts:
fake Claude Stop and SubagentStop hook records, and Codex `turn_context` lines and `exec --json`
stdout in the pinned CLIs' shapes. Every id is synthetic.
"""

import json
from pathlib import Path

import pytest
from test_usage import (
    GPT,
    OPUS,
    PROJECT,
    SESSIONS,
    SONNET,
    binding,
    hook,
    launch,
    log,
    message,
    meta,
    stdout,
    write,
)

from rbench.trial import SessionError, read_shim_log
from rbench.trial.applied import INVALID, applied_sessions, record_applied

SOL = "gpt-6.2-sol"
UNLISTED = "an unlisted value (see sessions.json)"
A = f"{SESSIONS}/rollout-a.jsonl"
B = f"{SESSIONS}/rollout-b.jsonl"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "transcripts"


def effort(session: str, level: str | None, agent: str | None = None) -> dict:
    """A record of the image's hook at a Stop (or, with `agent`, a SubagentStop) event."""
    record = {"kind": "effort", "event": "SubagentStop" if agent else "Stop"}
    record |= {"session_id": session, "claude_pid": 101, "claude_start": 1}
    return record | ({"agent_id": agent} if agent else {}) | ({"effort": level} if level else {})


def claude(model: str = OPUS, level: str = "high") -> list[dict]:
    """A launch the shim gave `--model` and `--effort`, as it records the CLI's argv."""
    records = launch(1, "claude", "--model", model, "--effort", level, "-p", "review")
    return [*records[:2], hook(1, "s1"), *records[2:]]


def turn(turn: str, model: str, level: str | None = None) -> dict:
    payload = {"turn_id": turn, "model": model} | ({"effort": level} if level else {})
    return {"type": "turn_context", "payload": payload}


def test_claude_session_and_subagent_record_what_applied(root):
    write(root, f"{PROJECT}/s1.jsonl", [message("s1", "m1", OPUS, 1, 1)])
    write(root, f"{PROJECT}/s1/subagents/agent-x7.jsonl", [message("s1", "m2", OPUS, 1, 1)])
    records = log(*claude(), effort("s1", "high"), effort("s1", "high", agent="x7"))

    applied = applied_sessions(records, root)

    want = {"model": OPUS, "effort": "high"}
    assert applied.sessions == [
        {
            "cli": "claude",
            "session": "s1/subagents/x7",
            "launch": 1,
            "requested": want,
            "applied": {"models": [OPUS], "efforts": ["high"]},
        },
        {
            "cli": "claude",
            "session": "s1",
            "launch": 1,
            "requested": want,
            "applied": {"models": [OPUS], "efforts": ["high"]},
        },
    ]
    assert (applied.mismatched, applied.flagged) == ([], [])


def test_a_lower_or_unreported_claude_effort_is_flagged_not_misconfigured(root):
    write(root, f"{PROJECT}/s1.jsonl", [message("s1", "m1", OPUS, 1, 1)])
    write(root, f"{PROJECT}/s1/subagents/agent-x7.jsonl", [message("s1", "m2", OPUS, 1, 1)])
    records = log(*claude(level="xhigh"), effort("s1", "high"), effort("s1", None, agent="x7"))

    applied = applied_sessions(records, root)

    assert applied.mismatched == []
    assert applied.flagged == [
        "claude session 0 of sessions.json (launch 1): effort not reported",
        "claude session 1 of sessions.json (launch 1) ran effort high; it requested xhigh",
    ]
    assert applied.sessions[1]["applied"]["efforts"] == ["high"]


def test_a_claude_session_on_another_model_is_misconfigured_with_both_models(root):
    write(root, f"{PROJECT}/s1.jsonl", [message("s1", "m1", SONNET, 1, 1)])
    write(root, f"{PROJECT}/s1/subagents/agent-x7.jsonl", [message("s1", "m2", "a b", 1, 1)])
    records = log(*claude(), effort("s1", "high"), effort("s1", "high", agent="x7"))

    applied = applied_sessions(records, root)

    assert applied.mismatched == [
        f"claude session 0 of sessions.json (launch 1) ran {UNLISTED}; it requested {OPUS}",
        f"claude session 1 of sessions.json (launch 1) ran {SONNET}; it requested {OPUS}",
    ]
    assert applied.sessions[0]["applied"]["models"] == [INVALID]  # not a plain model name
    assert applied.sessions[1]["applied"]["models"] == [SONNET]


def test_synthetic_messages_repository_files_and_links_are_not_sessions(tmp_path, root):
    write(root, f"{PROJECT}/s1.jsonl", [message("s1", "m1", "<synthetic>", 0, 0)])
    write(root, f"{PROJECT}/notes.jsonl", [{"type": "assistant", "message": {"model": "x"}}])
    write(tmp_path, "outside.jsonl", [message("s2", "m2", SONNET, 1, 1)])
    (root / PROJECT.lstrip("/") / "s2.jsonl").symlink_to(tmp_path / "outside.jsonl")

    applied = applied_sessions(log(*claude()), root)

    assert [s["session"] for s in applied.sessions] == ["s1"]
    assert applied.sessions[0]["applied"] == {"models": [], "efforts": []}
    assert applied.mismatched == []
    assert applied.flagged == [
        "claude session 0 of sessions.json (launch 1): model not reported",
        "claude session 0 of sessions.json (launch 1): effort not reported",
    ]


def codex(*argv: str) -> list[dict]:
    return [*launch(1, "codex", *argv, "exec", "-"), binding(1, A, "t1")]


def test_codex_requested_by_flag_or_config_and_applied_from_turn_context(root):
    write(root, A, [meta("t1"), turn("u1", GPT, "high"), turn("u2", GPT, "high")])
    config = f'model = "{SOL}"\nmodel_reasoning_effort = "high"\n'

    by_flag = applied_sessions(log(*codex("-c", f'model="{GPT}"')), root, config)
    by_config = applied_sessions(log(*codex()), root, f'model = "{GPT}"\n')

    assert by_flag.sessions == [
        {
            "cli": "codex",
            "session": "t1",
            "launch": 1,
            "requested": {"model": GPT, "effort": "high"},
            "applied": {"models": [GPT], "efforts": ["high"]},
        }
    ]
    assert (by_flag.mismatched, by_flag.flagged) == ([], [])
    assert by_config.sessions[0]["requested"] == {"model": GPT, "effort": None}
    assert (by_config.mismatched, by_config.flagged) == ([], [])


def test_a_codex_thread_on_another_or_a_default_model_is_misconfigured(root):
    write(root, A, [meta("t1"), turn("u1", SOL, "medium")])

    other = applied_sessions(log(*codex("-m", GPT)), root, 'model_reasoning_effort = "high"\n')
    default = applied_sessions(log(*codex()), root)

    where = "codex session 0 of sessions.json (launch 1)"
    assert other.mismatched == [f"{where} ran {SOL}; it requested {GPT}"]
    assert other.flagged == [f"{where} ran effort medium; it requested high"]
    assert default.mismatched == [f"{where} ran {SOL}; it requested no model"]


def test_a_codex_reroute_on_stdout_is_a_model_mismatch(tmp_path, root):
    write(root, A, [meta("t1"), turn("u1", GPT, "high")])
    write(root, B, [meta("t2"), turn("u2", GPT, "high")])
    records = log(*codex("-m", GPT), binding(1, B, "t2"))
    rerouted = {1: stdout(tmp_path, "t1", reroute=True)}

    applied = applied_sessions(records, root, None, rerouted)

    # The stdout reroutes its own thread only (`exec` filters the others out, D7).
    assert [s["applied"]["models"] for s in applied.sessions] == [[GPT, "gpt-other"], [GPT]]
    assert applied.mismatched == [
        f"codex session 0 of sessions.json (launch 1) ran {UNLISTED}; it requested {GPT}"
    ]


def test_a_codex_resume_takes_its_request_from_the_origin_launch(root):
    write(root, A, [meta("t1"), turn("u1", GPT, "high")])
    records = log(
        *launch(1, "codex", "-m", GPT, "exec", "-"),
        binding(1, A, "t1"),
        *launch(2, "codex", "-m", SOL, "exec", "resume", "t1", "-"),
        binding(2, A, "t1"),
    )

    applied = applied_sessions(records, root)

    assert applied.sessions[0]["launch"] == 1 and applied.mismatched == []


def test_the_record_is_written_whole(tmp_path, root):
    write(root, A, [meta("t1"), turn("u1", GPT)])
    applied = applied_sessions(log(*codex("-m", GPT)), root)

    record_applied(tmp_path, applied)

    assert json.loads((tmp_path / "sessions.json").read_text()) == {
        "sessions": applied.sessions,
        "misconfigured": [],
        "flagged": ["codex session 0 of sessions.json (launch 1): effort not reported"],
    }


def test_the_shim_log_reads_effort_records_strictly(tmp_path):
    file = tmp_path / "shim.jsonl"
    file.write_text(json.dumps({"seq": 1} | effort("s1", "high", agent="a1")) + "\n")
    assert read_shim_log(file)[0]["effort"] == "high"

    file.write_text(json.dumps({"seq": 1} | effort("s1", "high") | {"effort": 3}) + "\n")
    with pytest.raises(SessionError, match="effort record: invalid effort"):
        read_shim_log(file)
