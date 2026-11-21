"""Every registered engine through the trial executor (spec engine-configurations, Engine adapters,
Findings normalization; design D9, D13): one fake-docker trial of each engine's configuration ends
`done` with its findings, and a Markdown engine's report goes through the normalization step in a
project of its own. Every task, id and output is synthetic.
"""

import json
import shutil
import tomllib
from pathlib import Path

import pytest
import test_codex_review
import test_ponytail_review
import test_review_council
import test_rreview
from conftest import REPO
from test_executor import (
    CLAUDE_FLAGS,
    CONTAINER,
    COUNT_OBJECTS,
    ENGINE,
    OBJECTS,
    PROXY_ID,
    ROLLOUT,
    SESSION,
    TOKEN,
    TRANSCRIPT,
    docker_calls,
    launched,
    limit_line,
    shim_log,
)
from test_ledger import task_sets
from test_normalization import ANSWER, REPORT
from test_normalization import MODEL_USAGE as NORMALIZER_USAGE
from test_normalization import result as normalizer_result
from test_usage import GPT, codex_usage, message, meta, record, turn

import rbench.ledger.plan as plan_module
import rbench.run.trial as trial_module
from rbench.engines import normalization, ponytail_review, review_council
from rbench.ledger import (
    TRIALS_DIR,
    load_plan,
    read_results,
    read_status,
    read_wasted,
    start_run,
)
from rbench.ledger.plan import run_identity
from rbench.run import executor
from rbench.run.adapters import ADAPTERS, EngineFailure, EngineTrial, Output
from rbench.run.trial import normalizer_name, project_name
from rbench.trial.repository import RepositoryState

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"
DIGEST = "sha256:" + "e" * 64
MATRIX = """
stage = "screening"
tasks = "core"
attempts = 1

[[rounds]]
name = "r1"
budget = { claude = 1000, codex = 1000 }

[[rounds.entries]]
engine = "*"
default = true

[[rounds.entries]]
engine = "ponytail-review"
roles.reviewer = { model = ["gpt-6.2-sol"], effort = "high" }
"""
NORMALIZER = "agent claude -p --model " + normalization.MODEL
NORMALIZED = [{k: v for k, v in f.items() if v is not None} for f in ANSWER["findings"]]
NORMALIZER_COUNTS = {
    model: {"input": c["inputTokens"], "output": c["outputTokens"]}
    | {"cache_read": c["cacheReadInputTokens"], "cache_creation": c["cacheCreationInputTokens"]}
    for model, c in NORMALIZER_USAGE.items()
}
SHIM_LOG = "agent:/var/log/rbench/shim.jsonl"


@pytest.fixture
def run(tmp_path: Path, fake_bin, monkeypatch, tmp_path_factory):
    """A screening run of every declared engine's default, and ponytail-review on Codex, over the
    repository's engine declarations; returns (run dir, plan, context)."""
    monkeypatch.setattr(plan_module, "CHECKOUT_ROOT", tmp_path)
    shutil.copytree(REPO / "engines", tmp_path / "engines")
    task_sets(tmp_path / "tasks")
    (tmp_path / "matrices").mkdir()
    (tmp_path / "matrices" / "m.toml").write_text(MATRIX)
    images = {}
    for declaration in sorted((tmp_path / "engines").glob("*.toml")):
        data = tomllib.loads(declaration.read_text())
        images[data["name"]] = {"image": f"rbench/{data['name']}@{DIGEST}", "clis": data["cli"]}
    (tmp_path / "images.json").write_text(json.dumps(images))
    run_dir = start_run(
        tmp_path / "matrices" / "m.toml",
        "r1",
        images_path=tmp_path / "images.json",
        engines_dir=tmp_path / "engines",
        tasks_root=tmp_path / "tasks",
        runs_dir=tmp_path / "runs",
        run_id="r",
    )
    fake_bin.scenario("security", stdout=TOKEN + "\n")
    login = tmp_path_factory.mktemp("codex-login")
    (login / "auth.json").write_text("{}")
    monkeypatch.setenv(executor.CODEX_LOGIN_ENV, str(login))
    state = RepositoryState("a" * 40, "b" * 40, OBJECTS)
    monkeypatch.setattr(trial_module, "prepare_repository", lambda *args: state)
    plan = load_plan(run_dir)
    fake_bin.scenario("docker", replies=[{"match": "build --quiet", "stdout": PROXY_ID + "\n"}])
    ctx = executor.prepare_run(
        run_dir,
        plan,
        engines_dir=tmp_path / "engines",
        tasks_root=tmp_path / "tasks",
        archives=tmp_path / "archives",
    )
    return run_dir, plan, ctx


