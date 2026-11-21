"""Token usage counted once from transcripts (spec review-scoring, Per-trial metrics and Token usage
counted once; design D7), on synthetic shim logs and transcripts.

Transcript lines are built from the redacted fixtures in `tests/fixtures/limits/`: a Claude
assistant record of the pinned CLI and a Codex `token_count` event. The Codex `session_meta`,
`turn_context` and `token_usage_record` lines carry only the fields usage reads, in the layout
the rollout writes them. Every id is synthetic.
"""

import copy
import json
from pathlib import Path

import pytest

from rbench.ledger.trials import read_results, set_state, store_results
from rbench.trial import Usage, UsageError, count_usage, usage_record

LIMITS = Path(__file__).parent / "fixtures" / "limits"
CLAUDE_LINE = json.loads(
    (LIMITS / "claude" / "transcript-server-rate-limit.stub.jsonl").read_text().splitlines()[0]
)
TOKEN_COUNT = json.loads((LIMITS / "codex" / "rollout-token-count.live.jsonl").read_text())
PROJECT = "/home/node/.claude/projects/-work"
SESSIONS = "/home/node/.codex/sessions/2026/11/21"
OPUS, HAIKU, SONNET, GPT = (
    "claude-opus-5-5",
    "claude-haiku-5-5",
    "claude-sonnet-5-5",
    "gpt-6.2-astra",
)


def write(root: Path, path: str, lines: list[dict]) -> None:
    file = root / path.lstrip("/")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("".join(json.dumps(line) + "\n" for line in lines))


def message(session: str, id: str, model: str, i: int, o: int, read=0, created=0) -> dict:
    """An assistant line of `session` in the shape of the pinned CLI's transcript."""
    line = copy.deepcopy(CLAUDE_LINE)
    for key in ("apiError", "apiErrorStatus", "apiErrorIsTransient", "error", "isApiErrorMessage"):
        line.pop(key, None)
    line["sessionId"] = session
    line["message"] |= {"id": id, "model": model, "content": [{"type": "text", "text": "ok"}]}
    line["message"]["usage"] |= {
        "input_tokens": i,
        "output_tokens": o,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": created,
    }
    return line


def codex_usage(i: int, cached: int, o: int, written: int = 0) -> dict:
    return {
        "input_tokens": i,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": written,
        "output_tokens": o,
        "reasoning_output_tokens": 0,
        "total_tokens": i + o,
    }


def meta(thread: str, forked: str | None = None) -> dict:
    payload = {"id": thread, "source": "exec"} | ({"forked_from_id": forked} if forked else {})
    return {"type": "session_meta", "payload": payload}


def turn(turn: str, model: str) -> dict:
    return {"type": "turn_context", "payload": {"turn_id": turn, "model": model}}


def record(thread: str, turn: str, response: str, usage: dict) -> dict:
    payload = {"thread_id": thread, "turn_id": turn, "response_id": response, "usage": usage}
    return {"type": "token_usage_record", "payload": payload}


def snapshot(usage: dict) -> dict:
    line = copy.deepcopy(TOKEN_COUNT)
    line["payload"]["info"]["total_token_usage"] = usage
    return line


def unverified(thread: str) -> str:
    return (
        f"Codex thread {thread}: no clean stdout of a launch shows it, so the model its usage"
        " ran on is unverified"
    )


def info(value: object) -> dict:
    """A token_count event whose `info` is `value`."""
    line = copy.deepcopy(TOKEN_COUNT)
    line["payload"]["info"] = value
    return line


def log(*items: dict) -> list[dict]:
    return [{"seq": n} | item for n, item in enumerate(items, 1)]


def launch(n: int, cli: str, *argv: str, session: bool = True) -> list[dict]:
    """A shim launch; the shims add flags to session launches only."""
    added = ["--strict-mcp-config"] if cli == "claude" else ["--strict-config"]
    return [
        {"kind": "launch", "launch": n, "cli": cli, "argv": list(argv), "final_argv": list(argv)}
        | {"added": added if session else []},
        {"kind": "child", "launch": n, "pid": 100 + n, "start": n},
        {"kind": "exit", "launch": n, "status": 0},
    ]


def hook(n: int, session: str, source: str = "startup") -> dict:
    return {
        "kind": "hook",
        "session_id": session,
        "source": source,
        "claude_pid": 100 + n,
        "claude_start": n,
    }


