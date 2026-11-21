"""The trial executor (spec run-ledger: Balanced queue order, Failures and retries, Concurrency per
provider, Limit detection and stop; spec trial-isolation, One fresh sandbox per trial; design D3, D7,
D8).

The queue and its slots are tested with a stand-in attempt; whole trials run through fake `docker`
and `security` (tests/fixtures/bin), whose replies stand in for the agent container: the engine's
stdout, the shim log, the transcripts and the proxy log. Every task, id and output is synthetic.
"""

import dataclasses
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
import test_codex_review
import test_rreview
from test_ledger import FINDING, USAGE, cli_inputs
from test_usage import GPT, codex_usage, message, meta, record, turn

import rbench.cli as cli_module
import rbench.ledger.plan as plan_module
import rbench.run.trial as trial_module
from rbench.cli import ExitCode, main
from rbench.engines import codex_review, plain, rreview
from rbench.ledger import (
    TRIALS_DIR,
    Plan,
    add_wasted,
    load_plan,
    read_results,
    read_status,
    read_wasted,
    set_state,
    start_run,
)
from rbench.ledger.plan import IDENTITY_FILE, PlannedTrial, run_identity
from rbench.ledger.resume import recover_trials
from rbench.run import executor
from rbench.run.adapters import ADAPTERS, EngineFailure, EngineTrial, NotEligible, Output, Plain
from rbench.run.trial import project_name
from rbench.tasks.hidden import HiddenConfig
from rbench.trial.compose import TrialProject
from rbench.trial.hermetic import POLICY
from rbench.trial.images import TrialError
from rbench.trial.repository import RepositoryState

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"
TOKEN = "sk-test-token"
PROXY_ID = "sha256:" + "1" * 64
CONTAINER = "c0ffee"
SESSION = "11111111-2222-3333-4444-555555555555"
TRANSCRIPT = f"/home/node/.claude/projects/-rbench-repo/{SESSION}.jsonl"
ROLLOUT = "/home/node/.codex/sessions/2026/11/21/rollout-t1.jsonl"
OBJECTS = 6
COUNT_OBJECTS = f"count: 0\nsize: 0\nin-pack: {OBJECTS}\npacks: 1\nsize-pack: 1\n"
COUNT_OBJECTS += "prune-packable: 0\ngarbage: 0\nsize-garbage: 0\n"
ENGINE = "agent sh -c " + trial_module.RUN
CODEX = "gpt-6.2-sol"
MARKER = "zz-synthetic-marker-41"
CLAUDE_FLAGS = POLICY["claude"]["flags"]
LIMITS = Path(__file__).parent / "fixtures" / "limits"


def make_run(root: Path, models: list[str], run_id: str = "r") -> Path:
    """A run of `plain` on `models` over the core tasks, created as `rbench run` creates it."""
    matrix = root / "matrices" / "m.toml"
    if not matrix.exists():
        cli_inputs(root)
        text = matrix.read_text().replace(f'["{OPUS}", "{SONNET}"]', json.dumps(models))
        matrix.write_text(text.replace("{ claude = 1000 }", "{ claude = 1000, codex = 1000 }"))
    return start_run(
        matrix,
        "r1",
        images_path=root / "images.json",
        engines_dir=root / "engines",
        tasks_root=root / "tasks",
        runs_dir=root / "runs",
        run_id=run_id,
    )


def with_attempts(run_dir: Path, attempts: int) -> Plan:
    """The run's plan with `attempts` attempts, in plan order, and a pending checkpoint for each
    trial: a screening run has one, and the queue logic is the same for any number."""
    plan = load_plan(run_dir)
    trials = []
    for attempt in range(1, attempts + 1):
        for task in plan.tasks:
            for c in plan.configurations:
                trial = PlannedTrial(f"{task.id}--{c.id[-12:]}--{attempt}", attempt, task.id, c.id)
                (run_dir / TRIALS_DIR / trial.id).mkdir(exist_ok=True)
                set_state(run_dir / TRIALS_DIR / trial.id, "pending")
                trials.append(trial)
    return dataclasses.replace(plan, attempts=attempts, trials=tuple(trials))


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(plan_module, "CHECKOUT_ROOT", tmp_path)
    return tmp_path


# The queue and its slots, with a stand-in attempt.


class Attempts:
    """A stand-in attempt that records when each trial ran and marks it done."""

    def __init__(self, run_dir: Path, seconds: float = 0.02) -> None:
        self.run_dir, self.seconds = run_dir, seconds
        self.spans: list[tuple[str, float, float]] = []
        self.lock = threading.Lock()

    def __call__(self, trial) -> str:
        start = time.monotonic()
        time.sleep(self.seconds)
        with self.lock:
            self.spans.append((trial.id, start, time.monotonic()))
        set_state(self.run_dir / TRIALS_DIR / trial.id, "failed", "stand-in")
        return "failed"


def overlap(spans: list[tuple[str, float, float]]) -> int:
    """The most spans running at one moment."""
    events = sorted([(s, 1) for _, s, _ in spans] + [(e, -1) for _, _, e in spans])
    now = most = 0
    for _, step in events:
        now += step
        most = max(most, now)
    return most


@pytest.mark.real_executor
def test_trials_on_one_codex_login_run_one_after_the_other(checkout):
    run_dir = make_run(checkout, [OPUS, SONNET, "gpt-6.2-sol"])
    plan = with_attempts(run_dir, 2)
    attempts = Attempts(run_dir)

    counts = executor.execute(run_dir, plan, plan.trials, attempts, log=lambda _: None)

    assert counts == {"failed": len(plan.trials)}
    codex = {t.id for t in plan.trials if "codex" in executor.slots(plan, t)}
    assert overlap([s for s in attempts.spans if s[0] in codex]) == 1
    assert overlap([s for s in attempts.spans if s[0] not in codex]) == 2  # Claude: default 2


def completed_spread(run_dir: Path, plan: Plan, group: frozenset[str]) -> float:
    """How far apart, in attempts, the configurations of a provider group are by their trials
    the ledger holds as final."""
    trials = [t for t in plan.trials if executor.slots(plan, t) == group]
    done: dict[str, int] = {t.configuration: 0 for t in trials}
    for t in trials:
        if read_status(run_dir / TRIALS_DIR / t.id).state not in (
            "pending",
            "running",
            "interrupted",
        ):
            done[t.configuration] += 1
    tasks = len(plan.tasks)
    return (max(done.values()) - min(done.values())) / tasks