def first_trial(plan, engine: str, model: str | None = None):
    """The first trial of `engine`'s configuration (on `model`, when given)."""
    for trial in plan.trials:
        c = next(c for c in plan.configurations if c.id == trial.configuration)
        if c.settings["engine"] == engine and (model is None or model in json.dumps(c.settings)):
            return trial, c.settings
    raise AssertionError(f"no trial of {engine}")


def claude_session(argv: list[str], added: list[str] = CLAUDE_FLAGS) -> dict:
    """A Claude session of `argv` with one counted message, as the agent holds it after."""
    log = shim_log(
        {"kind": "launch", "launch": 1, "cli": "claude", "argv": argv}
        | {"final_argv": [*added, *argv], "added": list(added), "rewrites": []},
        {"kind": "child", "launch": 1, "pid": 101, "start": 1},
        {"kind": "hook", "session_id": SESSION, "source": "startup"}
        | {"claude_pid": 101, "claude_start": 1},
        {"kind": "exit", "launch": 1, "status": 0},
    )
    transcript = json.dumps(message(SESSION, "msg-1", OPUS, 3, 2)) + "\n"
    return {"diff": f"A {TRANSCRIPT}\n", "copy": {SHIM_LOG: log, f"agent:{TRANSCRIPT}": transcript}}


def codex_session(argv: list[str], *extra_rollout: dict, launch_record: dict | None = None) -> dict:
    """A Codex session of `argv` bound to one rollout with usage, as the agent holds it after."""
    launch = launch_record or {
        "kind": "launch",
        "cli": "codex",
        "argv": argv,
        "final_argv": argv,
        "added": ["--strict-config"],
        "rewrites": [],
    }
    log = shim_log(
        launch | {"launch": 1},
        {"kind": "child", "launch": 1, "pid": 101, "start": 1},
        {"kind": "binding", "launch": 1, "path": ROLLOUT, "meta": {"id": "t1", "source": "exec"}},
        {"kind": "exit", "launch": 1, "status": 0},
    )
    rollout = [meta("t1"), turn("u1", GPT), record("t1", "u1", "r1", codex_usage(10, 4, 3))]
    rollout += extra_rollout
    text = "".join(json.dumps(line) + "\n" for line in rollout)
    return {"diff": f"A {ROLLOUT}\n", "copy": {SHIM_LOG: log, f"agent:{ROLLOUT}": text}}


def replies(
    session: dict,
    stdout: str,
    *extra: dict,
    answer: tuple[str, str] | None = None,
    status: int = 0,
) -> list[dict]:
    copy = session["copy"] | {"proxy:/var/log/squid/access.log": ""}
    if answer is not None:
        copy[f"agent:{answer[0]}"] = answer[1]
    return [
        *extra,
        {"match": ENGINE, "stdout": stdout, "stderr": launched(status)},
        {"match": NORMALIZER, "stdout": json.dumps(normalizer_result())},
        {"match": "count-objects", "stdout": COUNT_OBJECTS},
        {"match": "ps --quiet agent", "stdout": CONTAINER + "\n"},
        {"match": f"diff {CONTAINER}", "stdout": session["diff"]},
        {"match": " cp ", "copy": copy},
    ]


def engine_trial(settings: dict, ctx, engine: str) -> EngineTrial:
    return EngineTrial(settings, ctx.plan.engines[engine], ctx.engines_dir, 7, "trial t-1")


def code_review_case(settings, ctx):
    launch = ADAPTERS["code-review"].launch(engine_trial(settings, ctx, "code-review"))
    stdout = test_ponytail_review.claude_result(result=REPORT)
    return replies(claude_session(list(launch.argv[1:])), stdout), NORMALIZED


