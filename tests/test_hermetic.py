"""The trial's hermetic configuration from the runner's side (design D5), with a fake docker, and
the audit's session binding on synthetic shim logs and transcripts."""

import json
from pathlib import Path

import pytest

from rbench.engines.declaration import Setting
from rbench.ledger.plan import Image
from rbench.trial import (
    TrialError,
    TrialProject,
    codex_config,
    collect,
    hermetic,
    launch_record,
    prepare,
    shim_settings,
    unbound_sessions,
)

AGENT = "rbench-agent@sha256:" + "a" * 64
PROXY = "rbench-proxy@sha256:" + "b" * 64
IMAGE = Image(AGENT, {"codex": "0.171.0", "claude": "2.1.316"})


def project(tmp_path: Path) -> TrialProject:
    return TrialProject.create(tmp_path / "project", "rbench-t1", AGENT, PROXY)


def exec_argv(call: dict) -> list[str]:
    """What a `docker compose ... exec -T` call runs, after the project and file options."""
    argv = call["argv"]
    return argv[argv.index("-T") + 1 :]


def test_shim_settings_hold_the_policy_and_the_flag_roles_only():
    settings = shim_settings({"claude": Setting("claude-opus-5-5", "high")})

    assert settings == {
        "socket": "/run/rbench/collector.sock",
        "claude": {
            "flags": ["--strict-mcp-config", "--disallowedTools=WebSearch,WebFetch"],
            "model": "claude-opus-5-5",
            "effort": "high",
        },
        "codex": {
            "flags": ["--strict-config", "-c", 'web_search="disabled"'],
            "exec_flags": ["--ignore-rules"],
        },
    }
    settings["codex"]["flags"].append("x")  # a copy, not the policy itself
    assert hermetic.POLICY["codex"]["flags"] == ["--strict-config", "-c", 'web_search="disabled"']
    with pytest.raises(ValueError, match="unknown CLI gemini"):
        shim_settings({"gemini": Setting("m", "high")})


def test_codex_config_carries_config_delivered_model_and_effort():
    text = codex_config({"model_reasoning_effort": "medium", "model": 'gpt-"6"'})

    assert text == 'model = "gpt-\\"6\\""\nmodel_reasoning_effort = "medium"\n'
    with pytest.raises(ValueError, match="bad entry"):
        codex_config({"model = 1\nx": "y"})


def test_prepare_starts_the_collector_then_writes_settings_and_homes(fake_bin, tmp_path):
    settings = shim_settings({})

    prepare(project(tmp_path), settings, 'model = "gpt-6.2-astra"\n', {"enabledPlugins": {}})

    calls = fake_bin.calls()
    assert calls[0]["argv"][:4] == ["compose", "--project-name", "rbench-t1", "--file"]
    assert [exec_argv(c) for c in calls] == [
        ["--detach", "--user", "rbench-collector", "agent", "/opt/rbench/lib/collector.py"],
        ["agent", "test", "-S", "/run/rbench/collector.sock"],
        ["--user", "0", "agent", "sh", "-c", hermetic.WRITE, "sh", "/etc/rbench/shim.json", "0444"],
        ["agent", "sh", "-c", hermetic.WRITE, "sh", "/home/node/.codex/config.toml", "0600"],
        ["agent", "sh", "-c", hermetic.WRITE, "sh", "/home/node/.claude/settings.json", "0600"],
    ]
    assert json.loads(calls[2]["stdin"]) == settings
    assert calls[3]["stdin"] == 'model = "gpt-6.2-astra"\n'
    assert json.loads(calls[4]["stdin"]) == {"enabledPlugins": {}}