def binding(n: int, path: str, thread: str, **extra: str) -> dict:
    return {"kind": "binding", "launch": n, "path": path, "meta": {"id": thread} | extra}


def counts(i: int, o: int, read: int = 0, created: int = 0) -> dict:
    return {"input": i, "output": o, "cache_read": read, "cache_creation": created}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """The collected transcripts, apart from the captured stdout files."""
    return tmp_path / "transcripts"


def stdout(tmp_path: Path, thread: str, *usages: dict, reroute: bool = False) -> Path:
    """A `codex exec --json` stdout in the pinned CLI's shape."""
    file = tmp_path / "stdout" / f"{thread}-{len(list(tmp_path.glob('stdout/*')))}.jsonl"
    file.parent.mkdir(exist_ok=True)
    events = [{"type": "thread.started", "thread_id": thread}, {"type": "turn.started"}]
    if reroute:
        message = f"model rerouted: {GPT} -> gpt-other (HighRiskCyberActivity)"
        events.append({"type": "item.completed", "item": {"id": "item_0", "type": "error"}})
        events[-1]["item"]["message"] = message
    events += [{"type": "turn.completed", "usage": u} for u in usages]
    file.write_text("".join(json.dumps(e) + "\n" for e in events))
    return file


def test_repeated_claude_usage_lines_with_one_message_id_count_once(root):
    # The transcript repeats a message's usage on each content block (text, then tool_use).
    write(
        root,
        f"{PROJECT}/s1.jsonl",
        [
            message("s1", "msg-1", OPUS, 2, 59, 11852, 5140),
            message("s1", "msg-2", OPUS, 4, 68, 17068, 54),
            message("s1", "msg-2", OPUS, 4, 68, 17068, 54),
        ],
    )
    usage = count_usage(log(*launch(1, "claude"), hook(1, "s1")), root)
    assert usage == Usage({OPUS: counts(6, 127, 28920, 5194)}, [])


@pytest.mark.parametrize("order", [1, -1])
def test_a_streamed_message_counts_its_final_usage_once(root, order):
    # An early snapshot of a message under-reports its output (spike #24: 16 against 59).
    lines = [message("s1", "msg-1", OPUS, 2, 16, 11852), message("s1", "msg-1", OPUS, 2, 59, 11852)]
    write(root, f"{PROJECT}/s1.jsonl", lines[::order])
    usage = count_usage(log(*launch(1, "claude"), hook(1, "s1")), root)
    assert usage == Usage({OPUS: counts(2, 59, 11852)}, [])


def test_a_repeat_with_another_model_is_rejected(root):
    write(
        root,
        f"{PROJECT}/s1.jsonl",
        [message("s1", "msg-1", OPUS, 1, 1), message("s1", "msg-1", HAIKU, 1, 1)],
    )
    with pytest.raises(UsageError, match=r"s1\.jsonl:2: msg-1 was counted for model"):
        count_usage([], root)