@pytest.mark.real_executor
@pytest.mark.parametrize("one_task", [False, True])
def test_a_run_stopped_at_40_percent_keeps_each_provider_group_within_one_attempt(
    checkout, one_task
):
    # A trial stuck in its retries holds one slot while the other slot could run ahead; the
    # ledger is checked whenever a trial starts, as a run killed at that moment would leave it.
    run_dir = make_run(checkout, [OPUS, SONNET, CODEX])
    plan = with_attempts(run_dir, 5 if one_task else 3)
    if one_task:  # two Claude configurations (A, B), five attempts, two slots (review of #126)
        plan = dataclasses.replace(
            plan,
            tasks=plan.tasks[:1],
            trials=tuple(t for t in plan.trials if t.task == plan.tasks[0].id),
        )
    claude = [t for t in plan.trials if executor.slots(plan, t) == {"claude"}]
    stop_at = claude[1] if one_task else plan.trials[int(len(plan.trials) * 0.4)]
    started: list[str] = []
    spreads: list[float] = []
    ran = Attempts(run_dir, seconds=0.01)
    lock = threading.Lock()

    def attempt(trial):
        with lock:
            started.append(trial.id)
            for group in ({"claude"}, {"codex"}):
                spreads.append(completed_spread(run_dir, plan, frozenset(group)))
        if trial == stop_at:
            if started.count(trial.id) == 1:
                time.sleep(0.5)  # stuck: the other slots stay free meanwhile
            raise TrialError("docker went away")
        return ran(trial)

    with pytest.raises(executor.RunStopped, match="after 3 retries"):
        executor.execute(run_dir, plan, plan.trials, attempt, log=lambda _: None)

    assert started.count(stop_at.id) == 4  # the first try and 3 retries
    assert read_status(run_dir / TRIALS_DIR / stop_at.id).state == "interrupted"
    for group in ({"claude"}, {"codex"}):
        spreads.append(completed_spread(run_dir, plan, frozenset(group)))
    assert max(spreads) <= 1
    for group in ({"claude"}, {"codex"}):
        trials = [t for t in plan.trials if executor.slots(plan, t) == group]
        ran_ = [t for t in trials if t.id in started]
        assert ran_ == trials[: len(ran_)]  # a prefix in plan order
    assert len(started) < len(plan.trials) + 3  # nothing new started after the stop


@pytest.mark.real_executor
def test_an_infrastructure_error_is_retried(checkout):
    run_dir = make_run(checkout, [OPUS])
    plan = load_plan(run_dir)
    first = plan.trials[0]
    tries = []
    lines: list[str] = []

    def attempt(trial):
        tries.append(trial.id)
        trial_dir = run_dir / TRIALS_DIR / trial.id
        set_state(trial_dir, "running")
        if len(tries) <= 2:
            raise TrialError("proxy did not become healthy")
        set_state(trial_dir, "failed", "engine error")
        return "failed"

    counts = executor.execute(run_dir, plan, [first], attempt, log=lines.append)

    assert tries == [first.id] * 3
    assert counts == {"failed": 1}
    assert (
        f"trial {first.id}: infrastructure error: proxy did not become healthy; retry 1 of 3"
        in lines
    )
    assert any("retry 2 of 3" in line for line in lines)


@pytest.mark.real_executor
def test_a_teardown_failure_keeps_the_final_state_and_stops_the_run(checkout):
    run_dir = make_run(checkout, [CODEX])
    plan = with_attempts(run_dir, 3)
    tries = []

    def attempt(trial):
        tries.append(trial.id)
        trial_dir = run_dir / TRIALS_DIR / trial.id
        set_state(trial_dir, "running")
        set_state(trial_dir, "failed", "timeout")
        raise TrialError("removing trial project p failed")

    with pytest.raises(executor.RunStopped, match="failed, then infrastructure error: removing"):
        executor.execute(run_dir, plan, plan.trials, attempt, log=lambda _: None)

    assert tries == [plan.trials[0].id]  # not rerun, and nothing new started
    assert read_status(run_dir / TRIALS_DIR / tries[0]).reason == "timeout"
    assert trials_left(run_dir, plan) == [t.id for t in plan.trials[1:]]


def trials_left(run_dir: Path, plan: Plan) -> list[str]:
    return [t.id for t in recover_trials(run_dir, plan)]


@pytest.mark.real_executor
def test_an_error_the_attempt_cannot_record_stops_the_run_without_retry(checkout):
    run_dir = make_run(checkout, [OPUS])
    plan = load_plan(run_dir)
    tries = []

    def attempt(trial):
        tries.append(trial.id)
        set_state(run_dir / TRIALS_DIR / trial.id, "running")
        raise ValueError("the plan was frozen with another prompt")

    with pytest.raises(executor.RunStopped, match="another prompt"):
        executor.execute(run_dir, plan, plan.trials, attempt, claude_concurrency=1)

    assert len(tries) == 1
    assert read_status(run_dir / TRIALS_DIR / tries[0]).state == "interrupted"


def interrupt(trial_dir: Path) -> str:
    """What a trial's attempt records when a CLI reported a limit."""
    add_wasted(trial_dir, USAGE)
    set_state(trial_dir, "interrupted", "Claude reported its usage limit")
    return "interrupted"


@pytest.mark.real_executor
def test_a_limit_starts_nothing_new_lets_running_trials_finish_and_stops(checkout):
    run_dir = make_run(checkout, [OPUS, SONNET])
    plan = load_plan(run_dir)
    first, second = plan.trials[:2]
    started, finished, lines = [], [], []

    def attempt(trial):
        started.append(trial.id)
        trial_dir = run_dir / TRIALS_DIR / trial.id
        set_state(trial_dir, "running")
        if trial == first:
            return interrupt(trial_dir)
        time.sleep(0.3)  # still running when the limit is reported
        set_state(trial_dir, "failed", "stand-in")
        finished.append(trial.id)
        return "failed"

    with pytest.raises(executor.LimitReached) as stopped:
        executor.execute(run_dir, plan, plan.trials, attempt, log=lines.append)

    assert len(plan.trials) > 2 and sorted(started) == sorted([first.id, second.id])
    assert finished == [second.id]
    assert str(stopped.value) == f"trial {first.id}: interrupted: Claude reported its usage limit"
    assert str(stopped.value) in lines
    assert read_wasted(run_dir / TRIALS_DIR / first.id) == [USAGE]
    # Resume reruns the interrupted trial and the ones never started.
    assert trials_left(run_dir, plan) == [first.id, *[t.id for t in plan.trials[2:]]]


@pytest.mark.real_executor
def test_a_teardown_failure_after_a_limit_keeps_the_limit_stop_and_reports_the_error(checkout):
    run_dir = make_run(checkout, [OPUS])
    plan = load_plan(run_dir)
    tries = []

    def attempt(trial):
        tries.append(trial.id)
        trial_dir = run_dir / TRIALS_DIR / trial.id
        set_state(trial_dir, "running")
        interrupt(trial_dir)
        raise TrialError("removing trial project p failed")

    with pytest.raises(executor.LimitReached) as stopped:
        executor.execute(run_dir, plan, plan.trials, attempt, claude_concurrency=1)

    name = f"trial {plan.trials[0].id}"
    assert str(stopped.value) == f"{name}: interrupted: Claude reported its usage limit"
    assert stopped.value.errors == [
        f"{name}: interrupted, then infrastructure error: removing trial project p failed"
    ]
    assert tries == [plan.trials[0].id]  # not rerun
    status = read_status(run_dir / TRIALS_DIR / tries[0])
    assert (status.state, status.reason) == ("interrupted", "Claude reported its usage limit")
    assert read_wasted(run_dir / TRIALS_DIR / tries[0]) == [USAGE]


@pytest.mark.real_executor
def test_a_limit_and_another_trials_infrastructure_failure_stop_as_a_limit(checkout):
    run_dir = make_run(checkout, [OPUS, SONNET])
    plan = load_plan(run_dir)
    first, second = plan.trials[:2]

    def attempt(trial):
        trial_dir = run_dir / TRIALS_DIR / trial.id
        set_state(trial_dir, "running")
        if trial == first:
            return interrupt(trial_dir)
        raise TrialError("proxy did not become healthy")

    with pytest.raises(executor.LimitReached) as stopped:
        executor.execute(run_dir, plan, plan.trials, attempt, retries=0, log=lambda _: None)

    assert str(stopped.value) == f"trial {first.id}: interrupted: Claude reported its usage limit"
    assert stopped.value.errors == [
        f"trial {second.id}: infrastructure error: proxy did not become healthy (after 0 retries)"
    ]


