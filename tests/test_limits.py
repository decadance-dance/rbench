"""Limit detection from the trial's session files (spec run-ledger, Limit detection and stop;
design D8), on the records the pinned CLIs wrote in spike #23 (tests/fixtures/limits/)."""

import json
import shutil
from pathlib import Path

import pytest

from rbench.trial.limits import CLAUDE_USAGE_PREFIXES, RATE, USAGE, Limit, detect_limits

FIXTURES = Path(__file__).parent / "fixtures" / "limits"
CLAUDE_DIR = "home/node/.claude/projects/-rbench-repo"
CODEX_DIR = "home/node/.codex/sessions/2026/11/21"
ZERO = "00000000-0000-0000-0000-000000000000"  # the redacted session id of every fixture
CLAUDE_HOOK = {"kind": "hook", "session_id": ZERO, "source": "startup"}
CLAUDE_HOOK |= {
    "claude_pid": 101,
    "claude_start": 1,
    "transcript_path": f"/{CLAUDE_DIR}/{ZERO}.jsonl",
}
# The shim log of a trial whose Claude session ZERO and Codex rollout rollout-t1 a shim started.
RECORDS = [
    {"kind": "launch", "launch": 1, "cli": "claude", "argv": []},
    {"kind": "child", "launch": 1, "pid": 101, "start": 1},
    CLAUDE_HOOK,
    {"kind": "launch", "launch": 2, "cli": "codex", "argv": []},
    {"kind": "binding", "launch": 2, "path": f"/{CODEX_DIR}/rollout-t1.jsonl", "meta": None},
]


def place(root: Path, fixture: str, path: str) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / fixture, target)
    return target


def lines(root: Path, path: str, *records: object) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(json.dumps(r) + "\n" for r in records))
    return target


def claude_record(fixture: str = "transcript-usage-limit-five-hour.stub.jsonl") -> dict:
    return json.loads((FIXTURES / "claude" / fixture).read_text())


@pytest.mark.parametrize(
    ("fixture", "kind"),
    [
        ("transcript-usage-limit-five-hour.stub.jsonl", USAGE),
        ("transcript-usage-limit-weekly.stub.jsonl", USAGE),
        # "not your usage limit", and marked transient: the record still ends the turn.
        ("transcript-server-rate-limit.stub.jsonl", RATE),
    ],
)
def test_a_claude_transcript_record_gives_the_limit(tmp_path, fixture, kind):
    place(tmp_path, f"claude/{fixture}", f"{CLAUDE_DIR}/{ZERO}.jsonl")

    assert detect_limits(RECORDS, tmp_path) == [Limit("claude", kind)]


def test_a_claude_subagent_transcript_counts_too(tmp_path):
    place(
        tmp_path,
        "claude/transcript-usage-limit-weekly.stub.jsonl",
        f"{CLAUDE_DIR}/{ZERO}/subagents/agent-1.jsonl",
    )

    assert detect_limits(RECORDS, tmp_path) == [Limit("claude", USAGE)]


@pytest.mark.parametrize(
    ("fixture", "kind"),
    [
        ("rollout-usage-limit-plus.stub.jsonl", USAGE),
        ("rollout-usage-limit-team.stub.jsonl", USAGE),
        ("rollout-usage-limit-no-plan.stub.jsonl", USAGE),
        ("rollout-retry-limit-429.stub.jsonl", RATE),
    ],
)
def test_a_codex_rollout_turn_end_gives_the_limit(tmp_path, fixture, kind):
    place(tmp_path, f"codex/{fixture}", f"{CODEX_DIR}/rollout-t1.jsonl")

    assert detect_limits(RECORDS, tmp_path) == [Limit("codex", kind)]