def test_multi_process_trial_sums_every_session_by_model_with_side_calls(tmp_path, root):
    # Three Claude sessions (one with a subagent and a side call, one a fork that copies its
    # parent's message) and two Codex sessions, one with a review thread.
    write(
        root,
        f"{PROJECT}/s1.jsonl",
        [message("s1", "m1", OPUS, 10, 100, 1000), message("s1", "side-1", HAIKU, 5, 1)],
    )
    write(root, f"{PROJECT}/s1/subagents/agent-a.jsonl", [message("s1", "m2", SONNET, 3, 30)])
    write(root, f"{PROJECT}/s2.jsonl", [message("s2", "m3", OPUS, 1, 10)])
    write(
        root,
        f"{PROJECT}/s3.jsonl",
        [message("s3", "m3", OPUS, 1, 10), message("s3", "m4", OPUS, 2, 20, 0, 7)],
    )
    a, b, c = (f"{SESSIONS}/rollout-{x}.jsonl" for x in "abc")
    write(root, a, [meta("t1"), turn("u1", GPT), record("t1", "u1", "r1", codex_usage(100, 60, 5))])
    review = record("t2", "u2", "r2", codex_usage(50, 0, 4, written=8))
    # Whether the review thread's usage lands in the parent rollout, the child or both is a
    # smoke check (D7): counted once either way.
    parent = [meta("t3"), turn("u3", GPT), record("t3", "u3", "r3", codex_usage(9, 9, 1)), review]
    write(root, b, parent)
    write(root, c, [meta("t2"), turn("u2", GPT), review])
    shim_log = log(
        *launch(1, "claude"),
        hook(1, "s1"),
        *launch(2, "claude"),
        hook(2, "s2"),
        *launch(3, "claude"),
        hook(3, "s3", "fork"),
        *launch(4, "codex", "exec"),
        binding(4, a, "t1"),
        *launch(5, "codex", "exec", "review"),
        binding(5, b, "t3"),
        binding(5, c, "t2"),
        *launch(6, "claude", "--version", session=False),
    )
    out = {4: stdout(tmp_path, "t1"), 5: stdout(tmp_path, "t3")}

    usage = count_usage(shim_log, root, out)

    # `exec --json` shows only its primary thread, so the review thread's model is unverified.
    assert usage.incomplete == [unverified("t2")]
    assert usage.models == {
        GPT: counts(40 + 50 + 0, 5 + 4 + 1, 60 + 0 + 9, 8),
        HAIKU: counts(5, 1),
        OPUS: counts(10 + 1 + 2, 100 + 10 + 20, 1000, 7),
        SONNET: counts(3, 30),
    }
    assert list(usage.models) == sorted(usage.models)


def test_fewer_sessions_with_usage_than_launches_flags_the_figure_incomplete(root):
    write(root, f"{PROJECT}/s1.jsonl", [message("s1", "m1", OPUS, 1, 1)])
    # A session that hit a limit before any request: only the CLI's own message.
    write(root, f"{PROJECT}/s2.jsonl", [message("s2", "m2", "<synthetic>", 0, 0)])
    shim_log = log(
        *launch(1, "claude"),
        hook(1, "s1"),
        *launch(2, "claude"),
        hook(2, "s2"),
        *launch(3, "claude"),  # its session never reached the log
        *launch(4, "codex", "--version", session=False),
    )

    usage = count_usage(shim_log, root)

    assert usage.models == {OPUS: counts(1, 1)}
    assert usage.incomplete == [
        "Launch 2 (claude): no usage of its own for its session s2",
        "Launch 3 (claude): no session in the shim log",
    ]


def test_a_resumed_session_counts_its_transcript_once(root):
    write(
        root,
        f"{PROJECT}/s1.jsonl",
        [message("s1", "m1", OPUS, 4, 63), message("s1", "m2", OPUS, 6, 73)],
    )
    shim_log = log(
        *launch(1, "claude"), hook(1, "s1"), *launch(2, "claude"), hook(2, "s1", "resume")
    )
    assert count_usage(shim_log, root) == Usage({OPUS: counts(10, 136)}, [])


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ('{"type": "assistant", "mess', "not a valid JSON record (Invalid control character"),
        (
            json.dumps(message("s1", "m2", OPUS, 1, 1)).replace(
                '"input_tokens": 1', '"input_tokens": -1, "input_tokens": 1'
            ),
            "not a valid JSON record (a key is repeated",
        ),
    ],
)
def test_a_line_that_cannot_be_read_flags_the_figure_incomplete(root, raw, why):
    write(root, f"{PROJECT}/s1.jsonl", [message("s1", "m1", OPUS, 1, 1)])
    with (root / PROJECT.lstrip("/") / "s1.jsonl").open("a") as f:
        f.write(raw + "\n")
    usage = count_usage([], root)
    assert usage.models == {OPUS: counts(1, 1)}
    [reason] = usage.incomplete
    assert reason.startswith(f"{PROJECT}/s1.jsonl:2: {why}")


def test_a_repository_file_under_projects_is_not_counted(root):
    write(root, "/work/repo/projects/x/notes.jsonl", [message("other", "m1", OPUS, 1, 1)])
    assert count_usage([], root) == Usage({}, [])


