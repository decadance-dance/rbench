"""The requested and applied model and effort of each CLI session, per launch (spec
engine-configurations, Applied model and effort are recorded; design D5, D7), on synthetic shim
logs and transcripts: fake Claude Stop and SubagentStop hook records, and Codex `turn_context`
lines and `exec --json` stdout in the pinned CLIs' shapes. Every id is synthetic.
"""

import json
from datetime import UTC, datetime
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
from rbench.trial.applied import INVALID, UNLISTED, applied_sessions, record_applied

SOL = "gpt-6.2-sol"
A = f"{SESSIONS}/rollout-a.jsonl"
B = f"{SESSIONS}/rollout-b.jsonl"
T0 = 1_800_000_000.0  # the collector's clock in these logs


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "transcripts"


def at(seconds: float) -> str:
    """A transcript timestamp `seconds` after T0, in the CLIs' ISO form."""
    return datetime.fromtimestamp(T0 + seconds, UTC).isoformat().replace("+00:00", "Z")


def timed(records: list[dict], n: int, start: float, end: float) -> list[dict]:
    """Launch `n`'s records, its launch and exit with the collector's times."""
    for r in records:
        if r.get("launch") == n and r["kind"] in ("launch", "exit"):
            r["time"] = T0 + (start if r["kind"] == "launch" else end)
    return records


def effort(session: str, level: str | None, agent: str | None = None, pid: int = 101) -> dict:
    """A record of the image's hook at a Stop (or, with `agent`, a SubagentStop) event, run under
    the `claude` process of launch `pid - 100`."""
    record = {"kind": "effort", "event": "SubagentStop" if agent else "Stop"}
    record |= {"session_id": session, "claude_pid": pid, "claude_start": pid - 100}
    return record | ({"agent_id": agent} if agent else {}) | ({"effort": level} if level else {})


def claude(n: int = 1, model: str = OPUS, level: str = "high", source: str = "startup") -> list:
    """A launch the shim gave `--model` and `--effort`, as it records the CLI's argv."""
    records = launch(n, "claude", "--model", model, "--effort", level, "-p", "review")
    return [*records[:2], hook(n, "s1", source), *records[2:]]


def turn(turn: str, model: str, level: str | None = None, when: float | None = None) -> dict:
    payload = {"turn_id": turn, "model": model} | ({"effort": level} if level else {})
    line = {"type": "turn_context", "payload": payload}
    return line | ({"timestamp": at(when)} if when is not None else {})


def runs(applied) -> list[list[tuple]]:
    """Per session, per launch: (launch, requested model, effort, applied models, efforts)."""
    return [
        [
            (e["launch"], *e["requested"].values(), e["applied"]["models"], e["applied"]["efforts"])
            for e in s["launches"]
        ]
        for s in applied.sessions
    ]


def test_claude_session_and_subagent_record_what_applied(root):
    write(root, f"{PROJECT}/s1.jsonl", [message("s1", "m1", OPUS, 1, 1)])
    write(root, f"{PROJECT}/s1/subagents/agent-x7.jsonl", [message("s1", "m2", OPUS, 1, 1)])
    records = log(*claude(), effort("s1", "high"), effort("s1", "high", agent="x7"))

    applied = applied_sessions(records, root)

    assert [(s["cli"], s["session"], s["launch"]) for s in applied.sessions] == [
        ("claude", "s1/subagents/x7", 1),
        ("claude", "s1", 1),
    ]
    assert runs(applied) == [[(1, OPUS, "high", [OPUS], ["high"])]] * 2
    assert (applied.mismatched, applied.flagged) == ([], [])


def test_a_lower_or_unreported_claude_effort_is_flagged_not_misconfigured(root):
    write(root, f"{PROJECT}/s1.jsonl", [message("s1", "m1", OPUS, 1, 1)])
    write(root, f"{PROJECT}/s1/subagents/agent-x7.jsonl", [message("s1", "m2", OPUS, 1, 1)])
    records = log(*claude(level="xhigh"), effort("s1", "high"), effort("s1", None, agent="x7"))

    applied = applied_sessions(records, root)

    assert applied.mismatched == []
    assert applied.flagged == [
        "claude session 0 of sessions.json, launch 1, effort not reported",
        "claude session 1 of sessions.json, launch 1, ran effort high; it requested xhigh",
    ]
    assert runs(applied)[0][0][4] is None  # not reported: null, as decided for #46


