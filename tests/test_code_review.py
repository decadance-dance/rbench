"""The `code-review` engine (spec engine-configurations, Engine adapters; design D13), with a fake
`claude` for the session and a fake docker for the normalization step."""

import json
import subprocess
import tomllib
from pathlib import Path

import pytest
from conftest import REPO
from test_normalization import ANSWER, REPORT, claude_answers, project, result
from test_shims import CLAUDE_FLAGS, shim

from rbench.engines.code_review import (
    ARGV,
    CodeReviewOutputError,
    bind,
    findings,
    launch,
    report,
    requested,
)
from rbench.engines.declaration import DeclarationError, Setting, load_engine, parse_engine
from rbench.matrix.expansion import parse_matrix
from rbench.trial.hermetic import shim_settings

DECLARATION = REPO / "engines" / "code-review.toml"
WHERE = "trial t-1: code-review"
MODEL_USAGE = {
    "claude-opus-5-5": {
        "inputTokens": 9000,
        "outputTokens": 1500,
        "cacheReadInputTokens": 40000,
        "cacheCreationInputTokens": 3000,
    }
}
# What only the engine wrote: an error must never quote it.
SECRET = "text only the engine wrote"


def session_result(**fields: object) -> str:
    """The `claude -p --output-format json` result of a `/code-review` session."""
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": REPORT,
        "modelUsage": MODEL_USAGE,
        "session_id": "00000000-0000-4000-8000-000000000002",
    }
    return json.dumps(base | fields)


def settings_of(model: str, effort: str) -> dict:
    """The frozen settings of a `code-review` configuration of the repository's declaration."""
    engine = load_engine(DECLARATION)
    entry = {"engine": "code-review", "roles": {"reviewer": {"model": model, "effort": effort}}}
    data = {"stage": "screening", "tasks": "core", "attempts": 1}
    data["rounds"] = [{"name": "r1", "budget": {"claude": 1}, "entries": [entry]}]
    (r,) = parse_matrix(data, "m.toml", {"code-review": engine}).rounds
    (configuration,) = r.configurations
    return configuration.settings


def test_declaration_pins_the_claude_code_version_and_markdown_output():
    engine = load_engine(DECLARATION)
    assert engine.version == engine.clis["claude"] == "2.1.316"
    assert engine.output == "markdown"
    assert engine.default_configuration() == {"reviewer": Setting("claude-sonnet-5-5", "high")}
    # Its settings carry the Claude Code version and the normalizer's pin, so either gives new ids.
    settings = settings_of("claude-opus-5-5", "xhigh")
    assert settings["version"] == "2.1.316" and "normalization" in settings


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda d: d | {"pin": {"kind": "version", "ref": "2.1.316"}}, "kind 'cli', ref 'claude'"),
        (lambda d: d | {"output": "structured"}, "output must be 'markdown'"),
        (lambda d: d["roles"]["reviewer"].pop("cli") and d, "cli = 'claude'"),
        (lambda d: d["roles"].update(judge=d["roles"]["reviewer"]) or d, "exactly one role"),
    ],
)
def test_declaration_shape_is_checked(edit, message):
    data = edit(tomllib.loads(DECLARATION.read_text()))
    with pytest.raises(DeclarationError, match=message):
        bind(parse_engine(data, "code-review.toml"), "code-review.toml")


def test_a_gpt_model_is_refused_for_its_reviewer():
    with pytest.raises(ValueError, match="runs on claude"):
        settings_of("gpt-6.2-astra", "high")


def test_launch_sends_the_command_for_the_pr_on_stdin_with_no_model_of_its_own():
    run = launch(7)
    assert (run.cli, run.argv, run.stdin) == ("claude", ARGV, "/code-review 7\n")
    assert run.argv == ("claude", "-p", "--permission-mode", "bypassPermissions",
                        "--output-format", "json")  # fmt: skip
    # No effort level is typed after the command, and no model or effort is in the argv: the
    # shim adds the plan's, and the review runs at the session's effort.
    assert not {"--model", "--effort"} & set(run.argv)
    assert (run.files, run.output) == ({}, None)


@pytest.mark.parametrize("pr", [0, -1, True, "7", 7.0, None])
def test_launch_refuses_what_is_not_a_pr_number(pr):
    with pytest.raises(ValueError, match="PR number"):
        launch(pr)


@pytest.mark.parametrize(("model", "effort"), [("claude-opus-5-5", "xhigh"), ("claude-sonnet-5-5", "medium")])  # fmt: skip
def test_the_plans_model_and_effort_reach_the_session_through_the_shim(model, effort):
    settings = settings_of(model, effort)
    assert requested(settings) == Setting(model, effort)
    by_flag = shim_settings({"claude": requested(settings)})["claude"]

    final, added, session = shim.claude_launch(list(launch(7).argv[1:]), by_flag)

    assert added == [*CLAUDE_FLAGS, "--model", model, "--effort", effort, "--session-id", session]
    assert final == added + list(ARGV[1:])


@pytest.mark.parametrize("settings", [{}, {"roles": {"reviewer": {"models": ["a", "b"]}}}, None])
def test_requested_refuses_other_settings(settings):
    with pytest.raises(ValueError, match="not the settings of a code-review configuration"):
        requested(settings)


def test_default_configuration_runs_with_the_fake_claude_and_gives_normalized_findings(
    fake_bin, tmp_path: Path
):
    fake_bin.scenario("claude", stdout=session_result())
    claude_answers(fake_bin, json.dumps(result()))  # the normalizer, through the fake docker
    run = launch(7)

    done = subprocess.run(
        run.argv, input=run.stdin, capture_output=True, text=True, check=True, cwd=tmp_path
    )
    got = findings(project(tmp_path), done.stdout, "t-1")

    session, *steps = fake_bin.calls()
    assert session["tool"] == "claude" and session["argv"] == list(ARGV[1:])
    assert session["stdin"] == "/code-review 7\n"
    (normalizer,) = [c for c in steps if " claude -p " in " ".join(c["argv"])]
    assert normalizer["tool"] == "docker" and REPORT in normalizer["stdin"]
    assert got.findings == [{k: v for k, v in f.items() if v is not None} for f in ANSWER["findings"]]  # fmt: skip
    assert got.unlocated == ["Missing tests: nothing covers the new branch."]


def test_a_blank_report_has_no_findings_and_runs_no_normalizer(fake_bin, tmp_path: Path):
    got = findings(project(tmp_path), session_result(result="\n"), "t-1")
    assert (got.findings, got.unlocated) == ([], [])
    assert fake_bin.calls() == []


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        (SECRET, "not one JSON object"),
        (json.dumps([SECRET]), "not a result object"),
        ('{"type": "result", "type": "result"}', "not one JSON object"),
        (session_result(is_error=True, subtype="error_during_execution"), "error_during_execution"),
        (session_result(subtype=SECRET), "an unknown subtype"),
        (session_result(modelUsage={}), "made no model call"),
        (session_result(result=None), "has no text"),
    ],
)
def test_output_without_a_review_fails_the_trial_without_quoting_it(stdout, message):
    with pytest.raises(CodeReviewOutputError, match=message) as e:
        report(stdout, WHERE)
    assert str(e.value).startswith(WHERE) and SECRET not in str(e.value)


def test_the_report_is_the_results_markdown():
    assert report(session_result(), WHERE) == REPORT