def with_usage(line: dict, **usage: object) -> dict:
    line["message"]["usage"] |= usage
    return line


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (message("s1", "m1", OPUS, -1, 1), r"s1\.jsonl:1: message m1: input_tokens: expected a"),
        (message("s1", "m1", OPUS, 1, 1.5), r"s1\.jsonl:1: message m1: output_tokens: expected"),
        (
            with_usage(message("s1", "m1", OPUS, 1, 1), cache_read_input_tokens=0.0),
            r"message m1: cache_read_input_tokens: expected a token count",
        ),
        (
            with_usage(message("s1", "m1", OPUS, 1, 1), cache_creation_input_tokens=True),
            r"message m1: cache_creation_input_tokens: expected a token count",
        ),
        (
            with_usage(message("s1", "m1", OPUS, 1, 1), input_tokens=None),
            r"message m1: input_tokens: expected a token count",
        ),
        (
            with_usage(message("s1", "m1", OPUS, 1, 1), output_tokens=9 * 10**4299),
            r"message m1: output_tokens: expected a token count from 0 below 2\*\*53",
        ),
        (message("s1", "", OPUS, 1, 1), r"s1\.jsonl:1: a message with usage needs an id"),
        (
            {"type": "assistant", "sessionId": "s1", "message": []},
            r"s1\.jsonl:1: an assistant record needs a message object",
        ),
    ],
)
def test_malformed_claude_usage_is_rejected_whole(root, line, message):
    write(root, f"{PROJECT}/s1.jsonl", [line])
    with pytest.raises(UsageError, match=message):
        count_usage([], root)


def test_optional_cache_counters_absent_or_null_are_zero(root):
    line = message("s1", "m1", OPUS, 1, 1)
    del line["message"]["usage"]["cache_read_input_tokens"]
    write(root, f"{PROJECT}/s1.jsonl", [with_usage(line, cache_creation_input_tokens=None)])
    assert count_usage([], root) == Usage({OPUS: counts(1, 1)}, [])


def test_totals_that_reach_2_53_are_rejected(root):
    big = 2**52
    write(
        root,
        f"{PROJECT}/s1.jsonl",
        [message("s1", "m1", OPUS, big, 1), message("s1", "m2", OPUS, big, 1)],
    )
    with pytest.raises(UsageError, match=r"model 'claude-opus-5-5': the input total reaches"):
        count_usage([], root)


A = f"{SESSIONS}/rollout-a.jsonl"
B = f"{SESSIONS}/rollout-b.jsonl"


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (
            record("t1", "u1", "r1", codex_usage(1, 2, 1)),
            r"rollout-a\.jsonl:3: token_usage_record: cached_input_tokens exceeds input_tokens",
        ),
        (
            record("t1", "u1", "r1", codex_usage(1, 0, 1) | {"output_tokens": 1.0}),
            r"rollout-a\.jsonl:3: token_usage_record: output_tokens: expected a token count",
        ),
        (
            {"type": "token_usage_record", "payload": []},
            r"rollout-a\.jsonl:3: token_usage_record: expected a payload object",
        ),
        (record("t1", "u1", "", codex_usage(1, 0, 1)), r"expected thread, turn and response ids"),
    ],
)
def test_malformed_codex_record_is_rejected_whole(root, line, message):
    valid = record("t1", "u1", "r0", codex_usage(5, 0, 1))
    write(root, A, [meta("t1"), turn("u1", GPT), line, valid])
    with pytest.raises(UsageError, match=message):
        count_usage([], root)


def test_a_record_without_its_turn_model_is_not_counted_and_flagged(tmp_path, root):
    write(
        root,
        A,
        [meta("t1"), turn("u1", GPT), record("t1", "u1", "r1", codex_usage(5, 0, 1))]
        + [record("t1", "u2", "r2", codex_usage(7, 0, 1))],
    )
    shim_log = log(*launch(1, "codex", "exec"), binding(1, A, "t1"))
    usage = count_usage(shim_log, root, {1: stdout(tmp_path, "t1")})
    assert usage.models == {GPT: counts(5, 1)}
    assert usage.incomplete == [f"{A}:4: turn u2 has 0 turn_context models"]


def test_a_thread_usage_recorded_in_its_parent_rollout_is_not_counted_again(tmp_path, root):
    # The child's response is in the parent's rollout; the child's own rollout holds only a
    # cumulative token_count of the same work.
    child = record("t2", "u2", "r2", codex_usage(10, 0, 2))
    write(root, A, [meta("t1"), turn("u1", GPT), turn("u2", GPT), child])
    write(root, B, [meta("t2"), turn("u2", GPT), snapshot(codex_usage(10, 0, 2))])
    shim_log = log(*launch(1, "codex", "exec", "review"), binding(1, A, "t1"), binding(1, B, "t2"))

    usage = count_usage(shim_log, root, {1: stdout(tmp_path, "t1")})

    assert usage.models == {GPT: counts(10, 2)}
    assert usage.incomplete == [
        f"Launch 1 (codex): no usage of its own for its session {A}",
        unverified("t2"),
    ]