@pytest.mark.real_executor
def test_no_trial_starts_once_a_limit_is_detected_even_before_its_teardown_ends(checkout):
    run_dir = make_run(checkout, [OPUS, SONNET])
    plan = load_plan(run_dir)
    first, second = plan.trials[:2]
    limit, teardown, started = threading.Event(), threading.Event(), []

    def attempt(trial):
        started.append(trial.id)
        trial_dir = run_dir / TRIALS_DIR / trial.id
        set_state(trial_dir, "running")
        if trial == first:
            limit.set()  # as `run_attempt` does on detection, before removing its project
            interrupt(trial_dir)
            assert teardown.wait(5)
            return "interrupted"
        while not limit.is_set():
            time.sleep(0.01)
        set_state(trial_dir, "failed", "stand-in")  # frees its slot while the first tears down
        threading.Timer(0.3, teardown.set).start()
        return "failed"

    with pytest.raises(executor.LimitReached):
        executor.execute(run_dir, plan, plan.trials, attempt, log=lambda _: None, limit=limit)

    assert len(plan.trials) > 2 and sorted(started) == sorted([first.id, second.id])


@pytest.mark.real_executor
def test_a_trial_a_limit_interrupted_before_is_retried_on_infrastructure_errors(checkout):
    run_dir = make_run(checkout, [OPUS])
    plan = load_plan(run_dir)
    first = plan.trials[0]
    interrupt(run_dir / TRIALS_DIR / first.id)  # the previous runner's limit
    tries = []

    def attempt(trial):
        tries.append(trial.id)
        if len(tries) == 1:  # fails before the trial is marked running
            raise TrialError("listing agents on the Codex login failed")
        set_state(run_dir / TRIALS_DIR / trial.id, "failed", "engine error")
        return "failed"

    assert executor.execute(run_dir, plan, [first], attempt, log=lambda _: None) == {"failed": 1}
    assert tries == [first.id] * 2


def test_a_limit_exits_75_and_prints_the_exact_resume_command(checkout, monkeypatch, capsys):
    def run_trials(ctx, queue, **options):
        raise executor.LimitReached(["trial 1: interrupted: Codex reported its rate limit"])

    monkeypatch.setattr(executor, "run_trials", run_trials)
    runs = checkout / "my runs"  # quoted in the command
    argv = [*cli_inputs(checkout), "--runs", str(runs), "--run-id", "r"]

    assert main([*argv, "--claude-concurrency", "1"]) == ExitCode.STOPPED == 75

    err = capsys.readouterr().err
    assert "rbench run: stopped at a limit: trial 1: interrupted: Codex reported its" in err
    resume = (
        f"rbench resume --engines={checkout / 'engines'} --tasks={checkout / 'tasks'}"
        f" --claude-concurrency=1 -- '{runs / 'r'}'"
    )
    assert f"\nresume after the limit resets: {resume}\n" in err


def shell_words(command: str, **variables: str) -> list[str]:
    """The words `/bin/sh` makes of `command`, with `variables` set."""
    script = "".join(f"{k}={shlex.quote(v)}; " for k, v in variables.items())
    script += f"set -- {command}; printf '%s\\0' \"$@\""
    done = subprocess.run(["/bin/sh", "-c", script], capture_output=True, check=True)
    return done.stdout.decode().split("\0")[:-1]


@pytest.mark.parametrize("hidden", [False, True])
def test_the_resume_command_survives_the_shell_and_the_parser(hidden):
    args = cli_module.build_parser().parse_args(
        [
            "resume",
            "--engines=-engines",
            "--tasks=-my tasks",
            "--archives=$HOME/'a'",
            "--claude-concurrency=3",
            "--",
            "-runs/r",
        ]
    )
    plan = types.SimpleNamespace(task_sets={"hidden": "1"} if hidden else {"core": "1"})

    command = cli_module._resume_command(args.run, plan, args)

    assert ("-runs" in command) is not hidden  # a hidden run's directory stays unprinted
    words = shell_words(command, RUN_DIR="-runs/r") if hidden else shell_words(command)
    assert words[:2] == ["rbench", "resume"]
    assert cli_module.build_parser().parse_args(words[1:]) == args
    defaults = cli_module.build_parser().parse_args(["resume", "r"])
    assert cli_module._resume_command(Path("r"), plan, defaults).startswith("rbench resume -- ")


@pytest.mark.parametrize("command", ["run", "resume"])
def test_a_limit_with_an_error_still_exits_75_and_reports_both(
    checkout, monkeypatch, capsys, command
):
    def run_trials(ctx, queue, **options):
        raise executor.LimitReached(
            ["trial 1: interrupted: Claude reported its usage limit"],
            ["trial 2: infrastructure error: proxy did not become healthy (after 3 retries)"],
        )

    argv = cli_inputs(checkout)
    if command == "resume":
        run_dir = make_run(checkout, [OPUS])
        engines, tasks = argv[argv.index("--engines") + 1], argv[argv.index("--tasks") + 1]
        argv = ["resume", str(run_dir), "--engines", engines, "--tasks", tasks]
    monkeypatch.setattr(executor, "run_trials", run_trials)

    assert main(argv) == ExitCode.STOPPED

    err = capsys.readouterr().err
    assert f"rbench {command}: stopped at a limit: trial 1: interrupted: Claude" in err
    assert f"rbench {command}: also stopped on an error: trial 2: infrastructure error" in err
    assert "fix the cause, then resume after the limit resets: rbench resume " in err


def test_resume_after_a_limit_queues_the_interrupted_trial(checkout, stub_executor, capsys):
    argv = cli_inputs(checkout)
    run_dir = make_run(checkout, [OPUS])
    plan = load_plan(run_dir)
    first = run_dir / TRIALS_DIR / plan.trials[0].id
    set_state(first, "running")
    interrupt(first)
    for trial in plan.trials[1:]:
        set_state(run_dir / TRIALS_DIR / trial.id, "failed", "engine error")
    engines, tasks = argv[argv.index("--engines") + 1], argv[argv.index("--tasks") + 1]

    assert main(["resume", str(run_dir), "--engines", engines, "--tasks", tasks]) == ExitCode.OK
    assert stub_executor.queues == [[plan.trials[0].id]]
    assert read_wasted(first) == [USAGE]


def test_project_names_are_unique_per_run_and_trial_and_hide_the_task(tmp_path):
    a = project_name("0" * 32, "o-1--abc--1")

    assert a == project_name("0" * 32, "o-1--abc--1")
    assert a != project_name("1" * 32, "o-1--abc--1")
    assert a != project_name("0" * 32, "o-1--abc--2")
    assert a.startswith("rbench-") and "o-1" not in a


@pytest.mark.real_executor
def test_project_names_follow_the_run_identity_not_its_path(checkout, fake_bin):
    run_dir = make_run(checkout, [OPUS])
    other = make_run(checkout, [OPUS], run_id="r2")
    plan = load_plan(run_dir)
    trial = plan.trials[1]
    identity = (run_dir / IDENTITY_FILE).read_text().strip()
    assert len(identity) == 32 and int(identity, 16) >= 0
    mine = project_name(run_identity(run_dir), trial.id)
    assert mine != project_name(run_identity(other), trial.id)  # same trial id, another run

    moved = run_dir.rename(checkout / "elsewhere")
    assert project_name(run_identity(moved), trial.id) == mine
    listing = {"match": "--filter label=com.docker.compose.project", "stdout": f"{mine}\n"}
    fake_bin.scenario("docker", replies=[listing])
    assert executor.remove_leftovers(moved, plan, f"rbench-proxy@{PROXY_ID}") == [mine]