def test_prepare_fails_when_the_collector_never_listens(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setattr(hermetic, "COLLECTOR_WAIT_SECONDS", 0.2)
    fake_bin.scenario("docker", replies=[{"match": "test -S", "exit": 1}])

    with pytest.raises(TrialError, match="rbench-t1: the collector did not open"):
        prepare(project(tmp_path), shim_settings({}))

    assert not any("/etc/rbench/shim.json" in c["argv"] for c in fake_bin.calls())


def test_prepare_names_the_step_that_failed(fake_bin, tmp_path):
    fake_bin.scenario("docker", replies=[{"match": "shim.json", "exit": 1, "stderr": "denied\n"}])

    with pytest.raises(TrialError, match="writing /etc/rbench/shim.json: exited 1: denied"):
        prepare(project(tmp_path), shim_settings({}))


DIFF = """\
C /home
C /home/node
A /home/node/.claude/projects/-work/0d6e.jsonl
A /home/node/.claude/projects/-work/0d6e/subagents/agent-1.jsonl
A /home/node/.claude/projects/-work/0d6e/tool-results/x.txt
A /home/node/.codex/sessions/2026/11/21/rollout-2026-11-21T10-00-00-t1.jsonl
A /work/rreview/run/codex-home/sessions/2026/11/21/rollout-2026-t2.jsonl
A /work/repo/notes.jsonl
D /home/node/.codex/sessions/2026/11/21/rollout-gone.jsonl
"""


def test_collect_copies_the_log_every_transcript_and_writes_the_record(fake_bin, tmp_path):
    replies = [
        {"match": "ps --quiet agent", "stdout": "abc123\n"},
        {"match": "diff", "stdout": DIFF},
    ]
    fake_bin.scenario("docker", replies=replies)
    dest = tmp_path / "raw"
    dest.mkdir()
    log = [
        {"seq": 1, "kind": "launch", "launch": 1, "cli": "codex", "argv": ["exec", "-"]},
        {"seq": 2, "kind": "child", "launch": 1, "pid": 40, "start": 7},
        {"seq": 3, "kind": "exit", "launch": 1, "status": 0},
    ]
    (dest / "shim.jsonl").write_text("".join(json.dumps(r) + "\n" for r in log))  # as cp leaves it

    found = collect(project(tmp_path), IMAGE, dest)

    assert found == [
        "/home/node/.claude/projects/-work/0d6e.jsonl",
        "/home/node/.claude/projects/-work/0d6e/subagents/agent-1.jsonl",
        "/home/node/.codex/sessions/2026/11/21/rollout-2026-11-21T10-00-00-t1.jsonl",
        "/work/rreview/run/codex-home/sessions/2026/11/21/rollout-2026-t2.jsonl",
    ]
    copies = [c["argv"][-3:] for c in fake_bin.calls() if "cp" in c["argv"]]
    assert copies[0] == ["cp", "agent:/var/log/rbench/shim.jsonl", str(dest / "shim.jsonl")]
    assert copies[1] == ["cp", f"agent:{found[0]}", str(dest / "transcripts" / found[0][1:])]
    assert len(copies) == 5
    record = json.loads((dest / "launches.json").read_text())
    assert record["image"] == AGENT and record["clis"] == {"claude": "2.1.316", "codex": "0.171.0"}
    assert record["launches"][0]["argv"] == ["exec", "-"]
    assert record["launches"][0]["exit"] == 0


# The session binding.


def records(*items: dict) -> list[dict]:
    return [{"seq": n} | item for n, item in enumerate(items, 1)]


def launch(n: int, pid: int) -> list[dict]:
    return [
        {"kind": "launch", "launch": n, "cli": "x", "argv": [str(n)]},
        {"kind": "child", "launch": n, "pid": pid, "start": pid * 10},
    ]


def hook(session: str, source: str, pid: int) -> dict:
    return {
        "kind": "hook",
        "session_id": session,
        "source": source,
        "claude_pid": pid,
        "claude_start": pid * 10,
    }


def binding(n: int, path: str, thread: str, parent: str | None = None) -> dict:
    source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "exec"
    return {"kind": "binding", "launch": n, "path": path, "meta": {"id": thread, "source": source}}


def claude_transcript(root: Path, session: str, sub: str | None = None) -> None:
    base = root / "home/node/.claude/projects/-work"
    file = base / session / "subagents" / f"{sub}.jsonl" if sub else base / f"{session}.jsonl"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text('{"type": "queue-operation"}\n' + json.dumps({"sessionId": session}) + "\n")


def rollout(root: Path, path: str, thread: str | None, parent: str | None = None) -> None:
    file = root / path.lstrip("/")
    file.parent.mkdir(parents=True, exist_ok=True)
    meta = {"id": thread, "source": "exec"}
    if parent:
        meta["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
    first = {"type": "session_meta", "payload": meta} if thread else {"type": "event_msg"}
    file.write_text(json.dumps(first) + "\n")


def test_claude_sessions_with_one_origin_and_resumes_are_bound(tmp_path):
    log = records(
        *launch(1, 40),
        hook("s1", "startup", 40),
        hook("s1", "compact", 40),
        *launch(2, 50),
        hook("s1", "resume", 50),  # rreview's wrap-up
        *launch(3, 60),
        hook("s2", "fork", 60),
    )
    claude_transcript(tmp_path, "s1")
    claude_transcript(tmp_path, "s1", sub="agent-1")
    claude_transcript(tmp_path, "s2")

    assert unbound_sessions(log, tmp_path) == []


def test_claude_session_without_exactly_one_origin_is_unbound(tmp_path):
    log = records(
        *launch(1, 40),
        hook("s1", "resume", 40),  # a resume never stands in for the origin
        *launch(2, 50),
        hook("s2", "startup", 50),
        *launch(3, 60),
        hook("s2", "startup", 60),
        hook("s3", "startup", 99),  # a claude process no shim started
    )
    for session in ("s1", "s2", "s3"):
        claude_transcript(tmp_path, session)
    claude_transcript(tmp_path, "s4", sub="agent-1")

    problems = unbound_sessions(log, tmp_path)

    work = "/home/node/.claude/projects/-work"
    assert problems == [
        "Claude session s3: its claude process (pid 99) was not started by a shim",
        f"Claude session s1 ({work}/s1.jsonl): no origin launch in the shim log",
        f"Claude session s2 ({work}/s2.jsonl): origin launches [2, 3] in the shim log",
        f"Claude session s3 ({work}/s3.jsonl): no origin launch in the shim log",
        f"Claude subagent transcript {work}/s4/subagents/agent-1.jsonl: no transcript of its session",
    ]


def test_a_jsonl_file_of_the_repository_is_not_a_transcript(tmp_path):
    file = tmp_path / "work/repo/projects/data/rows.jsonl"
    file.parent.mkdir(parents=True)
    file.write_text('{"row": 1}\n')

    assert unbound_sessions([], tmp_path) == []


A = "/home/node/.codex/sessions/2026/11/21/rollout-a.jsonl"
B = "/home/node/.codex/sessions/2026/11/21/rollout-b.jsonl"
C = "/work/run/codex-home/sessions/2026/11/21/rollout-c.jsonl"


def test_codex_rollouts_bound_by_origin_resume_and_child_lineage(tmp_path):
    log = records(
        *launch(1, 40),
        binding(1, A, "t1"),
        binding(1, B, "t2", parent="t1"),  # codex-review's child thread, same launch
        *launch(2, 50),
        binding(2, A, "t1"),  # `codex exec resume` reopens the rollout
        *launch(3, 60),
        binding(3, C, "t3"),  # in a home of rreview's own
    )
    rollout(tmp_path, A, "t1")
    rollout(tmp_path, B, "t2", parent="t1")
    rollout(tmp_path, C, "t3")

    assert unbound_sessions(log, tmp_path) == []
    sessions = launch_record(IMAGE, log)["sessions"]
    assert [(s["launch"], s["path"]) for s in sessions] == [(1, A), (1, B), (2, A), (3, C)]


def test_codex_rollouts_that_do_not_bind(tmp_path):
    log = records(*launch(1, 40), binding(1, A, "t1"), *launch(2, 50), binding(2, B, "t2", "t1"))
    rollout(tmp_path, A, "t1")
    rollout(tmp_path, B, "t2", parent="t1")  # its parent is a root of another launch
    rollout(tmp_path, C, "t3")  # no launch bound it
    rollout(tmp_path, "/tmp/x/rollout-d.jsonl", None)  # no session_meta

    problems = unbound_sessions(log, tmp_path)

    assert problems == [
        f"Codex thread t2 ({B}): its parent thread t1 is not a root thread of launch 2",
        "Codex rollout /tmp/x/rollout-d.jsonl: no session_meta with a thread id",
        f"Codex thread t3 ({C}): no origin launch in the shim log",
    ]


def test_launch_record_joins_hooks_to_launches():
    log = records(
        *launch(1, 40),
        {"kind": "exit", "launch": 1, "status": 2},
        hook("s1", "startup", 40),
        hook("s2", "startup", 99),
        {"kind": "rejected", "reason": "watch: no open launch", "sender_pid": 7},
    )

    record = launch_record(IMAGE, log)

    assert record["launches"] == [
        {
            "launch": 1,
            "cli": "x",
            "argv": ["1"],
            "final_argv": None,
            "added": None,
            "rewrites": None,
            "session_id": None,
            "cwd": None,
            "homes": None,
            "pid": 40,
            "start": 400,
            "exit": 2,
        }
    ]
    assert [(s["session_id"], s["launch"]) for s in record["sessions"]] == [("s1", 1), ("s2", None)]
    assert record["rejected"][0]["reason"] == "watch: no open launch"