def plugin_claude_case(settings, ctx):
    launch = ADAPTERS["ponytail-review"].launch(engine_trial(settings, ctx, "ponytail-review"))
    stdout = test_ponytail_review.claude_result(result=REPORT)
    return replies(claude_session(list(launch.argv[1:])), stdout), NORMALIZED


def plugin_codex_case(settings, ctx):
    launch = ADAPTERS["ponytail-review"].launch(engine_trial(settings, ctx, "ponytail-review"))
    stdout = test_ponytail_review.codex_stdout(
        *test_ponytail_review.STARTED,
        test_ponytail_review.message(REPORT),
        test_ponytail_review.COMPLETED,
    )
    session = codex_session(list(launch.argv[1:]))
    assert launch.output is not None
    return replies(session, stdout, answer=(launch.output, REPORT)), NORMALIZED


def review_council_case(settings, ctx):
    launch = ADAPTERS["review-council"].launch(engine_trial(settings, ctx, "review-council"))
    claude = review_council.requested(settings)["claude"]
    added = [*CLAUDE_FLAGS, "--model", claude.model, "--effort", claude.effort]
    stdout = test_review_council.events(
        test_review_council.said(test_review_council.REPORT),
        test_review_council.SUCCESS | {"result": "Done."},
    )
    # The overlay's paths are free in the checkout: `check_checkout` finds no file in the way.
    kinds = {"match": "echo link", "stdout": "none\n" * (1 + len(review_council.STATE_PATHS))}
    return replies(claude_session(list(launch.argv[1:]), added), stdout, kinds), NORMALIZED


def rreview_case(settings, ctx):
    launch = ADAPTERS["rreview"].launch(engine_trial(settings, ctx, "rreview"))
    stdout = json.dumps(test_rreview.result([test_rreview.FINDING]))
    found = ADAPTERS["rreview"].result(
        engine_trial(settings, ctx, "rreview"),
        launch,
        Output(1, stdout, None, [], Path("none")),
    )
    # rreview exits 1 when its gate kept findings.
    return replies(claude_session(list(launch.argv[1:])), stdout, status=1), found


def codex_review_case(settings, ctx):
    review = test_codex_review.review(test_codex_review.finding())
    item = {"type": "ExitedReviewMode", "id": "r", "review_output": review}
    done = {"type": "event_msg", "payload": {"type": "item_completed", "item": item}}
    launch = ADAPTERS["codex-review"].launch(engine_trial(settings, ctx, "codex-review"))
    # The launch as the real shim logs it.
    launch_record = test_codex_review.launch_record(list(launch.argv[1:]))
    session = codex_session([], done, launch_record=launch_record)
    return replies(session, ""), [test_codex_review.TWO_FINDINGS[0]]


CASES = {
    ("code-review", None): code_review_case,
    ("ponytail-review", SONNET): plugin_claude_case,
    ("ponytail-review", "gpt-6.2-sol"): plugin_codex_case,
    ("review-council", None): review_council_case,
    ("rreview", None): rreview_case,
    ("codex-review", None): codex_review_case,
}


@pytest.mark.real_executor
@pytest.mark.parametrize(("engine", "model"), list(CASES), ids=lambda v: v or "default")
def test_a_trial_of_each_registered_engine_ends_done_with_its_findings(
    run, fake_bin, engine, model
):
    run_dir, plan, ctx = run
    trial, settings = first_trial(plan, engine, model)
    answers, expected = CASES[engine, model](settings, ctx)
    fake_bin.scenario("docker", replies=answers)

    counts = executor.run_trials(ctx, [trial], log=lambda _: None)

    trial_dir = run_dir / TRIALS_DIR / trial.id
    assert counts == {"done": 1}, read_status(trial_dir).reason
    findings, usage = read_results(trial_dir)
    assert findings == expected
    assert usage["engine"]  # counted from the engine's own collection
    identity = run_identity(run_dir)
    ups = [c[c.index("--project-name") + 1] for c in docker_calls(fake_bin, "up --detach")]
    record = trial_dir / "raw" / normalization.RECORD_FILE
    if plan.engines[engine].output == "markdown":
        # The normalizer ran in its own project after the engine's, and both are removed.
        assert ups == [project_name(identity, trial.id), normalizer_name(identity, trial.id)]
        (step,) = docker_calls(fake_bin, NORMALIZER)
        assert step[step.index("--project-name") + 1] == normalizer_name(identity, trial.id)
        assert usage["normalization"] == NORMALIZER_COUNTS
        assert usage["incomplete"]["normalization"] == []
        assert json.loads(record.read_text())["normalizer"] == normalization.SETTINGS
        for evidence in ("shim.jsonl", "proxy.log", "findings.json"):
            assert (trial_dir / "raw" / "normalizer" / evidence).is_file()
    else:
        assert ups == [project_name(identity, trial.id)]
        assert usage["normalization"] == {} and not record.exists()
    downs = [c[c.index("--project-name") + 1] for c in docker_calls(fake_bin, " down ")]
    assert set(downs) == set(ups)