def test_a_fork_holding_only_its_parent_history_has_no_usage_of_its_own(tmp_path, root):
    history = [turn("u1", GPT), record("t1", "u1", "r1", codex_usage(10, 0, 2))]
    write(root, A, [meta("t1"), *history, snapshot(codex_usage(10, 0, 2))])
    write(root, B, [meta("t9", forked="t1"), *history, snapshot(codex_usage(10, 0, 2))])
    shim_log = log(
        *launch(1, "codex", "exec"),
        binding(1, A, "t1"),
        *launch(2, "codex", "fork", "t1"),
        binding(2, B, "t9"),
    )

    usage = count_usage(shim_log, root, {1: stdout(tmp_path, "t1"), 2: stdout(tmp_path, "t9")})

    assert usage.models == {GPT: counts(10, 2)}
    assert usage.incomplete == [
        (
            f"Codex thread t9 ({B}): no token_usage_record, and its token_count total includes"
            " its parent's usage (a fork)"
        ),
        f"Launch 2 (codex): no usage of its own for its session {B}",
    ]


def test_codex_model_attribution_needs_the_launch_stdout(tmp_path, root):
    write(root, A, [meta("t1"), turn("u1", GPT), record("t1", "u1", "r1", codex_usage(5, 0, 1))])
    shim_log = log(*launch(1, "codex", "exec"), binding(1, A, "t1"))

    rerouted = count_usage(shim_log, root, {1: stdout(tmp_path, "t1", reroute=True)})
    without = count_usage(shim_log, root)

    assert rerouted.models == without.models == {GPT: counts(5, 1)}
    assert rerouted.incomplete == [
        f"Launch 1 (codex): model rerouted: {GPT} -> gpt-other (HighRiskCyberActivity)",
        unverified("t1"),
    ]
    assert without.incomplete == [unverified("t1")]


def broken(tmp_path: Path, kind: str) -> Path:
    """A launch's stdout that cannot be read whole."""
    file = tmp_path / "stdout" / f"{kind}.jsonl"
    if kind == "truncated":  # cut inside the reroute error item
        text = stdout(tmp_path, "t1", reroute=True).read_text()
        file.write_text(text[: text.index("model rerouted") + 10])
    elif kind == "empty":
        file.parent.mkdir(exist_ok=True)
        file.write_text("")
    return file


@pytest.mark.parametrize(
    ("kind", "why"),
    [
        ("missing", "its stdout is unreadable (No such file or directory)"),
        ("empty", "its stdout is empty"),
        ("truncated", "its stdout line 3 is not a valid JSON record (Unterminated string"),
    ],
)
def test_stdout_that_cannot_be_read_whole_leaves_the_model_unverified(tmp_path, root, kind, why):
    write(root, A, [meta("t1"), turn("u1", GPT), record("t1", "u1", "r1", codex_usage(7, 0, 1))])
    shim_log = log(*launch(1, "codex", "exec"), binding(1, A, "t1"))
    usage = count_usage(shim_log, root, {1: broken(tmp_path, kind)})
    assert usage.models == {GPT: counts(7, 1)}
    [problem, thread] = usage.incomplete
    assert problem.startswith(f"Launch 1 (codex): {why}")
    assert problem.endswith(", so the models it shows are unverified")
    assert thread == unverified("t1")


def test_a_child_thread_the_stdout_does_not_show_has_an_unverified_model(tmp_path, root):
    write(root, A, [meta("t1"), turn("u1", GPT), record("t1", "u1", "r1", codex_usage(7, 0, 1))])
    write(root, B, [meta("t2"), turn("u2", GPT), record("t2", "u2", "r2", codex_usage(10, 0, 2))])
    shim_log = log(*launch(1, "codex", "exec"), binding(1, A, "t1"), binding(1, B, "t2"))
    usage = count_usage(shim_log, root, {1: stdout(tmp_path, "t1")})
    assert usage.models == {GPT: counts(17, 3)}
    assert usage.incomplete == [unverified("t2")]


