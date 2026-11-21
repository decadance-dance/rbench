"""The shims, the watcher, the collector and the SessionStart hook in images/agent/lib (design D5).

Their argv merging, record checks and /proc reading are tested on any platform; the launch flow
through a real collector needs Linux (SO_PEERCRED, /proc) and runs in CI.
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from rbench.trial import read_shim_log, unbound_sessions
from rbench.trial.hermetic import POLICY

LIB = Path(__file__).resolve().parents[1] / "images" / "agent" / "lib"
linux = pytest.mark.skipif(sys.platform != "linux", reason="needs SO_PEERCRED and /proc")


def load(name: str):
    """Import an image script without leaving bytecode in the image's build context."""
    spec = importlib.util.spec_from_file_location(f"agent_{name}", LIB / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    dont_write, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = dont_write
    return module


shim = load("shim")
collector = load("collector")
CLAUDE = POLICY["claude"]
CODEX = POLICY["codex"]
CLAUDE_FLAGS = ["--strict-mcp-config", "--disallowedTools=WebSearch,WebFetch"]
CODEX_FLAGS = ["--strict-config", "-c", 'web_search="disabled"']


def test_claude_fresh_launch_gets_the_policy_model_effort_and_a_session_id():
    settings = CLAUDE | {"model": "claude-sonnet-5-5", "effort": "high"}
    argv = ["-p", "--tools", "Read,Grep,Glob,Bash", "--json-schema", "{}", "review this"]

    final, added, session = shim.claude_launch(argv, settings)

    assert added == [
        *CLAUDE_FLAGS, "--model", "claude-sonnet-5-5", "--effort", "high", "--session-id", session,
    ]  # fmt: skip
    assert final == added + argv  # the engine's argv, its prompt included, follows unchanged
    assert len(session) == 36


@pytest.mark.parametrize(
    "argv",
    [
        ["-p", "--model", "claude-opus-5-5", "--effort=low", "--session-id", "x", "go"],
        ["-p", "--model=claude-opus-5-5", "--effort", "low", "--session-id=x", "go"],
    ],
)
def test_claude_engine_values_win(argv):
    settings = CLAUDE | {"model": "claude-sonnet-5-5", "effort": "high"}

    final, added, session = shim.claude_launch(argv, settings)

    assert (added, session) == (CLAUDE_FLAGS, None)
    assert final == CLAUDE_FLAGS + argv


@pytest.mark.parametrize(
    ("argv", "session_id"),
    [
        (["-p", "--resume", "abc", "wrap up"], False),
        (["-p", "--resume=abc", "wrap up"], False),
        (["-p", "-c", "go on"], False),
        (["-p", "--continue"], False),
        (["-p", "--from-pr", "1"], False),
        (["-p", "--resume", "abc", "--fork-session", "x"], True),
    ],
)
def test_claude_continuation_keeps_its_session_id_unless_it_forks(argv, session_id):
    final, added, session = shim.claude_launch(argv, CLAUDE)

    assert (session is not None) == session_id
    assert added[:2] == CLAUDE_FLAGS and final[len(added) :] == argv


@pytest.mark.parametrize(
    "argv", [["--version"], ["-v"], ["mcp", "list"], ["plugin", "install", "x"], ["-p", "--help"]]
)
def test_claude_non_session_calls_pass_unchanged(argv):
    assert shim.claude_launch(argv, CLAUDE | {"model": "m"}) == (argv, [], None)


def test_codex_exec_gets_global_flags_before_and_ignore_rules_after_exec():
    settings = CODEX | {"model": "gpt-6.2-astra", "effort": "medium"}
    argv = ["exec", "--output-schema", "s.json", "-c", "web_search=disabled", "--json", "-"]

    final, added, rewrites = shim.codex_launch(argv, settings)

    model = ["-c", 'model="gpt-6.2-astra"', "-c", 'model_reasoning_effort="medium"']
    assert final == [*CODEX_FLAGS, *model, "exec", "--ignore-rules", *argv[1:]]
    assert added == [*CODEX_FLAGS, *model, "--ignore-rules"]
    assert rewrites == []


def test_codex_engine_model_and_effort_win():
    settings = CODEX | {"model": "gpt-6.2-astra", "effort": "medium"}
    argv = [
        "e",
        "-m",
        "gpt-6.2-sol",
        "-c",
        'model_reasoning_effort="high"',
        "review",
        "--base",
        "b",
    ]

    final, _, _ = shim.codex_launch(argv, settings)

    assert final == [*CODEX_FLAGS, "e", "--ignore-rules", *argv[1:]]


@pytest.mark.parametrize(
    "own", [["-mgpt-x"], ["--model=gpt-x"], ["-c", "model=gpt-x"], ["--config=model=gpt-x"]]
)
def test_codex_engine_model_in_any_form_wins(own):
    _, added, _ = shim.codex_launch(["exec", *own, "-"], CODEX | {"model": "gpt-6.2-astra"})

    assert added == [*CODEX_FLAGS, "--ignore-rules"]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (["-s", "read-only"], ["-s", "danger-full-access"]),
        (["--sandbox", "read-only"], ["--sandbox", "danger-full-access"]),
        (["--sandbox=read-only"], ["--sandbox=danger-full-access"]),
        (["-s", "danger-full-access"], ["-s", "danger-full-access"]),
    ],
)
def test_codex_read_only_sandbox_is_rewritten_and_logged(given, expected):
    argv = ["exec", *given, "--skip-git-repo-check", "-o", "out.md"]

    final, _, rewrites = shim.codex_launch(argv, CODEX)

    assert final == [*CODEX_FLAGS, "exec", "--ignore-rules", *expected, *argv[-3:]]
    assert len(rewrites) == (given != expected)