@pytest.mark.real_executor
def test_the_plugin_review_on_codex_installs_its_plugin_before_the_launch(run, fake_bin):
    _, plan, ctx = run
    trial, settings = first_trial(plan, "ponytail-review", "gpt-6.2-sol")
    answers, _ = plugin_codex_case(settings, ctx)
    fake_bin.scenario("docker", replies=answers)

    executor.run_trials(ctx, [trial], log=lambda _: None)

    calls = [" ".join(c["argv"]) for c in fake_bin.calls() if c["tool"] == "docker"]
    setup = [" ".join(argv) for argv in ponytail_review.CODEX_SETUP]
    at = [next(i for i, c in enumerate(calls) if c.endswith(f"agent {s}")) for s in setup]
    assert at == sorted(at) and at[-1] < next(i for i, c in enumerate(calls) if ENGINE in c)


@pytest.mark.real_executor
def test_a_failing_setup_command_is_an_infrastructure_error(run, fake_bin):
    _, plan, ctx = run
    trial, settings = first_trial(plan, "ponytail-review", "gpt-6.2-sol")
    answers, _ = plugin_codex_case(settings, ctx)
    failing = {"match": "codex plugin add", "exit": 1, "stderr": "engine text\n"}
    fake_bin.scenario("docker", replies=[failing, *answers])

    with pytest.raises(executor.RunStopped, match=r"setup command 2 \(codex\) exited 1") as e:
        executor.run_trials(ctx, [trial], log=lambda _: None)
    assert "engine text" not in str(e.value)
    assert not docker_calls(fake_bin, ENGINE)


@pytest.mark.real_executor
@pytest.mark.parametrize(
    ("hook", "reply", "reason"),
    [
        (
            "check_checkout",
            {"match": "echo link", "stdout": "link\nnone\nnone\nnone\n"},
            "the checkout's .review-council is a symlink",
        ),
        (
            "prepare_checkout",
            {"match": "status --porcelain", "stdout": "?? .review-council/learnings.md\0"},
            "git status shows the review-council overlay's .review-council/learnings.md",
        ),
    ],
)
def test_a_checkout_hook_reason_makes_the_trial_misconfigured_before_the_engine(
    run, fake_bin, hook, reply, reason
):
    run_dir, plan, ctx = run
    trial, settings = first_trial(plan, "review-council")
    answers, _ = review_council_case(settings, ctx)
    fake_bin.scenario("docker", replies=[reply, *answers])

    assert executor.run_trials(ctx, [trial], log=lambda _: None) == {"misconfigured": 1}

    assert reason in read_status(run_dir / TRIALS_DIR / trial.id).reason
    assert not docker_calls(fake_bin, ENGINE)
    writes = docker_calls(fake_bin, trial_module.WRITE)
    if hook == "check_checkout":
        assert writes == []  # refused before any of Launch.files is written
    else:
        assert len(writes) == len(review_council.launch(ctx.engines_dir, settings).files)
    assert docker_calls(fake_bin, " down ")  # the project is still removed


@pytest.mark.real_executor
def test_adapters_without_checkout_hooks_write_their_files_without_them(run, fake_bin):
    _, plan, ctx = run
    trial, settings = first_trial(plan, "code-review")
    answers, _ = code_review_case(settings, ctx)
    fake_bin.scenario("docker", replies=answers)

    executor.run_trials(ctx, [trial], log=lambda _: None)

    assert not hasattr(ADAPTERS["code-review"], "check_checkout")
    assert not docker_calls(fake_bin, "echo link")
    assert not docker_calls(fake_bin, "skip-worktree")