@pytest.mark.parametrize(
    "record",
    [
        effort("s1", "high", pid=999),  # a claude process no launch of the session started
        effort("s1", "high") | {"event": "Other"},
        effort("s1", "high") | {"agent_id": "x7"},  # a Stop names no agent
        effort("s1", "high") | {"effort": "turbo"},
    ],
)
def test_a_claude_effort_counts_only_from_a_valid_record_bound_to_the_launch(root, record):
    write(root, f"{PROJECT}/s1.jsonl", [message("s1", "m1", OPUS, 1, 1)])

    applied = applied_sessions(log(*claude(), record), root)

    assert runs(applied) == [[(1, OPUS, "high", [OPUS], None)]]
    assert applied.flagged == ["claude session 0 of sessions.json, launch 1, effort not reported"]


def test_a_claude_session_on_another_model_is_misconfigured_with_both_models(root):
    write(root, f"{PROJECT}/s1.jsonl", [message("s1", "m1", SONNET, 1, 1)])
    write(root, f"{PROJECT}/s1/subagents/agent-x7.jsonl", [message("s1", "m2", "a b", 1, 1)])
    records = log(*claude(), effort("s1", "high"), effort("s1", "high", agent="x7"))

    applied = applied_sessions(records, root)

    assert applied.mismatched == [
        f"claude session 0 of sessions.json, launch 1, ran {UNLISTED}; it requested {OPUS}",
        f"claude session 1 of sessions.json, launch 1, ran {SONNET}; it requested {OPUS}",
    ]
    assert runs(applied)[0][0][3] == [INVALID]  # not a plain model name


def test_each_claude_resume_is_judged_against_its_own_request(root):
    lines = [
        message("s1", "m1", OPUS, 1, 1) | {"timestamp": at(5)},
        message("s1", "m2", SONNET, 1, 1) | {"timestamp": at(25)},  # the resume asked for Sonnet
        message("s1", "m3", OPUS, 1, 1) | {"timestamp": at(45)},  # the next one fell back
    ]
    write(root, f"{PROJECT}/s1.jsonl", lines)
    records = log(
        *timed(claude(1, OPUS, "high"), 1, 0, 10),
        *timed(claude(2, SONNET, "medium", "resume"), 2, 20, 30),
        *timed(claude(3, SONNET, "medium", "resume"), 3, 40, 50),
        effort("s1", "high", pid=101),
        effort("s1", "medium", pid=102),
        effort("s1", "low", pid=103),
    )

    applied = applied_sessions(records, root)

    assert runs(applied) == [
        [
            (1, OPUS, "high", [OPUS], ["high"]),
            (2, SONNET, "medium", [SONNET], ["medium"]),
            (3, SONNET, "medium", [OPUS], ["low"]),
        ]
    ]
    where = "claude session 0 of sessions.json, launch 3,"
    assert applied.mismatched == [f"{where} ran {OPUS}; it requested {SONNET}"]
    assert applied.flagged == [f"{where} ran effort low; it requested medium"]


def test_a_claude_fork_does_not_count_the_history_it_copied(root):
    parent = message("s0", "m1", OPUS, 1, 1)
    write(root, f"{PROJECT}/s0.jsonl", [parent])
    copied = parent | {"sessionId": "s1"}
    write(root, f"{PROJECT}/s1.jsonl", [copied, message("s1", "m2", SONNET, 1, 1)])
    origin = [r | {"session_id": "s0"} if r["kind"] == "hook" else r for r in claude(1, OPUS)]
    records = log(*origin, *claude(2, SONNET, source="fork"))

    applied = applied_sessions(records, root)

    assert [s["session"] for s in applied.sessions] == ["s0", "s1"]
    assert [r[0][3] for r in runs(applied)] == [[OPUS], [SONNET]]
    assert applied.mismatched == []


def test_synthetic_messages_repository_files_and_links_are_not_sessions(tmp_path, root):
    write(root, f"{PROJECT}/s1.jsonl", [message("s1", "m1", "<synthetic>", 0, 0)])
    write(root, f"{PROJECT}/notes.jsonl", [{"type": "assistant", "message": {"model": "x"}}])
    write(tmp_path, "outside.jsonl", [message("s2", "m2", SONNET, 1, 1)])
    (root / PROJECT.lstrip("/") / "s2.jsonl").symlink_to(tmp_path / "outside.jsonl")

    applied = applied_sessions(log(*claude()), root)

    assert [s["session"] for s in applied.sessions] == ["s1"]
    assert runs(applied) == [[(1, OPUS, "high", [], None)]]
    assert applied.mismatched == []
    assert applied.flagged == [
        "claude session 0 of sessions.json, launch 1, model not reported",
        "claude session 0 of sessions.json, launch 1, effort not reported",
    ]