def test_conflicting_turn_models_in_one_rollout_stay_visible(tmp_path, root):
    lines = [meta("t1"), turn("u1", GPT), record("t1", "u1", "r1", codex_usage(7, 0, 1))]
    write(root, A, [*lines, turn("u1", "gpt-other")])
    shim_log = log(*launch(1, "codex", "exec"), binding(1, A, "t1"))
    usage = count_usage(shim_log, root, {1: stdout(tmp_path, "t1")})
    assert usage.models == {}
    assert usage.incomplete == [
        f"{A}:3: turn u1 has 2 turn_context models",
        f"Launch 1 (codex): no usage of its own for its session {A}",
    ]


@pytest.mark.parametrize("model", [["gpt-other"], "", None])
def test_a_turn_context_without_a_model_name_is_rejected(root, model):
    lines = [meta("t1"), turn("u1", GPT), record("t1", "u1", "r1", codex_usage(7, 0, 1))]
    write(root, A, [*lines, {"type": "turn_context", "payload": {"turn_id": "u1", "model": model}}])
    with pytest.raises(UsageError, match=r"rollout-a\.jsonl:4: turn_context: expected a turn id"):
        count_usage([], root)


def test_without_records_the_last_token_count_total_is_the_thread_usage(tmp_path, root):
    rate_only = copy.deepcopy(TOKEN_COUNT)
    rate_only["payload"]["info"] = None
    write(
        root,
        A,
        [meta("t1"), turn("u1", GPT), snapshot(codex_usage(15946, 13952, 5))]
        + [turn("u2", GPT), snapshot(codex_usage(31909, 29696, 11)), rate_only],
    )
    shim_log = log(*launch(1, "codex", "exec"), binding(1, A, "t1"))
    usage = count_usage(shim_log, root, {1: stdout(tmp_path, "t1")})
    assert usage == Usage({GPT: counts(31909 - 29696, 11, 29696)}, [])


@pytest.mark.parametrize(
    ("lines", "why"),
    [
        (
            [meta("t1", forked="t0"), turn("u1", GPT), snapshot(codex_usage(9, 0, 1))],
            "includes its parent's usage (a fork)",
        ),
        (
            [meta("t1"), turn("u1", GPT), snapshot(codex_usage(9, 0, 1))]
            + [snapshot(codex_usage(2, 0, 1))],
            "was reset",
        ),
        (
            [meta("t1"), turn("u1", GPT), turn("u2", "gpt-other"), snapshot(codex_usage(9, 0, 1))],
            "covers 2 models",
        ),
        (
            [meta("t1"), turn("u1", GPT), snapshot(codex_usage(9, 0, 1)), turn("u1", "gpt-other")],
            "covers 2 models",
        ),
        (
            [meta("t1"), turn("u1", GPT), snapshot(codex_usage(7, 0, 1)), info([])],
            f"is malformed ({A}:4: token_count info: expected an object)",
        ),
        (
            [meta("t1"), turn("u1", GPT), snapshot(codex_usage(1, 2, 1))],
            f"is malformed ({A}:3: cached_input_tokens exceeds input_tokens)",
        ),
        (
            [meta("t1"), turn("u1", GPT), snapshot(codex_usage(1, 0, 1) | {"input_tokens": 1.0})],
            f"is malformed ({A}:3: input_tokens: expected a token count from 0 below 2**53)",
        ),
    ],
)
def test_a_token_count_total_that_is_not_the_thread_own_is_flagged(tmp_path, root, lines, why):
    write(root, A, lines)
    shim_log = log(*launch(1, "codex", "exec"), binding(1, A, "t1"))
    usage = count_usage(shim_log, root, {1: stdout(tmp_path, "t1")})
    assert usage.models == {}
    assert usage.incomplete == [
        f"Codex thread t1 ({A}): no token_usage_record, and its token_count total {why}",
        f"Launch 1 (codex): no usage of its own for its session {A}",
    ]