@pytest.mark.real_executor
def test_a_failed_normalization_fails_the_trial_without_quoting_the_report(run, fake_bin):
    run_dir, plan, ctx = run
    trial, settings = first_trial(plan, "code-review")
    answers, _ = code_review_case(settings, ctx)
    failing = {"match": NORMALIZER, "stdout": REPORT, "exit": 1, "stderr": REPORT}
    fake_bin.scenario("docker", replies=[failing, *answers])

    assert executor.run_trials(ctx, [trial], log=lambda _: None) == {"failed": 1}

    reason = read_status(run_dir / TRIALS_DIR / trial.id).reason
    assert reason == f"normalization failed: trial {trial.id}: normalization: claude exited 1"
    assert not (run_dir / TRIALS_DIR / trial.id / "findings.json").exists()
    identity = run_identity(run_dir)
    downs = [c[c.index("--project-name") + 1] for c in docker_calls(fake_bin, " down ")]
    assert normalizer_name(identity, trial.id) in downs


@pytest.mark.real_executor
def test_a_blank_report_has_no_findings_and_starts_no_normalizer(run, fake_bin):
    run_dir, plan, ctx = run
    trial, settings = first_trial(plan, "code-review")
    answers, _ = code_review_case(settings, ctx)
    blank = test_ponytail_review.claude_result(result="  \n")
    answers = [a | {"stdout": blank} if a["match"] == ENGINE else a for a in answers]
    fake_bin.scenario("docker", replies=answers)

    assert executor.run_trials(ctx, [trial], log=lambda _: None) == {"done": 1}

    findings, usage = read_results(run_dir / TRIALS_DIR / trial.id)
    assert findings == [] and usage["normalization"] == {}
    assert len(docker_calls(fake_bin, "up --detach")) == 1


@pytest.mark.real_executor
def test_startup_removes_a_normalizer_project_a_crash_left(run, fake_bin):
    run_dir, plan, _ = run
    left = normalizer_name(run_identity(run_dir), plan.trials[0].id)
    listing = {"match": "--filter label=com.docker.compose.project", "stdout": f"{left}\n"}
    fake_bin.scenario("docker", replies=[listing])

    assert executor.remove_leftovers(run_dir, plan, f"rbench-proxy@{PROXY_ID}") == [left]
    (down,) = docker_calls(fake_bin, " down ")
    assert down[down.index("--project-name") + 1] == left
    assert "/normalizer/" in " ".join(down)


@pytest.mark.real_executor
@pytest.mark.parametrize(
    ("engine", "model"), [("code-review", None), ("review-council", None),
                          ("ponytail-review", SONNET), ("ponytail-review", "gpt-6.2-sol")],
)  # fmt: skip
def test_a_markdown_adapters_failures_name_no_engine_output(run, engine, model):
    _, plan, ctx = run
    _, settings = first_trial(plan, engine, model)
    adapter, trial = ADAPTERS[engine], engine_trial(settings, ctx, engine)
    launch = adapter.launch(trial)

    with pytest.raises(EngineFailure, match=rf"^engine error: {launch.cli} exited 2$"):
        adapter.report(trial, launch, Output(2, REPORT, REPORT, [], Path("none")))
    # Named by the executor's label for the trial, never by its payload.
    with pytest.raises(EngineFailure, match=rf"^invalid output: trial t-1: {engine}: ") as e:
        adapter.report(trial, launch, Output(0, f"not json {REPORT}", None, [], Path("none")))
    assert "Off by one" not in e.value.reason


def with_normalizer(answers: list[dict], **reply) -> list[dict]:
    """`answers` with the normalizer's reply changed."""
    return [a | reply if a["match"] == NORMALIZER else a for a in answers]