def test_a_run_without_an_identity_gets_one_that_stays(tmp_path):
    first = run_identity(tmp_path)

    assert first == run_identity(tmp_path) == (tmp_path / IDENTITY_FILE).read_text().strip()
    (tmp_path / IDENTITY_FILE).write_text("../x\n")
    with pytest.raises(ValueError, match="not a run identity"):
        run_identity(tmp_path)


def test_run_holds_the_lock_from_creation_through_its_trials(checkout, monkeypatch):
    seen = []

    def run_trials(ctx, queue, **options):
        run_dir = checkout / "runs" / "r"
        second = subprocess.run(
            [sys.executable, "-m", "rbench", "resume", str(run_dir)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        seen.append(second)
        return {}

    monkeypatch.setattr(executor, "run_trials", run_trials)
    assert main([*cli_inputs(checkout), "--run-id", "r"]) == ExitCode.OK

    (second,) = seen
    assert second.returncode == ExitCode.ERROR
    assert "run is locked by pid" in second.stderr
    assert (checkout / "runs" / "r" / "ledger.lock").read_bytes() == b""


# Whole trials through fake docker.


def shim_log(*records: dict) -> str:
    return "".join(json.dumps({"seq": n} | r) + "\n" for n, r in enumerate(records, 1))


def claude_trial(launch: plain.Launch) -> dict:
    """What the agent container holds after a `plain` Claude session: replies for fake docker."""
    final = [*CLAUDE_FLAGS, *launch.argv[1:]]
    log = shim_log(
        {"kind": "launch", "launch": 1, "cli": "claude", "argv": list(launch.argv[1:])}
        | {"final_argv": final, "added": list(CLAUDE_FLAGS), "rewrites": []},
        {"kind": "child", "launch": 1, "pid": 101, "start": 1},
        {"kind": "hook", "session_id": SESSION, "source": "startup"}
        | {"claude_pid": 101, "claude_start": 1, "transcript_path": TRANSCRIPT},
        {"kind": "exit", "launch": 1, "status": 0},
    )
    findings = [FINDING | {"severity": "must_fix"}]
    result = {"type": "result", "subtype": "success", "is_error": False}
    result["structured_output"] = {"findings": findings}
    transcript = json.dumps(message(SESSION, "msg-1", OPUS, 3, 2)) + "\n"
    return {
        "stdout": json.dumps(result),
        "diff": f"A {TRANSCRIPT}\n",
        "copy": {"agent:/var/log/rbench/shim.jsonl": log, f"agent:{TRANSCRIPT}": transcript},
    }


def codex_trial(launch: plain.Launch) -> dict:
    """The same for a `plain` Codex session, which writes its answer to a file."""
    log = shim_log(
        {"kind": "launch", "launch": 1, "cli": "codex", "argv": list(launch.argv[1:])}
        | {"final_argv": list(launch.argv[1:]), "added": ["--strict-config"], "rewrites": []},
        {"kind": "child", "launch": 1, "pid": 101, "start": 1},
        {"kind": "binding", "launch": 1, "path": ROLLOUT, "meta": {"id": "t1", "source": "exec"}},
        {"kind": "exit", "launch": 1, "status": 0},
    )
    rollout = [meta("t1"), turn("u1", GPT), record("t1", "u1", "r1", codex_usage(10, 4, 3))]
    answer = {"findings": [FINDING | {"severity": "nit"}]}
    return {
        "stdout": "",
        "diff": f"A {ROLLOUT}\n",
        "copy": {
            "agent:/var/log/rbench/shim.jsonl": log,
            f"agent:{ROLLOUT}": "".join(json.dumps(line) + "\n" for line in rollout),
            f"agent:{plain.OUTPUT_PATH}": json.dumps(answer),
        },
    }


def launched(status: int = 0, outcome: str = "exit") -> str:
    """The line the launch wrapper ends the engine's stderr with in the agent."""
    return f"engine noise\n{trial_module.MARK} {outcome} {status}\n"


def docker_replies(trial: dict, *, engine: dict | None = None, extra: list | None = None) -> dict:
    copy = trial["copy"] | {"proxy:/var/log/squid/access.log": ""}
    return {
        "replies": [
            *(extra or []),
            {"match": "build --quiet --tag rbench-proxy", "stdout": PROXY_ID + "\n"},
            {"match": ENGINE, "stdout": trial["stdout"], "stderr": launched()} | (engine or {}),
            {"match": "count-objects", "stdout": COUNT_OBJECTS},
            {"match": "ps --quiet agent", "stdout": CONTAINER + "\n"},
            {"match": f"diff {CONTAINER}", "stdout": trial["diff"]},
            {"match": " cp ", "copy": copy},
        ]
    }


@pytest.fixture
def sandbox(checkout, fake_bin, monkeypatch, tmp_path_factory):
    """A run of one `plain` trial per model, ready for `run` (fake docker and Keychain)."""
    fake_bin.scenario("security", stdout=TOKEN + "\n")
    login = tmp_path_factory.mktemp("codex-login")
    (login / "auth.json").write_text("{}")
    monkeypatch.setenv(executor.CODEX_LOGIN_ENV, str(login))
    state = RepositoryState("a" * 40, "b" * 40, OBJECTS)
    monkeypatch.setattr(trial_module, "prepare_repository", lambda *args: state)

    def run(models: list[str], replies, adapters=ADAPTERS, timeout=None):
        run_dir = make_run(checkout, models)
        plan = load_plan(run_dir)
        trial = plan.trials[0]
        settings = next(c for c in plan.configurations if c.id == trial.configuration).settings
        launch = plain.launch(checkout / "engines", settings, plan.engines["plain"].clis)
        fake_bin.scenario("docker", **replies(launch))
        ctx = executor.prepare_run(
            run_dir,
            plan,
            engines_dir=checkout / "engines",
            tasks_root=checkout / "tasks",
            archives=checkout / "archives",
            adapters=adapters,
        )
        if timeout is not None:
            ctx.timeout = timeout
        counts = executor.run_trials(ctx, plan.trials[:1], log=lambda _: None)
        return run_dir / TRIALS_DIR / trial.id, counts, plan

    return run


def docker_calls(fake_bin, needle: str) -> list[list[str]]:
    return [
        c["argv"]
        for c in fake_bin.calls()
        if c["tool"] == "docker" and needle in " ".join(c["argv"])
    ]


@pytest.mark.real_executor
def test_a_claude_trial_runs_in_its_project_and_stores_findings_and_usage(sandbox, fake_bin):
    trial_dir, counts, _ = sandbox([OPUS], lambda launch: docker_replies(claude_trial(launch)))

    assert counts == {"done": 1}
    findings, usage = read_results(trial_dir)
    assert findings == [FINDING | {"severity": "must_fix"}]
    assert usage["engine"] == {
        OPUS: {"input": 3, "output": 2, "cache_read": 0, "cache_creation": 0}
    }
    assert usage["incomplete"]["engine"] == []
    name = project_name(run_identity(trial_dir.parents[1]), trial_dir.name)
    ups = docker_calls(fake_bin, "up --detach --wait")
    assert len(ups) == 1 and ups[0][ups[0].index("--project-name") + 1] == name
    (engine,) = docker_calls(fake_bin, ENGINE)
    wrapper = engine.index(trial_module.RUN)
    assert engine[wrapper + 1 : wrapper + 6] == ["sh", "/rbench/repo", "30s", "3600s", "claude"]
    up = next(c for c in fake_bin.calls() if "up" in c["argv"] and c["tool"] == "docker")
    assert TOKEN not in json.dumps(up["argv"])
    assert docker_calls(fake_bin, "down")[-1][2] == name  # removed after the trial
    assert json.loads((trial_dir / "audit.json").read_text())["contaminated"] == []


@pytest.mark.real_executor
def test_a_plain_codex_trial_stores_usage_with_its_model_unverified(sandbox):
    trial_dir, counts, _ = sandbox(
        ["gpt-6.2-sol"], lambda launch: docker_replies(codex_trial(launch))
    )

    assert counts == {"done": 1}
    findings, usage = read_results(trial_dir)
    assert findings == [FINDING | {"severity": "nit"}]
    assert usage["engine"] == {GPT: {"input": 6, "output": 3, "cache_read": 4, "cache_creation": 0}}
    # plain's launch has no `--json`: no stdout events can verify the model (D7).
    unverified = "Codex thread t1: no clean stdout of a launch shows it, so the model its usage"
    assert usage["incomplete"]["engine"] == [f"{unverified} ran on is unverified"]


def test_only_a_codex_exec_json_launch_passes_its_stdout_to_usage(tmp_path):
    def launch(*argv):
        return plain.Launch("codex", ("codex", *argv), "", {}, None)

    def record(n, argv):
        return {"kind": "launch", "launch": n, "cli": "codex", "argv": list(argv)}

    stdout = tmp_path / trial_module.STDOUT_FILE
    own = ("exec", "--json", "-m", GPT, "-")
    records = [record(1, ("exec", "-")), record(2, own)]
    assert trial_module._codex_stdout(launch(*own), records, tmp_path) == {2: stdout}
    assert trial_module._codex_stdout(launch("exec", "-"), records, tmp_path) == {}
    assert trial_module._codex_stdout(launch("exec", "--", "--json"), records, tmp_path) == {}
    twice = [*records, record(3, own)]  # which launch is the engine's is unknown
    assert trial_module._codex_stdout(launch(*own), twice, tmp_path) == {}


@pytest.mark.real_executor
@pytest.mark.parametrize("status", [124, 137])  # `timeout`'s TERM, then KILL
def test_a_timeout_fails_the_trial_and_resume_does_not_rerun_it(sandbox, status):
    def replies(launch):
        engine = {"exit": status, "stderr": launched(status, "timeout")}
        return docker_replies(claude_trial(launch), engine=engine)

    trial_dir, counts, plan = sandbox([OPUS], replies, timeout=1.0)

    assert counts == {"failed": 1}
    assert read_status(trial_dir).reason == "timeout"
    assert trial_dir.name not in [t.id for t in recover_trials(trial_dir.parents[1], plan)]


@pytest.mark.real_executor
def test_time_in_docker_outside_the_agent_is_not_a_timeout(sandbox):
    # 1.2 s on the host, but the wrapper saw the engine end well inside its 1 s limit.
    def replies(launch):
        return docker_replies(claude_trial(launch), engine={"delay": 1.2, "stderr": launched(0)})

    _, counts, _ = sandbox([OPUS], replies, timeout=1.0)

    assert counts == {"done": 1}


@pytest.mark.real_executor
def test_an_engine_exiting_124_on_its_own_is_an_engine_error(sandbox):
    def replies(launch):
        engine = {"exit": 124, "stderr": launched(124)}
        return docker_replies(claude_trial(launch), engine=engine)

    trial_dir, counts, _ = sandbox([OPUS], replies, timeout=5.0)

    assert counts == {"failed": 1}
    assert read_status(trial_dir).reason == "engine error: claude exited 124"


@pytest.mark.real_executor
def test_a_stuck_docker_exec_is_cut_off_and_retried(sandbox, fake_bin, monkeypatch):
    monkeypatch.setattr(trial_module, "KILL_AFTER", 0)
    monkeypatch.setattr(trial_module, "HOST_GRACE", 0.3)

    def replies(launch):
        return docker_replies(claude_trial(launch), engine={"delay": 5})

    with pytest.raises(executor.RunStopped, match=r"did not return within 0\.5s.*after 3 retries"):
        sandbox([OPUS], replies, timeout=0.2)

    assert len(docker_calls(fake_bin, ENGINE)) == 4
    assert docker_calls(fake_bin, " down ")  # each attempt still removed its project


@pytest.mark.real_executor
def test_a_docker_exec_failure_before_the_engine_is_retried(sandbox, fake_bin, checkout):
    def replies(launch):
        engine = {"exit": 1, "stderr": f"Error response from daemon: {MARKER}\n"}
        return docker_replies(claude_trial(launch), engine=engine)

    with pytest.raises(executor.RunStopped, match="did not complete in the agent") as stopped:
        sandbox([OPUS], replies)

    assert len(docker_calls(fake_bin, ENGINE)) == 4  # the first try and 3 retries
    status = first_trial(checkout) / "status.json"
    assert json.loads(status.read_text())["state"] == "interrupted"
    assert MARKER not in str(stopped.value) and MARKER not in status.read_text()


def first_trial(checkout: Path) -> Path:
    run_dir = checkout / "runs" / "r"
    return run_dir / TRIALS_DIR / load_plan(run_dir).trials[0].id


@pytest.mark.real_executor
def test_a_pr_the_engine_refuses_fails_the_trial_rather_than_finding_nothing(sandbox):
    class Refusing(Plain):
        def result(self, trial, launch, output):
            raise NotEligible("the PR is closed")

    replies = lambda launch: docker_replies(claude_trial(launch))
    trial_dir, counts, _ = sandbox([OPUS], replies, adapters={"plain": Refusing()})

    assert counts == {"failed": 1}
    assert read_status(trial_dir).reason == "PR not eligible: the PR is closed"
    assert not (trial_dir / "findings.json").exists()


@pytest.mark.real_executor
def test_the_audit_wins_over_a_good_result(sandbox):
    def replies(launch):
        changed = {
            "match": "count-objects",
            "stdout": COUNT_OBJECTS.replace("in-pack: 6", "in-pack: 7"),
        }
        return docker_replies(claude_trial(launch), extra=[changed])

    trial_dir, counts, _ = sandbox([OPUS], replies)

    assert counts == {"contaminated": 1}
    assert "object count changed from 6 to 7" in read_status(trial_dir).reason


@pytest.mark.real_executor
def test_an_engine_launch_not_as_built_makes_the_trial_misconfigured(sandbox):
    def replies(launch):
        trial = claude_trial(launch)
        log = trial["copy"]["agent:/var/log/rbench/shim.jsonl"]
        trial["copy"]["agent:/var/log/rbench/shim.jsonl"] = log.replace("dontAsk", "default")
        return docker_replies(trial)

    trial_dir, counts, _ = sandbox([OPUS], replies)

    assert counts == {"misconfigured": 1}
    assert "--permission-mode" in read_status(trial_dir).reason


def limit_line(fixture: str, **fields) -> str:
    """The record a pinned CLI ended its turn with at a limit (tests/fixtures/limits/)."""
    return json.dumps(json.loads((LIMITS / fixture).read_text().splitlines()[-1]) | fields) + "\n"


def claude_limit_trial(launch: plain.Launch) -> dict:
    """A Claude session that used tokens, then hit its 5-hour limit."""
    trial = claude_trial(launch)
    line = limit_line("claude/transcript-usage-limit-five-hour.stub.jsonl", sessionId=SESSION)
    trial["copy"][f"agent:{TRANSCRIPT}"] += line
    trial["stdout"] = json.dumps({"type": "result", "subtype": "success", "is_error": True})
    return trial


@pytest.mark.real_executor
def test_a_claude_usage_limit_interrupts_the_trial_keeping_its_tokens_as_wasted(
    sandbox, fake_bin, checkout
):
    def replies(launch):
        engine = {"exit": 1, "stderr": launched(1)}
        return docker_replies(claude_limit_trial(launch), engine=engine)

    with pytest.raises(executor.LimitReached, match="Claude reported its usage limit"):
        sandbox([OPUS], replies)

    trial_dir = first_trial(checkout)
    status = read_status(trial_dir)
    assert (status.state, status.reason) == ("interrupted", "Claude reported its usage limit")
    (wasted,) = read_wasted(trial_dir)
    assert wasted["engine"] == {
        OPUS: {"input": 3, "output": 2, "cache_read": 0, "cache_creation": 0}
    }
    assert not (trial_dir / "findings.json").exists()
    assert docker_calls(fake_bin, " down ")  # the project is still removed

    # Resume reruns it from the start; the wasted tokens stay.
    run_dir = trial_dir.parents[1]
    plan = load_plan(run_dir)
    queue = recover_trials(run_dir, plan)
    assert queue[0].id == trial_dir.name
    settings = plan.configurations[0].settings
    launch = plain.launch(checkout / "engines", settings, plan.engines["plain"].clis)
    fake_bin.scenario("docker", **docker_replies(claude_trial(launch)))
    ctx = executor.prepare_run(
        run_dir,
        plan,
        engines_dir=checkout / "engines",
        tasks_root=checkout / "tasks",
        archives=checkout / "archives",
    )
    assert executor.run_trials(ctx, queue[:1], log=lambda _: None) == {"done": 1}
    assert read_wasted(trial_dir) == [wasted]
    assert read_results(trial_dir)[1]["engine"] == wasted["engine"]


@pytest.mark.real_executor
def test_a_repository_file_holding_a_limit_record_does_not_interrupt_the_trial(sandbox):
    replay = "/rbench/repo/testdata/projects/sample/replay.jsonl"

    def replies(launch):
        trial = claude_trial(launch)
        trial["diff"] += f"A {replay}\n"
        trial["copy"][f"agent:{replay}"] = limit_line(
            "claude/transcript-usage-limit-five-hour.stub.jsonl"
        )
        return docker_replies(trial)

    trial_dir, counts, _ = sandbox([OPUS], replies)

    assert counts == {"done": 1}
    assert read_wasted(trial_dir) == []


@pytest.mark.real_executor
@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("codex/rollout-usage-limit-plus.stub.jsonl", "Codex reported its usage limit"),
        ("codex/rollout-retry-limit-429.stub.jsonl", "Codex reported its rate limit"),
    ],
)
def test_a_codex_limit_interrupts_the_trial_keeping_its_tokens_as_wasted(
    sandbox, checkout, fixture, reason
):
    def replies(launch):
        trial = codex_trial(launch)
        trial["copy"][f"agent:{ROLLOUT}"] += limit_line(fixture)
        return docker_replies(trial, engine={"exit": 1, "stderr": launched(1)})

    with pytest.raises(executor.LimitReached, match=reason):
        sandbox([CODEX], replies)

    trial_dir = first_trial(checkout)
    assert read_status(trial_dir).reason == reason
    (wasted,) = read_wasted(trial_dir)
    assert wasted["engine"] == {
        GPT: {"input": 6, "output": 3, "cache_read": 4, "cache_creation": 0}
    }