def codex(n: int, *argv: str, path: str = A, thread: str = "t1") -> list[dict]:
    return [*launch(n, "codex", *argv), binding(n, path, thread)]


def test_codex_requested_by_flag_or_config_and_applied_from_turn_context(root):
    write(root, A, [meta("t1"), turn("u1", GPT, "high"), turn("u2", GPT, "high")])
    config = f'model = "{SOL}"\nmodel_reasoning_effort = "high"\n'

    by_flag = applied_sessions(log(*codex(1, "-c", f'model="{GPT}"', "exec", "-")), root, config)
    by_config = applied_sessions(log(*codex(1, "exec", "-")), root, f'model = "{GPT}"\n')

    assert by_flag.sessions[0]["session"] == "t1"
    assert runs(by_flag) == [[(1, GPT, "high", [GPT], ["high"])]]
    assert (by_flag.mismatched, by_flag.flagged) == ([], [])
    assert runs(by_config) == [[(1, GPT, None, [GPT], ["high"])]]
    assert (by_config.mismatched, by_config.flagged) == ([], [])


@pytest.mark.parametrize(
    "argv",
    [
        (f"-m{GPT}", "-cmodel_reasoning_effort=high", "exec", "-"),
        (f"--config=model={GPT}", "--config=model_reasoning_effort='high'", "exec", "-"),
        ("--model", GPT, "exec", "-c", 'model_reasoning_effort="high"', "-"),
        ("exec", "-m", GPT, "-c", "model_reasoning_effort=high", "--", "-m", SOL),
    ],
)
def test_codex_requests_are_read_in_every_option_form_the_cli_takes(root, argv):
    write(root, A, [meta("t1"), turn("u1", GPT, "high")])

    applied = applied_sessions(log(*codex(1, *argv)), root)

    assert runs(applied) == [[(1, GPT, "high", [GPT], ["high"])]]
    assert (applied.mismatched, applied.flagged) == ([], [])


@pytest.mark.parametrize(
    "argv",
    [
        ("-c", "model_reasoning_effort", "exec", "-"),  # Codex refuses an override without `=`
        ("-c", "model=3", "exec", "-"),  # not a name
        ("-m", GPT, "-c", f'model="{SOL}"', "exec", "-"),  # two models
        ("exec", "-m"),  # an option without its value
    ],
)
def test_an_invalid_codex_request_is_misconfigured_not_an_exception(root, argv):
    write(root, A, [meta("t1"), turn("u1", GPT, "high")])

    applied = applied_sessions(log(*codex(1, *argv)), root)

    assert applied.mismatched == [
        "codex session 0 of sessions.json, launch 1, sets an invalid model or effort on its argv"
    ]


def test_a_codex_thread_on_another_or_a_default_model_is_misconfigured(root):
    write(root, A, [meta("t1"), turn("u1", SOL, "medium")])
    config = 'model_reasoning_effort = "high"\n'

    other = applied_sessions(log(*codex(1, "-m", GPT, "exec", "-")), root, config)
    default = applied_sessions(log(*codex(1, "exec", "-")), root)

    where = "codex session 0 of sessions.json, launch 1,"
    assert other.mismatched == [f"{where} ran {SOL}; it requested {GPT}"]
    assert other.flagged == [f"{where} ran effort medium; it requested high"]
    assert default.mismatched == [f"{where} ran {SOL}; it requested no model"]


def test_the_codex_review_thread_is_judged_against_review_model(root):
    review = {"type": "session_meta", "payload": {"id": "t2", "parent_thread_id": "t1"}}
    review["payload"]["source"] = {"subagent": "review"}
    write(root, A, [meta("t1"), turn("u1", GPT, "high")])
    write(root, B, [review, turn("u2", SOL, "high")])
    argv = ("-m", GPT, "-c", f'review_model="{SOL}"', "-c", 'model_reasoning_effort="high"')
    records = log(*codex(1, *argv, "exec", "review"), binding(1, B, "t2"))

    applied = applied_sessions(records, root)

    assert runs(applied) == [
        [(1, GPT, "high", [GPT], ["high"])],
        [(1, SOL, "high", [SOL], ["high"])],
    ]
    assert applied.mismatched == []