def test_a_missing_rollout_falls_back_to_its_last_launch_stdout(tmp_path, root):
    shim_log = log(
        *launch(1, "codex", "-c", f'model="{GPT}"', "exec", "prompt"),
        binding(1, A, "t1"),
        *launch(2, "codex", "-m", GPT, "exec", "resume", "t1"),
        binding(2, A, "t1"),
    )
    # turn.completed.usage is cumulative over the thread: the last one covers both launches.
    first = stdout(tmp_path, "t1", codex_usage(15946, 13952, 5))
    last = stdout(tmp_path, "t1", codex_usage(15946, 13952, 5), codex_usage(31909, 29696, 11))
    usage = count_usage(shim_log, root, {1: first, 2: last})
    assert usage == Usage({GPT: counts(2213, 11, 29696)}, [])


ZERO = codex_usage(0, 0, 0)


@pytest.mark.parametrize(
    ("argv", "extra", "out", "usage", "why"),
    [
        (("-m", GPT, "exec"), {}, None, ZERO, "no stdout of its last launch 1"),
        (("exec",), {}, "t1", codex_usage(9, 0, 1), "its launches do not name one model"),
        (("-m", GPT, "exec"), {}, "t9", ZERO, "the stdout of launch 1 is not thread t1's"),
        (("-m", GPT, "exec"), {"forked_from_id": "t0"}, "t1", ZERO, "includes its parent's"),
        # Codex 0.171.0 reports all zeros when it never received a token count.
        (("-m", GPT, "exec"), {}, "t1", ZERO, "reports no usage (all counters zero)"),
        (
            ("-m", GPT, "exec"),
            {},
            "t1",
            codex_usage(9, 0, 1) | {"input_tokens": -9},
            "turn.completed: input_tokens: expected a token count",
        ),
    ],
)
def test_a_stdout_total_that_does_not_cover_the_thread_is_flagged(
    tmp_path, root, argv, extra, out, usage, why
):
    shim_log = log(*launch(1, "codex", *argv), binding(1, A, "t1", **extra))
    out = {1: stdout(tmp_path, out, usage)} if out else {}
    result = count_usage(shim_log, root, out)
    assert result.models == {}
    assert result.incomplete[0].startswith(f"Codex rollout {A}: missing or unreadable, and ")
    assert why in result.incomplete[0]
    assert f"Launch 1 (codex): no usage of its own for its session {A}" in result.incomplete


def test_a_reroute_on_stdout_flags_the_stdout_fallback_too(tmp_path, root):
    shim_log = log(*launch(1, "codex", "-m", GPT, "exec"), binding(1, A, "t1"))
    out = {1: stdout(tmp_path, "t1", codex_usage(9, 0, 1), reroute=True)}
    usage = count_usage(shim_log, root, out)
    assert usage.models == {GPT: counts(9, 1)}
    assert usage.incomplete == [
        f"Launch 1 (codex): model rerouted: {GPT} -> gpt-other (HighRiskCyberActivity)",
        unverified("t1"),
    ]


def test_a_stdout_line_with_a_repeated_key_is_not_dropped(tmp_path, root):
    shim_log = log(*launch(1, "codex", "-m", GPT, "exec"), binding(1, A, "t1"))
    out = stdout(tmp_path, "t1", codex_usage(7, 0, 1))
    with out.open("a") as f:
        f.write('{"type": "turn.completed", "usage": {"input_tokens": 9, "input_tokens": 9}}\n')
    usage = count_usage(shim_log, root, {1: out})
    assert usage.models == {}
    why = "line 4 is not a valid JSON record (a key is repeated"
    assert usage.incomplete[0].startswith(
        f"Codex rollout {A}: missing or unreadable, and the stdout of launch 1 {why}"
    )
    assert any(r.startswith(f"Launch 1 (codex): its stdout {why}") for r in usage.incomplete)


def test_usage_record_keeps_normalization_apart_in_the_ledger(tmp_path):
    engine = Usage({OPUS: counts(1, 2)}, ["Launch 3 (claude): no session in the shim log"])
    normalization = Usage({HAIKU: counts(3, 4)}, [])
    record = usage_record(engine, normalization)
    assert record == {
        "engine": {OPUS: counts(1, 2)},
        "normalization": {HAIKU: counts(3, 4)},
        "incomplete": {
            "engine": ["Launch 3 (claude): no session in the shim log"],
            "normalization": [],
        },
    }
    set_state(tmp_path, "running")
    store_results(tmp_path, [], record)
    assert read_results(tmp_path) == ([], record)
    assert usage_record(engine)["normalization"] == {}