@pytest.mark.parametrize(
    "argv",
    [
        ["review", "--base", "base"],  # top-level review: no exec option
        ["resume", "--last"],
        ["-c", "x=1", "fork"],
        [],  # the TUI
        ["fix the bug"],  # the TUI with a prompt
        ["--", "exec"],  # a prompt after --
    ],
)
def test_codex_other_sessions_get_global_flags_only(argv):
    final, added, _ = shim.codex_launch(argv, CODEX)

    assert added == CODEX_FLAGS and final == CODEX_FLAGS + argv


@pytest.mark.parametrize(
    "argv",
    [
        ["--version"],
        ["features", "list"],
        ["debug", "models", "--bundled"],
        ["plugin", "marketplace", "add", "/opt/x"],
        ["-c", "x=1", "login", "status"],
        ["exec", "--help"],
    ],
)
def test_codex_non_session_calls_pass_unchanged(argv):
    assert shim.codex_launch(argv, CODEX | {"model": "m"}) == (argv, [], [])


def fake_proc(root: Path, processes: dict[int, int], fds: dict[int, dict[str, tuple[str, int]]]):
    """A /proc with `processes` (pid -> ppid) and their open files (fd -> (target, flags))."""
    for pid, ppid in processes.items():
        (root / str(pid) / "fd").mkdir(parents=True)
        (root / str(pid) / "fdinfo").mkdir()
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(1000 + pid)]
        (root / str(pid) / "stat").write_text(f"{pid} (some (odd) name) {' '.join(fields)}\n")
        for fd, (target, flags) in fds.get(pid, {}).items():
            (root / str(pid) / "fd" / fd).symlink_to(target)
            (root / str(pid) / "fdinfo" / fd).write_text(
                f"pos:\t0\nflags:\t{flags:o}\nmnt_id:\t1\n"
            )


def test_watcher_reads_rollouts_held_for_append_by_the_cli_and_its_descendants(
    tmp_path, monkeypatch
):
    proc = tmp_path / "proc"
    rollout = "/home/node/.codex/sessions/2026/11/21/rollout-a.jsonl"
    other = "/home/node/.codex/sessions/2026/11/21/rollout-b.jsonl"
    fake_proc(
        proc,
        {10: 1, 11: 10, 12: 11, 20: 1},
        {
            11: {"3": (rollout, 0o2102002)},  # O_RDWR | O_APPEND | O_CLOEXEC
            12: {"4": (other, 0o100000), "5": ("/tmp/notes.jsonl", 0o2002)},  # read only
            20: {"3": ("/x/sessions/y/rollout-c.jsonl", 0o2001)},  # not under the CLI
        },
    )
    monkeypatch.setattr(shim, "PROC", str(proc))

    assert sorted(shim.descendants(10)) == [10, 11, 12]
    assert shim.open_rollouts(11) == [(rollout, 0o2102002)]
    assert shim.open_rollouts(12) == []
    assert shim.start_time(11) == 1011
    assert shim.parent(12) == 11


def test_session_meta_is_read_from_the_first_line(tmp_path):
    rollout = tmp_path / "rollout-a.jsonl"
    assert shim.session_meta(str(rollout)) is None
    rollout.write_text("")
    assert shim.session_meta(str(rollout)) is None
    rollout.write_text('{"type": "session_meta", "payload": {"id": "t1"}}\n{"type": "x"}\n')
    assert shim.session_meta(str(rollout)) == {"id": "t1"}