def test_sessions_without_a_limit_give_none(tmp_path):
    place(tmp_path, "codex/rollout-token-count.live.jsonl", f"{CODEX_DIR}/rollout-t1.jsonl")
    ordinary = claude_record()
    for key in ("apiError", "apiErrorStatus", "error", "isApiErrorMessage", "quotaLimits"):
        ordinary.pop(key)
    lines(tmp_path, f"{CLAUDE_DIR}/{ZERO}.jsonl", ordinary)
    (tmp_path / "home/node/.claude/notes.jsonl").write_text(json.dumps(claude_record()) + "\n")

    assert detect_limits(RECORDS, tmp_path) == []  # a file outside the session layout is not read


def test_limits_of_both_clis_are_each_given_once(tmp_path):
    projects = "home/node/.claude/projects"
    place(tmp_path, "claude/transcript-usage-limit-weekly.stub.jsonl", f"{projects}/a/{ZERO}.jsonl")
    place(tmp_path, "claude/transcript-server-rate-limit.stub.jsonl", f"{projects}/b/{ZERO}.jsonl")
    place(tmp_path, "claude/transcript-usage-limit-weekly.stub.jsonl", f"{projects}/c/{ZERO}.jsonl")
    hooks = [CLAUDE_HOOK | {"transcript_path": f"/{projects}/{p}/{ZERO}.jsonl"} for p in "abc"]
    place(tmp_path, "codex/rollout-retry-limit-429.stub.jsonl", f"{CODEX_DIR}/rollout-t1.jsonl")

    assert detect_limits([*RECORDS, *hooks], tmp_path) == [
        Limit("claude", RATE),
        Limit("claude", USAGE),
        Limit("codex", RATE),
    ]
    assert str(Limit("claude", USAGE)) == "Claude reported its usage limit"


def test_a_repository_file_shaped_like_a_transcript_decides_nothing(tmp_path):
    # The record of a session the shim log never saw, in the reviewed repository.
    replay = "rbench/repo/testdata/projects/sample/replay.jsonl"
    place(tmp_path, "claude/transcript-usage-limit-five-hour.stub.jsonl", replay)
    place(tmp_path, "codex/rollout-usage-limit-plus.stub.jsonl", "rbench/repo/t/rollout-x.jsonl")
    # Named and filled like the bound session's transcript, but not where its hook said it is.
    lines(tmp_path, f"rbench/repo/testdata/projects/sample/{ZERO}.jsonl", claude_record())

    assert detect_limits(RECORDS, tmp_path) == []
    lines(tmp_path, f"{CLAUDE_DIR}/{ZERO}.jsonl", claude_record())

    assert detect_limits(RECORDS, tmp_path) == [Limit("claude", USAGE)]


@pytest.mark.parametrize(
    "records",
    [
        [],  # no shim log
        RECORDS[:1] + RECORDS[2:],  # the hook's claude process is not a shim's child
        [*RECORDS[:2], CLAUDE_HOOK | {"session_id": "other"}],
        [*RECORDS[:2], {k: v for k, v in CLAUDE_HOOK.items() if k != "transcript_path"}],
    ],
)
def test_only_sessions_the_shim_log_binds_count(tmp_path, records):
    place(tmp_path, "claude/transcript-usage-limit-weekly.stub.jsonl", f"{CLAUDE_DIR}/{ZERO}.jsonl")
    sub = f"{CLAUDE_DIR}/{ZERO}/subagents/agent-1.jsonl"
    place(tmp_path, "claude/transcript-usage-limit-weekly.stub.jsonl", sub)
    place(tmp_path, "codex/rollout-retry-limit-429.stub.jsonl", f"{CODEX_DIR}/rollout-t2.jsonl")

    assert detect_limits(records, tmp_path) == []


def test_a_root_transcript_counts_only_its_own_sessions_records(tmp_path):
    lines(tmp_path, f"{CLAUDE_DIR}/{ZERO}.jsonl", claude_record() | {"sessionId": "other"})

    assert detect_limits(RECORDS, tmp_path) == []


def test_the_claude_usage_prefixes_are_the_clis_own():
    code = json.loads((FIXTURES / "claude" / "messages.code.json").read_text())

    assert list(CLAUDE_USAGE_PREFIXES) == code["usage_limit_prefixes"]