@pytest.mark.real_executor
def test_a_canary_the_normalizer_copies_into_a_finding_contaminates_the_trial(run, fake_bin):
    run_dir, plan, ctx = run
    trial, settings = first_trial(plan, "code-review")
    answers, _ = code_review_case(settings, ctx)
    canary = ctx.tasks[trial.task].canary
    first = ANSWER["findings"][0] | {"message": f"Off by one, see {canary}"}
    leaked = ANSWER | {"findings": [first, *ANSWER["findings"][1:]]}
    stdout = json.dumps(normalizer_result(leaked))
    fake_bin.scenario("docker", replies=with_normalizer(answers, stdout=stdout))

    assert executor.run_trials(ctx, [trial], log=lambda _: None) == {"contaminated": 1}

    trial_dir = run_dir / TRIALS_DIR / trial.id
    assert "canary" in read_status(trial_dir).reason
    assert json.loads((trial_dir / "audit.json").read_text())["contaminated"]
    assert not (trial_dir / "findings.json").exists()  # excluded from scores


@pytest.mark.real_executor
def test_a_normalizer_past_its_limit_fails_the_trial_without_rerunning_the_engine(
    run, fake_bin, monkeypatch
):
    run_dir, plan, ctx = run
    trial, settings = first_trial(plan, "code-review")
    answers, _ = code_review_case(settings, ctx)
    monkeypatch.setattr(trial_module, "NORMALIZE_TIMEOUT", 0.5)
    fake_bin.scenario("docker", replies=with_normalizer(answers, delay=10))

    assert executor.run_trials(ctx, [trial], log=lambda _: None) == {"failed": 1}

    trial_dir = run_dir / TRIALS_DIR / trial.id
    reason = read_status(trial_dir).reason
    assert reason.startswith("normalization failed: ") and "did not return within 0.5s" in reason
    assert len(docker_calls(fake_bin, ENGINE)) == 1
    assert not (trial_dir / "findings.json").exists()


@pytest.mark.real_executor
def test_a_failed_normalizer_stays_failed_when_its_evidence_and_teardown_fail(
    run, fake_bin, monkeypatch
):
    run_dir, plan, ctx = run
    trial, settings = first_trial(plan, "code-review")
    answers, _ = code_review_case(settings, ctx)
    monkeypatch.setattr(trial_module, "NORMALIZE_TIMEOUT", 0.5)
    step = f"/{trial_module.NORMALIZER_DIR}/compose.json"
    broken = [{"match": f"{step} {verb}", "exit": 1} for verb in ("cp", "down")]
    fake_bin.scenario("docker", replies=[*broken, *with_normalizer(answers, delay=10)])

    with pytest.raises(executor.RunStopped, match="failed, then"):
        executor.run_trials(ctx, [trial], log=lambda _: None)

    status = read_status(run_dir / TRIALS_DIR / trial.id)
    assert status.state == "failed" and status.reason.startswith("normalization failed: ")
    assert len(docker_calls(fake_bin, ENGINE)) == 1


@pytest.mark.real_executor
def test_a_report_that_is_not_utf8_fails_only_its_trial(run, fake_bin):
    run_dir, plan, ctx = run
    trial, settings = first_trial(plan, "ponytail-review", SONNET)
    answers, _ = plugin_claude_case(settings, ctx)
    stdout = test_ponytail_review.claude_result(result="Broken \ud800 here")
    answers = [a | {"stdout": stdout} if a["match"] == ENGINE else a for a in answers]
    fake_bin.scenario("docker", replies=answers)

    assert executor.run_trials(ctx, [trial], log=lambda _: None) == {"failed": 1}

    reason = read_status(run_dir / TRIALS_DIR / trial.id).reason
    assert reason == "invalid output: the report is not valid UTF-8"
    assert not docker_calls(fake_bin, NORMALIZER)


@pytest.mark.real_executor
def test_a_normalizer_count_past_the_ledgers_bound_fails_without_quoting_its_model(run, fake_bin):
    run_dir, plan, ctx = run
    trial, settings = first_trial(plan, "code-review")
    answers, _ = code_review_case(settings, ctx)
    key = "zz-side-model-" + "x" * 300
    huge = dict.fromkeys(next(iter(NORMALIZER_USAGE.values())), 2**53)
    stdout = json.dumps(normalizer_result(modelUsage=NORMALIZER_USAGE | {key: huge}))
    fake_bin.scenario("docker", replies=with_normalizer(answers, stdout=stdout))

    assert executor.run_trials(ctx, [trial], log=lambda _: None) == {"failed": 1}

    reason = read_status(run_dir / TRIALS_DIR / trial.id).reason
    assert "modelUsage entry 1: bad " in reason and "side-model" not in reason


