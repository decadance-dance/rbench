"""The trial executor (spec run-ledger: Balanced queue order, Failures and retries, Concurrency per
provider; spec trial-isolation, One fresh sandbox per trial; design D3, D7).

The queue and its slots are tested with a stand-in attempt; whole trials run through fake `docker`
and `security` (tests/fixtures/bin), whose replies stand in for the agent container: the engine's
stdout, the shim log, the transcripts and the proxy log. Every task, id and output is synthetic.
"""

import dataclasses
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import test_codex_review
import test_rreview
from test_ledger import FINDING, cli_inputs
from test_usage import GPT, codex_usage, message, meta, record, turn

import rbench.ledger.plan as plan_module
import rbench.run.trial as trial_module
from rbench.cli import ExitCode, main
from rbench.engines import codex_review, plain, rreview
from rbench.ledger import (
    TRIALS_DIR,
    Plan,
    load_plan,
    read_results,
    read_status,
    set_state,
    start_run,
)
from rbench.ledger.plan import PlannedTrial
from rbench.ledger.resume import recover_trials
from rbench.run import executor
from rbench.run.adapters import ADAPTERS, EngineFailure, EngineTrial, NotEligible, Output, Plain
from rbench.run.trial import project_name
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
ENGINE = "agent env -C /rbench/repo timeout"
CLAUDE_FLAGS = POLICY["claude"]["flags"]


def make_run(root: Path, models: list[str]) -> Path:
    """A run of `plain` on `models` over the core tasks, created as `rbench run` creates it."""
    argv = cli_inputs(root)
    matrix = Path(argv[1])
    text = matrix.read_text().replace(f'["{OPUS}", "{SONNET}"]', json.dumps(models))
    matrix.write_text(text.replace("{ claude = 1000 }", "{ claude = 1000, codex = 1000 }"))
    return start_run(
        matrix,
        "r1",
        images_path=root / "images.json",
        engines_dir=root / "engines",
        tasks_root=root / "tasks",
        runs_dir=root / "runs",
        run_id="r",
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


@pytest.mark.real_executor
def test_a_run_stopped_at_40_percent_keeps_each_provider_group_within_one_attempt(checkout):
    run_dir = make_run(checkout, [OPUS, SONNET, "gpt-6.2-sol"])
    plan = with_attempts(run_dir, 3)
    stop_at = plan.trials[int(len(plan.trials) * 0.4)]
    started: list[str] = []
    ran = Attempts(run_dir, seconds=0.01)

    def attempt(trial):
        started.append(trial.id)
        if trial == stop_at:
            raise TrialError("docker went away")
        return ran(trial)

    with pytest.raises(executor.RunStopped, match="after 3 retries"):
        executor.execute(run_dir, plan, plan.trials, attempt, log=lambda _: None)

    assert started.count(stop_at.id) == 4  # the first try and 3 retries
    assert read_status(run_dir / TRIALS_DIR / stop_at.id).state == "interrupted"
    for group in ({"claude"}, {"codex"}):
        trials = [t for t in plan.trials if executor.slots(plan, t) == group]
        ran_ = [t for t in trials if t.id in started]
        assert ran_ == trials[: len(ran_)]  # a prefix in plan order
        attempts_done = {}
        for c in {t.configuration for t in trials}:
            done = [t for t in ran_ if t.configuration == c]
            per_attempt = len(plan.tasks)
            attempts_done[c] = len(done) / per_attempt
        assert max(attempts_done.values()) - min(attempts_done.values()) <= 1
    assert len(started) < len(plan.trials) + 3  # nothing new started after the stop


@pytest.mark.real_executor
def test_an_infrastructure_error_is_retried_and_an_error_after_the_final_state_is_not(checkout):
    run_dir = make_run(checkout, [OPUS])
    plan = load_plan(run_dir)
    first, second = plan.trials[:2]
    tries = {first.id: 0, second.id: 0}
    lines: list[str] = []

    def attempt(trial):
        tries[trial.id] += 1
        trial_dir = run_dir / TRIALS_DIR / trial.id
        set_state(trial_dir, "running")
        if trial == first and tries[trial.id] <= 2:
            raise TrialError("proxy did not become healthy")
        if trial == second:
            set_state(trial_dir, "failed", "timeout")
            raise TrialError("removing the project failed")
        set_state(trial_dir, "failed", "engine error")
        return "failed"

    counts = executor.execute(run_dir, plan, [first, second], attempt, log=lines.append)

    assert tries == {first.id: 3, second.id: 1}
    assert counts == {"failed": 2}
    assert (
        f"trial {first.id}: infrastructure error: proxy did not become healthy; retry 1 of 3"
        in lines
    )
    assert any("retry 2 of 3" in line for line in lines)
    assert read_status(run_dir / TRIALS_DIR / second.id).reason == "timeout"


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


def test_project_names_are_unique_per_run_and_trial_and_hide_the_task(tmp_path):
    a = project_name(tmp_path / "runs" / "a", "o-1--abc--1")

    assert a == project_name(tmp_path / "runs" / "x" / ".." / "a", "o-1--abc--1")
    assert a != project_name(tmp_path / "runs" / "b", "o-1--abc--1")
    assert a != project_name(tmp_path / "runs" / "a", "o-1--abc--2")
    assert a.startswith("rbench-") and "o-1" not in a


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
        | {"claude_pid": 101, "claude_start": 1},
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


def docker_replies(trial: dict, *, engine: dict | None = None, extra: list | None = None) -> dict:
    copy = trial["copy"] | {"proxy:/var/log/squid/access.log": ""}
    return {
        "replies": [
            *(extra or []),
            {"match": "build --quiet --tag rbench-proxy", "stdout": PROXY_ID + "\n"},
            {"match": ENGINE, "stdout": trial["stdout"]} | (engine or {}),
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
    name = project_name(trial_dir.parents[1], trial_dir.name)
    ups = docker_calls(fake_bin, "up --detach --wait")
    assert len(ups) == 1 and ups[0][ups[0].index("--project-name") + 1] == name
    (engine,) = docker_calls(fake_bin, ENGINE)
    assert engine[engine.index("timeout") + 1 : engine.index("timeout") + 4] == [
        "-k",
        "30s",
        "3600s",
    ]
    assert engine[engine.index("timeout") + 4 :] == [
        "claude",
        *engine[engine.index("claude") + 1 :],
    ]
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
def test_a_timeout_fails_the_trial_and_resume_does_not_rerun_it(sandbox):
    def replies(launch):
        return docker_replies(claude_trial(launch), engine={"delay": 1.2, "exit": 124})

    trial_dir, counts, plan = sandbox([OPUS], replies, timeout=1.0)

    assert counts == {"failed": 1}
    assert read_status(trial_dir).reason == "timeout"
    assert trial_dir.name not in [t.id for t in recover_trials(trial_dir.parents[1], plan)]


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
    mine = project_name(run_dir, plan.trials[1].id)
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