def test_each_codex_resume_is_judged_against_its_own_request(tmp_path, root):
    write(root, A, [meta("t1"), turn("u1", GPT, "high", 5), turn("u2", SOL, "medium", 25)])
    first = codex(1, "-m", GPT, "-c", "model_reasoning_effort=high", "exec", "-")
    resume = codex(2, "-m", SOL, "-c", "model_reasoning_effort=medium", "exec", "resume", "t1")
    records = log(*timed(first, 1, 0, 10), *timed(resume, 2, 20, 30))
    message = f"model rerouted: {SOL} -> {GPT} (x)"
    out = {2: stdout(tmp_path, "t1", reroute=True, message=message)}

    honored = applied_sessions(records, root)
    rerouted = applied_sessions(records, root, None, out)

    assert runs(honored) == [
        [(1, GPT, "high", [GPT], ["high"]), (2, SOL, "medium", [SOL], ["medium"])]
    ]
    assert (honored.mismatched, honored.flagged) == ([], [])
    assert runs(rerouted)[0][1][3] == sorted([GPT, SOL])
    assert rerouted.mismatched == [
        f"codex session 0 of sessions.json, launch 2, ran {GPT}; it requested {SOL}"
    ]


def test_a_line_outside_every_launch_run_is_flagged(root):
    write(root, A, [meta("t1"), turn("u1", GPT, "high", 5), turn("u2", SOL, "high", 15)])
    first = codex(1, "-m", GPT, "-c", "model_reasoning_effort=high", "exec", "-")

    applied = applied_sessions(log(*timed(first, 1, 0, 10)), root)

    assert runs(applied) == [[(1, GPT, "high", [GPT], ["high"])]]
    assert applied.flagged == [
        "codex session 0 of sessions.json: transcript lines that no launch's run holds"
    ]


REROUTED = f"model rerouted: {GPT} -> gpt-other (HighRiskCyberActivity)"


@pytest.mark.parametrize(
    ("message", "item", "models"),
    [
        (REROUTED, "error", [GPT, "gpt-other"]),
        (REROUTED, "other", [GPT]),
        (f"model rerouted: {SOL} -> {GPT} (zz-marker)", "error", [GPT, INVALID]),  # not its own
        ("model rerouted: somewhere", "error", [GPT, INVALID]),
    ],
)
def test_only_a_reroute_of_the_launch_request_is_evidence(tmp_path, root, message, item, models):
    write(root, A, [meta("t1"), turn("u1", GPT, "high")])
    out = {1: stdout(tmp_path, "t1", reroute=True, message=message, item=item)}

    applied = applied_sessions(log(*codex(1, "-m", GPT, "exec", "--json", "-")), root, None, out)

    assert runs(applied)[0][0][3] == sorted(models)
    assert bool(applied.mismatched) == (models != [GPT])
    assert "zz-marker" not in json.dumps([applied.mismatched, applied.flagged])


def test_a_codex_fork_does_not_count_the_turns_it_copied(root):
    write(root, A, [meta("t1"), turn("u1", GPT, "high")])
    write(root, B, [meta("t2", forked="t1"), turn("u1", GPT, "high"), turn("u2", SOL, "high")])
    records = log(
        *codex(1, "-m", GPT, "exec", "-"),
        *codex(2, "-m", SOL, "fork", "t1", path=B, thread="t2"),
    )

    applied = applied_sessions(records, root, 'model_reasoning_effort = "high"\n')

    assert [r[0][3] for r in runs(applied)] == [[GPT], [SOL]]
    assert applied.mismatched == []


def test_the_record_is_written_whole(tmp_path, root):
    write(root, A, [meta("t1"), turn("u1", GPT)])
    applied = applied_sessions(log(*codex(1, "-m", GPT, "exec", "-")), root)

    record_applied(tmp_path, applied)

    assert json.loads((tmp_path / "sessions.json").read_text()) == {
        "sessions": [
            {
                "cli": "codex",
                "session": "t1",
                "launch": 1,
                "launches": [
                    {
                        "launch": 1,
                        "requested": {"model": GPT, "effort": None},
                        "applied": {"models": [GPT], "efforts": None},
                    }
                ],
            }
        ],
        "misconfigured": [],
        "flagged": ["codex session 0 of sessions.json, launch 1, effort not reported"],
    }


def test_the_shim_log_reads_effort_records_strictly(tmp_path):
    file = tmp_path / "shim.jsonl"
    file.write_text(json.dumps({"seq": 1, "time": T0} | effort("s1", "high", agent="a1")) + "\n")
    assert read_shim_log(file)[0]["effort"] == "high"

    for bad in (
        effort("s1", "high") | {"effort": 3},
        effort("s1", "turbo"),
        effort("s1", "high") | {"event": "Other"},
        effort("s1", "high") | {"agent_id": "a1"},
        effort("s1", "high") | {"time": "x"},
    ):
        file.write_text(json.dumps({"seq": 1} | bad) + "\n")
        with pytest.raises(SessionError, match="effort record"):
            read_shim_log(file)