@pytest.mark.parametrize(
    ("record", "kinds", "error"),
    [
        (
            {"kind": "launch", "cli": "claude", "argv": [], "final_argv": [], "added": []},
            None,
            None,
        ),
        ({"kind": "hook", "session_id": "s", "source": "startup", "claude_pid": None}, None, None),
        ({"kind": "child", "pid": 3}, ("launch",), "expected one of launch"),
        ({"kind": "hook", "session_id": "s"}, None, "missing source"),
        ({"kind": "hook", "session_id": "s", "source": "x", "seq": 1}, None, "unknown field 'seq'"),
        ({"kind": "child", "pid": "3"}, ("child",), "has type str"),
        ({"kind": "child", "pid": True}, ("child",), "has type bool"),
        ({"kind": "launch", "cli": "c", "argv": [1], "final_argv": [], "added": []}, None, "non-"),
        ([], None, "not a JSON object"),
    ],
)
def test_collector_accepts_only_well_formed_records(record, kinds, error):
    kinds = kinds or ("launch", "watch", "hook")
    if error is None:
        assert collector.check(record, kinds) == record
    else:
        with pytest.raises(collector.Rejected, match=error):
            collector.check(record, kinds)


# The launch flow on Linux: a real collector, the shim, a fake CLI and the hook.

FAKE_CLI = """\
import json, os, subprocess, sys, time
if "FAKE_SESSION" in os.environ:  # a Claude session: run the SessionStart hook under this process
    event = {"session_id": os.environ["FAKE_SESSION"], "source": "startup", "cwd": os.getcwd()}
    hook = [sys.executable, "-c", os.environ["FAKE_HOOK"]]
    subprocess.run(hook, input=json.dumps(event), text=True, check=True)
else:  # a Codex session: hold a rollout open for appending
    thread = os.environ["FAKE_THREAD"]
    path = os.path.join(os.environ["FAKE_HOME"], "sessions", "rollout-" + thread + ".jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    meta = {"id": thread, "source": "exec"}
    if "FAKE_PARENT" in os.environ:
        meta["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": os.environ["FAKE_PARENT"]}}}
    os.write(fd, (json.dumps({"type": "session_meta", "payload": meta}) + "\\n").encode())
    time.sleep(0.4)
sys.stdout.write(json.dumps(sys.argv[1:]))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""

# The shim and the hook with the trial's fixed paths pointed at the test's own.
SHIM = """\
import sys
sys.path.insert(0, {lib!r})
import shim
shim.SETTINGS, shim.REAL = {settings!r}, {{"claude": {cli!r}, "codex": {cli!r}}}
sys.argv[0] = {name!r}
shim.main()
"""
HOOK = """\
import sys
sys.path.insert(0, {lib!r})
import session_hook
session_hook.SETTINGS, session_hook.CLAUDE = {settings!r}, {python!r}
session_hook.main()
"""


@pytest.fixture
def trial(tmp_path):
    """A running collector, a settings file and a way to launch through the shim or not."""
    sock, log = tmp_path / "c.sock", tmp_path / "shim.jsonl"
    server = subprocess.Popen([sys.executable, str(LIB / "collector.py"), str(sock), str(log)])
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.05)
    settings = tmp_path / "shim.json"
    settings.write_text(json.dumps({"socket": str(sock), "claude": CLAUDE, "codex": CODEX}))
    cli = tmp_path / "fake-cli"
    cli.write_text(f"#!{sys.executable}\n" + FAKE_CLI)
    cli.chmod(0o755)
    home = tmp_path / "home"
    paths = {"lib": str(LIB), "settings": str(settings)}
    hook = HOOK.format(**paths, python=os.path.realpath(sys.executable))
    env = os.environ | {"FAKE_HOME": str(home), "FAKE_HOOK": hook, "PYTHONDONTWRITEBYTECODE": "1"}

    def launch(name: str, *argv: str, via_shim: bool = True, **extra: str):
        code = SHIM.format(**paths, cli=str(cli), name=name)
        cmd = [sys.executable, "-c", code, *argv] if via_shim else [str(cli), *argv]
        return subprocess.run(
            cmd, check=False, capture_output=True, text=True, env=env | extra, timeout=30
        )

    yield launch, log, home, tmp_path
    server.terminate()
    server.wait()


def wait_for_log(log: Path, count: int) -> list[dict]:
    """The collector's records once there are `count` of them (it writes as they arrive)."""
    for _ in range(100):
        records = read_shim_log(log)
        if len(records) >= count:
            return records
        time.sleep(0.05)
    return read_shim_log(log)