@pytest.mark.real_executor
def test_the_audit_wins_over_a_limit(sandbox, checkout):
    def replies(launch):
        changed = {
            "match": "count-objects",
            "stdout": COUNT_OBJECTS.replace("in-pack: 6", "in-pack: 7"),
        }
        return docker_replies(claude_limit_trial(launch), extra=[changed])

    trial_dir, counts, _ = sandbox([OPUS], replies)

    assert counts == {"contaminated": 1}
    assert read_wasted(trial_dir) == []


@pytest.mark.real_executor
def test_malformed_usage_at_a_limit_is_wasted_as_unknown(sandbox, checkout):
    def replies(launch):
        trial = claude_limit_trial(launch)
        bad = json.dumps(message(SESSION, "msg-2", OPUS, -1, 2)) + "\n"
        trial["copy"][f"agent:{TRANSCRIPT}"] += bad
        return docker_replies(trial)

    with pytest.raises(executor.LimitReached):
        sandbox([OPUS], replies)

    (wasted,) = read_wasted(first_trial(checkout))
    assert wasted["engine"] == {}
    assert wasted["incomplete"]["engine"] == [
        "the transcripts' usage is malformed, so it is unknown"
    ]


@pytest.mark.real_executor
def test_a_start_that_keeps_failing_is_retried_three_times_then_stops(sandbox, fake_bin):
    def replies(launch):
        failing = {"match": "up --detach --wait", "exit": 1, "stderr": "network create failed"}
        return docker_replies(claude_trial(launch), extra=[failing])

    with pytest.raises(executor.RunStopped, match="network create failed.*after 3 retries"):
        sandbox([OPUS], replies)

    assert len(docker_calls(fake_bin, "up --detach --wait")) == 4