@pytest.mark.parametrize("prefix", CLAUDE_USAGE_PREFIXES)
def test_claude_text_decides_only_without_the_typed_fields(tmp_path, prefix):
    untyped = claude_record()
    for key in ("apiError", "apiErrorStatus", "error"):
        untyped.pop(key)
    untyped["message"]["content"][0]["text"] = f"{prefix} · resets 3:33am (UTC)"
    lines(tmp_path, f"{CLAUDE_DIR}/{ZERO}.jsonl", untyped)

    assert detect_limits(RECORDS, tmp_path) == [Limit("claude", USAGE)]

    typed = untyped | {"error": "unknown", "apiErrorStatus": 500}
    lines(tmp_path, f"{CLAUDE_DIR}/{ZERO}.jsonl", typed)

    assert detect_limits(RECORDS, tmp_path) == []


def test_codex_text_decides_only_without_codex_error_info(tmp_path):
    rollout = [
        json.loads(line)
        for line in (FIXTURES / "codex/rollout-usage-limit-plus.stub.jsonl")
        .read_text()
        .splitlines()
    ]
    del rollout[-1]["payload"]["error"]["codex_error_info"]
    lines(tmp_path, f"{CODEX_DIR}/rollout-t1.jsonl", *rollout)

    assert detect_limits(RECORDS, tmp_path) == [Limit("codex", USAGE)]

    rollout[-1]["payload"]["error"]["codex_error_info"] = None  # how Codex writes "unclassified"
    lines(tmp_path, f"{CODEX_DIR}/rollout-t1.jsonl", *rollout)

    assert detect_limits(RECORDS, tmp_path) == [Limit("codex", USAGE)]

    rollout[-1]["payload"]["error"]["codex_error_info"] = "other"
    lines(tmp_path, f"{CODEX_DIR}/rollout-t1.jsonl", *rollout)

    assert detect_limits(RECORDS, tmp_path) == []


@pytest.mark.parametrize(
    ("fixture", "kind"),
    [
        ("exec-json-usage-limit-plus.stub.jsonl", USAGE),
        ("exec-json-usage-limit-team.stub.jsonl", USAGE),
        ("exec-json-usage-limit-no-plan.stub.jsonl", USAGE),
        ("exec-json-retry-limit-429.stub.jsonl", RATE),
    ],
)
def test_codex_stdout_decides_only_when_no_rollout_holds_a_turn_error(tmp_path, fixture, kind):
    stdout = FIXTURES / "codex" / fixture

    assert detect_limits(RECORDS, tmp_path / "none", [stdout]) == [Limit("codex", kind)]

    other = {"codex_error_info": "other", "message": "something else"}
    end = {"type": "event_msg", "payload": {"type": "task_complete", "error": other}}
    lines(tmp_path, f"{CODEX_DIR}/rollout-t1.jsonl", end)

    assert detect_limits(RECORDS, tmp_path, [stdout]) == []


def test_malformed_lines_and_unexpected_shapes_are_skipped(tmp_path):
    path = lines(
        tmp_path,
        f"{CLAUDE_DIR}/{ZERO}.jsonl",
        [1, 2],
        {"type": "assistant", "error": "rate_limit", "apiErrorStatus": "429"},
        {"type": "assistant", "error": "rate_limit", "apiErrorStatus": True},
        {"type": "assistant", "isApiErrorMessage": True, "message": "You've hit your limit"},
        {"type": "assistant", "isApiErrorMessage": True, "message": {"content": [None, 3]}},
    )
    with path.open("a") as f:
        f.write('{"type": "assistant", "error": \n')
    lines(
        tmp_path,
        f"{CODEX_DIR}/rollout-t1.jsonl",
        {"type": "event_msg", "payload": []},
        {"type": "event_msg", "payload": {"type": "task_complete", "error": "429"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "error": {"codex_error_info": {"response_too_many_failed_attempts": 429}},
            },
        },
    )

    assert detect_limits(RECORDS, tmp_path) == []