def transcripts_of(home: Path, root: Path) -> Path:
    """The rollouts as `collect` lays them out: container paths below one directory."""
    out = root / "transcripts"
    for file in home.rglob("rollout-*.jsonl"):
        target = out / str(file).lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.read_bytes())
    return out


@linux
def test_codex_launch_is_logged_and_its_rollouts_bound(trial):
    launch, log, home, root = trial

    result = launch("codex", "exec", "-s", "read-only", "-", FAKE_THREAD="t1", FAKE_EXIT="3")

    assert result.returncode == 3  # the CLI's status, and its stdio, reach the engine
    assert json.loads(result.stdout) == [
        *CODEX_FLAGS, "exec", "--ignore-rules", "-s", "danger-full-access", "-",
    ]  # fmt: skip
    records = wait_for_log(log, 5)
    assert [r["kind"] for r in records] == ["launch", "child", "watch", "binding", "exit"]
    launch_rec, child, watch, binding, exit_ = records
    assert launch_rec["argv"] == ["exec", "-s", "read-only", "-"]
    assert launch_rec["rewrites"] == [{"from": "read-only", "index": 2, "to": "danger-full-access"}]
    assert {r["launch"] for r in records} == {1}
    assert launch_rec["sender_pid"] == child["sender_pid"] != watch["sender_pid"]
    assert binding["meta"] == {"id": "t1", "source": "exec"} and binding["flags"] & 0o2000
    assert (binding["pid"], binding["start"]) == (child["pid"], child["start"])
    assert exit_["status"] == 3
    assert unbound_sessions(records, transcripts_of(home, root)) == []


@linux
def test_codex_child_thread_binds_through_its_launch_and_a_bypass_does_not(trial):
    launch, log, home, root = trial
    launch("codex", "exec", "-", FAKE_THREAD="t1")
    launch("codex", "exec", "-", FAKE_THREAD="t2", FAKE_PARENT="t1")  # parent in another launch
    launch("codex", "exec", "-", FAKE_THREAD="t3", via_shim=False)  # the real binary by path

    records = wait_for_log(log, 10)
    problems = unbound_sessions(records, transcripts_of(home, root))

    assert len(problems) == 2
    assert "Codex thread t2" in problems[0] and "parent thread t1 is not a root" in problems[0]
    assert "Codex thread t3" in problems[1] and "no origin launch" in problems[1]


@linux
def test_claude_hook_binds_a_shim_launch_and_exposes_a_bypass(trial):
    launch, log, _, root = trial
    via_shim = launch("claude", "-p", "hi", FAKE_SESSION="s1")
    by_path = launch("claude", "-p", "hi", FAKE_SESSION="s2", via_shim=False)
    assert via_shim.returncode == by_path.returncode == 0

    records = wait_for_log(log, 5)
    hooks = [r for r in records if r["kind"] == "hook"]
    child = next(r for r in records if r["kind"] == "child")
    assert [h["session_id"] for h in hooks] == ["s1", "s2"]
    assert (hooks[0]["claude_pid"], hooks[0]["claude_start"]) == (child["pid"], child["start"])
    transcripts = root / "transcripts"
    for session in ("s1", "s2"):
        file = transcripts / "home/node/.claude/projects/-work" / f"{session}.jsonl"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({"type": "user", "sessionId": session}) + "\n")

    problems = unbound_sessions(records, transcripts)

    assert len(problems) == 2
    assert "Claude session s2: its claude process" in problems[0]
    assert "Claude session s2 (" in problems[1] and "no origin launch" in problems[1]


@linux
def test_collector_refuses_a_watcher_no_shim_forked(trial):
    _, log, _, root = trial
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(str(root / "c.sock"))
        conn.sendall(b'{"kind": "watch"}\n')
        assert conn.recv(64) == b""  # closed without the ok

    (record,) = wait_for_log(log, 1)
    assert record["kind"] == "rejected" and "no open launch" in record["reason"]
    assert record["sender_pid"] == os.getpid()


@linux
def test_shim_fails_closed_without_its_collector(trial):
    launch, _, _, root = trial
    (root / "c.sock").unlink()

    result = launch("codex", "exec", "-", FAKE_THREAD="t1")

    assert result.returncode == 1 and "cannot reach the collector" in result.stderr
    assert result.stdout == ""  # the CLI never started