@pytest.mark.real_executor
def test_startup_removes_only_this_runs_leftover_projects(checkout, fake_bin, monkeypatch):
    run_dir = make_run(checkout, [OPUS])
    plan = load_plan(run_dir)
    mine = project_name(run_identity(run_dir), plan.trials[1].id)
    listing = {"match": "--filter label=com.docker.compose.project", "stdout": f"other\n{mine}\n"}
    fake_bin.scenario("docker", replies=[listing])

    assert executor.remove_leftovers(run_dir, plan, f"rbench-proxy@{PROXY_ID}") == [mine]
    downs = docker_calls(fake_bin, " down ")
    assert [d[d.index("--project-name") + 1] for d in downs] == [mine]


@pytest.mark.real_executor
def test_an_engine_without_an_adapter_is_refused_before_any_trial(checkout):
    run_dir = make_run(checkout, [OPUS])

    with pytest.raises(ValueError, match="no adapter for engine plain"):
        executor.prepare_run(
            run_dir,
            load_plan(run_dir),
            engines_dir=checkout / "engines",
            tasks_root=checkout / "tasks",
            archives=checkout,
            adapters={},
        )


def engine_trial(settings: dict) -> EngineTrial:
    return EngineTrial(settings, None, Path("engines"), 7)  # type: ignore[arg-type]


def output(status: int = 0, stdout: str = "", records=(), transcripts=Path("none")) -> Output:
    return Output(status, stdout, None, list(records), transcripts)