def normalizer_session(*transcript: str) -> dict:
    """The normalizer's evidence: its one Claude session, bound to `transcript`'s lines."""
    log = shim_log(
        {"kind": "launch", "launch": 1, "cli": "claude", "argv": normalization.argv()[1:]}
        | {"final_argv": [*CLAUDE_FLAGS, *normalization.argv()[1:]]}
        | {"added": list(CLAUDE_FLAGS), "rewrites": []},
        {"kind": "child", "launch": 1, "pid": 101, "start": 1},
        {"kind": "hook", "session_id": SESSION, "source": "startup"}
        | {"claude_pid": 101, "claude_start": 1, "transcript_path": TRANSCRIPT},
        {"kind": "exit", "launch": 1, "status": 1},
    )
    copy = {SHIM_LOG: log, f"agent:{TRANSCRIPT}": "".join(transcript)}
    copy["proxy:/var/log/squid/access.log"] = ""
    step = f"/{trial_module.NORMALIZER_DIR}/compose.json cp"
    return {"match": step, "copy": copy}


@pytest.mark.real_executor
def test_a_limit_the_normalizer_reports_interrupts_the_trial_and_stops_the_run(run, fake_bin):
    run_dir, plan, ctx = run
    trial, settings = first_trial(plan, "code-review")
    answers, _ = code_review_case(settings, ctx)
    used = json.dumps(message(SESSION, "msg-n", SONNET, 7, 1)) + "\n"
    hit = limit_line("claude/transcript-usage-limit-five-hour.stub.jsonl", sessionId=SESSION)
    error = json.dumps({"type": "result", "subtype": "success", "is_error": True})
    answers = with_normalizer(answers, stdout=error, exit=1)
    fake_bin.scenario("docker", replies=[normalizer_session(used, hit), *answers])

    # LimitReached: the run exits 75 with its resume command (test_executor).
    with pytest.raises(executor.LimitReached, match="normalization: Claude reported its usage"):
        executor.run_trials(ctx, [trial], log=lambda _: None)

    assert ctx.limit.is_set()
    trial_dir = run_dir / TRIALS_DIR / trial.id
    status = read_status(trial_dir)
    assert (status.state, status.reason) == (
        "interrupted",
        "normalization: Claude reported its usage limit",
    )
    (wasted,) = read_wasted(trial_dir)
    assert wasted["engine"] == {
        OPUS: {"input": 3, "output": 2, "cache_read": 0, "cache_creation": 0}
    }
    assert wasted["normalization"] == {
        SONNET: {"input": 7, "output": 1, "cache_read": 0, "cache_creation": 0}
    }
    assert not (trial_dir / "findings.json").exists()
    assert len(docker_calls(fake_bin, ENGINE)) == 1
    identity = run_identity(run_dir)
    downs = {c[c.index("--project-name") + 1] for c in docker_calls(fake_bin, " down ")}
    assert downs == {project_name(identity, trial.id), normalizer_name(identity, trial.id)}


@pytest.mark.real_executor
def test_a_rate_limit_the_normalizer_retried_past_does_not_interrupt(run, fake_bin):
    run_dir, plan, ctx = run
    trial, settings = first_trial(plan, "code-review")
    answers, _ = code_review_case(settings, ctx)
    # A retried API error is a system record; only an assistant record ending the turn is a limit.
    retried = {"type": "system", "subtype": "api_error", "level": "error", "sessionId": SESSION}
    retried |= {"error": {"status": 429}, "retryAttempt": 1, "maxRetries": 10}
    used = json.dumps(message(SESSION, "msg-n", SONNET, 7, 1)) + "\n"
    fake_bin.scenario(
        "docker", replies=[normalizer_session(json.dumps(retried) + "\n", used), *answers]
    )

    assert executor.run_trials(ctx, [trial], log=lambda _: None) == {"done": 1}
    assert not ctx.limit.is_set()
    assert read_wasted(run_dir / TRIALS_DIR / trial.id) == []