def test_rreview_is_registered_and_runs_its_launch_with_findings_from_stdout():
    adapter, trial = ADAPTERS["rreview"], engine_trial(test_rreview.settings())
    launch = adapter.launch(trial)
    assert (launch.cli, launch.stdin, launch.files, launch.output) == ("rreview", "", {}, None)
    assert launch.argv == rreview.launch(trial.settings).argv
    assert adapter.homes(trial).by_flag == {}  # rreview passes model and effort itself

    finding = test_rreview.FINDING
    out = adapter.result(trial, launch, output(1, json.dumps(test_rreview.result([finding]))))
    assert [f["severity"] for f in out] == ["P1"]
    with pytest.raises(EngineFailure, match=r"^engine error: rreview exited 2$"):
        adapter.result(trial, launch, output(2, "{}"))
    with pytest.raises(EngineFailure, match=r"^invalid output: rreview: stdout is not JSON$"):
        adapter.result(trial, launch, output(0, "not json"))


def test_codex_review_is_registered_with_findings_from_its_root_rollout(tmp_path):
    adapter = ADAPTERS["codex-review"]
    trial = engine_trial(test_codex_review.settings())
    launch = adapter.launch(trial)
    assert launch == codex_review.launch(trial.settings)
    records = test_codex_review.records((test_codex_review.ROOT, test_codex_review.ROOT_META))
    assert adapter.check(trial, launch, records) == []
    # Its `exec --json` stdout is the evidence count_usage reads for its launch (design D7).
    assert trial_module._codex_stdout(launch, records, tmp_path) == {
        1: tmp_path / trial_module.STDOUT_FILE
    }

    review = test_codex_review.review(test_codex_review.finding())
    test_codex_review.rollout(tmp_path, test_codex_review.ROOT, review)
    assert adapter.result(trial, launch, output(0, records=records, transcripts=tmp_path)) == [
        test_codex_review.TWO_FINDINGS[0]
    ]
    with pytest.raises(EngineFailure, match=r"^engine error: codex exited 1$"):
        adapter.result(trial, launch, output(1, records=records, transcripts=tmp_path))
    with pytest.raises(EngineFailure, match=r"^invalid output: codex-review: expected one root"):
        adapter.result(trial, launch, output(0, transcripts=tmp_path))


HOLD_LOGIN = """
import sys, time
from pathlib import Path
from rbench.run.trial import login_lock
with login_lock(Path(sys.argv[1])):
    print("held", flush=True)
    time.sleep(0.5)
    Path(sys.argv[2]).write_text(repr(time.time()))
"""


@pytest.mark.real_executor
def test_a_codex_trial_waits_while_another_runner_holds_its_login(sandbox, monkeypatch, tmp_path):
    # Another run's runner, a separate process, holds the same login home.
    login = Path(os.environ[executor.CODEX_LOGIN_ENV])
    released = tmp_path / "released"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOGIN, str(login), str(released)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None and holder.stdout.readline() == "held\n"
    ups: list[float] = []
    up = TrialProject.up

    def recorded_up(self):
        ups.append(time.time())
        return up(self)

    monkeypatch.setattr(TrialProject, "up", recorded_up)
    _, counts, _ = sandbox([CODEX], lambda launch: docker_replies(codex_trial(launch)))

    assert holder.wait(timeout=30) == 0
    assert counts == {"done": 1}
    assert ups and ups[0] >= float(released.read_text())


@pytest.mark.real_executor
def test_a_teardown_that_keeps_failing_is_retried_then_stops_the_run(sandbox, monkeypatch):
    downs = []

    def down(self):
        downs.append(self.name)
        if len(downs) > 1:  # the one `up` runs first succeeds
            raise TrialError(f"removing trial project {self.name}: docker compose exited 1")

    monkeypatch.setattr(TrialProject, "down", down)

    with pytest.raises(executor.RunStopped, match="done, then infrastructure error: removing"):
        sandbox([OPUS], lambda launch: docker_replies(claude_trial(launch)))

    assert len(downs) == 1 + 3  # `up` first, then three tries after the trial


@pytest.mark.real_executor
@pytest.mark.parametrize("model", [OPUS, CODEX])
def test_the_result_is_checked_against_the_schema_the_trial_launched_with(checkout, model):
    run_dir = make_run(checkout, [model])
    plan = load_plan(run_dir)
    settings = plan.configurations[0].settings
    trial = EngineTrial(settings, plan.engines["plain"], checkout / "engines", 7)
    launch = ADAPTERS["plain"].launch(trial)
    # The schema file gains a severity after the launch was built from it.
    schema = checkout / "engines" / "plain" / plain.SCHEMA_FILE
    schema.write_text(schema.read_text().replace('"nit"]', '"nit", "blocker"]'))
    answer = {"findings": [FINDING | {"severity": "blocker"}]}
    if launch.cli == "claude":
        stdout = {"type": "result", "subtype": "success", "is_error": False}
        out = Output(0, json.dumps(stdout | {"structured_output": answer}), None, [], Path("t"))
    else:
        out = Output(0, "", json.dumps(answer), [], Path("t"))

    with pytest.raises(EngineFailure, match="^invalid output: "):
        ADAPTERS["plain"].result(trial, launch, out)


@pytest.mark.real_executor
def test_output_that_is_not_valid_unicode_fails_the_trial_without_stopping_the_run(sandbox):
    def replies(launch):
        trial = claude_trial(launch)
        trial["stdout"] = trial["stdout"].replace("Off by one.", "Off by \\ud800 one.")
        return docker_replies(trial)

    trial_dir, counts, _ = sandbox([OPUS], replies)

    assert counts == {"failed": 1}
    assert read_status(trial_dir).reason == "invalid output: a string in it is not valid Unicode"


@pytest.mark.real_executor
def test_a_failing_count_objects_does_not_quote_the_agent(sandbox):
    def replies(launch):
        failing = {"match": "count-objects", "exit": 1, "stderr": f"fatal: {MARKER}\n"}
        return docker_replies(claude_trial(launch), extra=[failing])

    trial_dir, counts, _ = sandbox([OPUS], replies)

    assert counts == {"contaminated": 1}
    reason = read_status(trial_dir).reason
    assert "git -C /rbench/repo count-objects exited 1" in reason
    assert MARKER not in reason and MARKER not in (trial_dir / "audit.json").read_text()


@pytest.mark.real_executor
def test_malformed_usage_does_not_quote_the_transcript(sandbox):
    def replies(launch):
        trial = claude_trial(launch)
        again = message(SESSION, "msg-1", f"claude-{MARKER}", 3, 2)  # same id, another model
        trial["copy"][f"agent:{TRANSCRIPT}"] += json.dumps(again) + "\n"
        return docker_replies(trial)

    trial_dir, counts, _ = sandbox([OPUS], replies)

    assert counts == {"failed": 1}
    assert read_status(trial_dir).reason == "invalid output: the transcripts' usage is malformed"
    assert MARKER not in (trial_dir / "status.json").read_text()


@pytest.mark.real_executor
def test_a_transcript_that_cannot_be_copied_is_not_named(sandbox, checkout):
    path = f"/home/node/.claude/projects/-rbench-repo/{MARKER}.jsonl"

    def replies(launch):
        trial = claude_trial(launch)
        trial["diff"] += f"A {path}\n"
        failing = {"match": f"agent:{path}", "exit": 1, "stderr": f"no such file: {path}\n"}
        return docker_replies(trial, extra=[failing])

    with pytest.raises(executor.RunStopped, match="transcript 2 of 2") as stopped:
        sandbox([OPUS], replies)

    status = (first_trial(checkout) / "status.json").read_text()
    assert MARKER not in str(stopped.value) and MARKER not in status


def test_a_hidden_tasks_archive_is_found_in_the_hidden_directory(tmp_path):
    name = trial_module.archive_name("a" * 64)
    (tmp_path / "hidden").mkdir()
    (tmp_path / "hidden" / name).write_bytes(b"synthetic")
    hidden = HiddenConfig(tmp_path / "hidden", "synthetic-")
    ctx = types.SimpleNamespace(archives=tmp_path / "cache", hidden=hidden)
    public = types.SimpleNamespace(set="ours", archive_sha256="a" * 64)
    secret = types.SimpleNamespace(set="hidden", archive_sha256="a" * 64)

    assert trial_module._archive(ctx, public) == tmp_path / "cache" / name
    assert trial_module._archive(ctx, secret) == tmp_path / "hidden" / name
    with pytest.raises(ValueError, match="no hidden directory"):
        trial_module._archive(types.SimpleNamespace(archives=tmp_path, hidden=None), secret)


@pytest.mark.real_executor
def test_a_launch_that_cannot_start_is_retried_as_infrastructure(sandbox, fake_bin):
    # `timeout` could not run the engine: the wrapper's line says so, the shim log is empty.
    def replies(launch):
        trial = claude_trial(launch)
        trial["copy"]["agent:/var/log/rbench/shim.jsonl"] = ""
        engine = {"exit": 127, "stderr": launched(127, "start")}
        return docker_replies(trial, engine=engine)

    with pytest.raises(executor.RunStopped, match=r"could not start in the agent \(exit 127\)"):
        sandbox([OPUS], replies)

    assert len(docker_calls(fake_bin, ENGINE)) == 4  # the first try and 3 retries


STAND_IN_TIMEOUT = """#!/usr/bin/env python3
# GNU timeout's behaviour the wrapper relies on: with -v it reports a signal it sends, and a
# command it cannot run, on its own stderr; it keeps the descriptors it was given.
import subprocess, sys
args = sys.argv[1:]
assert args[:2] == ["-v", "-k"]
limit = float(args[3].rstrip("s"))
try:
    sys.exit(subprocess.run(args[4:], timeout=limit, close_fds=False).returncode)
except subprocess.TimeoutExpired:
    print(f"timeout: sending signal TERM to command {args[4]!r}", file=sys.stderr)
    sys.exit(124)
except OSError:
    print(f"timeout: failed to run command {args[4]!r}", file=sys.stderr)
    sys.exit(126)
"""
# A clock whose every read is a second later: a wrapper reading it would see each launch straddle
# a second boundary.
STAND_IN_DATE = """#!/bin/sh
n=$(cat "$0.n" 2>/dev/null || echo 1000); echo $((n + 1)) > "$0.n"; echo "$n"
"""


@pytest.mark.parametrize(
    ("command", "line"),
    [
        (["sh", "-c", "exit 124"], "exit 124"),  # the engine's own 124, even at a boundary
        (["sh", "-c", "exit 137"], "exit 137"),
        (["sh", "-c", "echo out; echo err >&2"], "exit 0"),
        (["sh", "-c", "sleep 5"], "timeout 124"),  # the timer fired
        (["no-such-engine"], "start 127"),
        (["./not-executable"], "start 127"),
    ],
)
def test_the_launch_wrapper_reports_what_ended_the_engine(tmp_path, command, line):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("timeout", STAND_IN_TIMEOUT), ("date", STAND_IN_DATE)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    (tmp_path / "not-executable").write_text("#!/bin/sh\n")
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    argv = ["sh", "-c", trial_module.RUN, "sh", str(tmp_path), "1s", "0.5s", *command]

    done = subprocess.run(argv, check=False, capture_output=True, text=True, env=env, timeout=30)

    last = done.stderr.rstrip("\n").rpartition("\n")[2]
    assert last == f"{trial_module.MARK} {line}"
    assert done.returncode == int(line.split()[1])
    if line == "exit 0":
        assert done.stdout == "out\n" and done.stderr.startswith("err\n")


@pytest.mark.real_executor
def test_a_codex_login_another_runs_agent_still_mounts_is_refused(sandbox, fake_bin):
    # That run's runner died (its flock is gone) or could not remove its project.
    other = "rbench-0123456789abcdef"

    def replies(launch):
        holder = {"match": f"label={trial_module.LOGIN_LABEL}=", "stdout": f"{other}\n"}
        return docker_replies(codex_trial(launch), extra=[holder])

    with pytest.raises(executor.RunStopped, match=f"mounted by the running agent of .*{other}"):
        sandbox([CODEX], replies)

    assert not docker_calls(fake_bin, "up --detach --wait")
    assert not docker_calls(fake_bin, f"--project-name {other}")  # its run removes it


@pytest.mark.real_executor
def test_an_agent_on_a_codex_login_is_labelled_with_it(sandbox):
    trial_dir, counts, _ = sandbox([CODEX], lambda launch: docker_replies(codex_trial(launch)))

    assert counts == {"done": 1}
    spec = json.loads((trial_dir / trial_module.PROJECT_DIR / "compose.json").read_text())
    login = Path(os.environ[executor.CODEX_LOGIN_ENV]).resolve()
    labels = spec["services"]["agent"]["labels"]
    assert labels == {trial_module.LOGIN_LABEL: trial_module.login_key(login)}


def test_a_malformed_thread_started_event_flags_usage_instead_of_raising(tmp_path):
    adapter = ADAPTERS["codex-review"]
    trial = engine_trial(test_codex_review.settings())
    launch = adapter.launch(trial)
    records = test_codex_review.records((test_codex_review.ROOT, test_codex_review.ROOT_META))
    test_codex_review.rollout(tmp_path, test_codex_review.ROOT, test_codex_review.review())
    events = [{"type": "thread.started", "thread_id": ["t1"]}, {"type": "turn.completed"}]
    (tmp_path / trial_module.STDOUT_FILE).write_text("".join(json.dumps(e) + "\n" for e in events))
    stdout = trial_module._codex_stdout(launch, records, tmp_path)
    assert stdout  # the engine is that one `codex exec --json` launch

    usage = trial_module.count_usage(records, tmp_path, stdout)

    assert any("thread.started without a thread id" in i for i in usage.incomplete)


@pytest.mark.real_executor
def test_a_transcript_directory_that_cannot_be_made_is_not_named(sandbox, checkout):
    # The second path makes the first, a file, a parent directory.
    first = f"/home/node/.claude/projects/-rbench-repo/{MARKER}.jsonl"
    second = f"{first}/subagents/a.jsonl"

    def replies(launch):
        trial = claude_trial(launch)
        trial["diff"] += f"A {first}\nA {second}\n"
        trial["copy"][f"agent:{first}"] = "{}\n"
        return docker_replies(trial)

    with pytest.raises(
        executor.RunStopped, match=r"transcript 3 of 3 out of the agent failed"
    ) as e:
        sandbox([OPUS], replies)

    status = (first_trial(checkout) / "status.json").read_text()
    assert MARKER not in str(e.value) and MARKER not in status


def test_a_stalled_compose_down_is_cut_off(fake_bin, tmp_path, monkeypatch):
    from rbench.trial import compose

    monkeypatch.setattr(compose, "DOWN_TIMEOUT", 0.3)
    fake_bin.scenario("docker", replies=[{"match": " down ", "delay": 10}])
    project = TrialProject.create(tmp_path, "rbench-t1", f"a@{PROXY_ID}", f"p@{PROXY_ID}")
    started = time.monotonic()

    with pytest.raises(TrialError, match=r"removing trial project rbench-t1: .* within 0\.3s"):
        project.down()

    assert time.monotonic() - started < 5
