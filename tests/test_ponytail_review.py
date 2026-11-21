"""The `ponytail-review` engine (spec engine-configurations, Engine adapters; design D5, D13): a
review plugin on Claude Code and on Codex, with the fake `claude` and `codex` for the session and a
fake docker for the normalization step."""

import json
import subprocess
import tomllib
from pathlib import Path

import pytest
from conftest import REPO
from test_normalization import ANSWER, REPORT, claude_answers, project, result
from test_shims import CLAUDE_FLAGS, CODEX_FLAGS, shim

from rbench.engines.declaration import DeclarationError, Setting, load_engine, parse_engine
from rbench.engines.pins import FloatingRefError
from rbench.engines.ponytail_review import (
    CLAUDE_ARGV,
    CODEX_ARGV,
    CODEX_SETUP,
    OUTPUT_PATH,
    PLUGIN_DIR,
    RELEASES,
    SOURCE,
    PluginReviewOutputError,
    bind,
    findings,
    launch,
    plugin,
    report,
    requested,
    setup,
)
from rbench.matrix.expansion import parse_matrix
from rbench.trial.hermetic import shim_settings

DECLARATION = REPO / "engines" / "ponytail-review.toml"
WHERE = "trial t-1: ponytail-review"
CLAUDE_MODELS = ("claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5")
GPT_MODELS = ("gpt-6.2-sol", "gpt-6.2-astra")
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


def claude_result(**fields: object) -> str:
    """The `claude -p --output-format json` result of a review session."""
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": REPORT,
        "modelUsage": MODEL_USAGE,
        "session_id": "00000000-0000-4000-8000-000000000002",
    }
    return json.dumps(base | fields)


def codex_events(*types: str) -> str:
    """The `codex exec --json` events of a session, one per line."""
    return "".join(json.dumps({"type": t}) + "\n" for t in types)


CODEX_OK = codex_events("thread.started", "turn.started", "item.completed", "turn.completed")


def settings_of(model: str, effort: str = "high") -> dict:
    """The frozen settings of a configuration of the repository's declaration."""
    (configuration,) = configurations([model], effort)
    return configuration.settings


def configurations(models: list[str], effort: str = "high") -> list:
    engine = load_engine(DECLARATION)
    roles = {"reviewer": {"model": models, "effort": effort}}
    entry = {"engine": "ponytail-review", "roles": roles}
    data = {"stage": "screening", "tasks": "core", "attempts": 1}
    data["rounds"] = [{"name": "r1", "budget": {"claude": 1, "codex": 1}, "entries": [entry]}]
    (r,) = parse_matrix(data, "m.toml", {"ponytail-review": engine}).rounds
    return list(r.configurations)


def test_declaration_pins_a_known_release_with_its_source_and_commit():
    engine = load_engine(DECLARATION)
    assert (engine.pin_kind, engine.version, engine.output) == ("version", "5.3.3", "markdown")
    assert engine.clis == {"claude": "2.1.316", "codex": "0.171.0"}
    assert engine.default_configuration() == {"reviewer": Setting("claude-sonnet-5-5", "high")}
    tag, commit = RELEASES["5.3.3"]
    assert (tag, len(commit)) == ("refs/tags/v5.3.3", 40)
    assert plugin(engine) == (SOURCE, commit, "5.3.3")
    # The plugin's release and the normalizer's pin are in the settings, so either gives new ids.
    settings = settings_of("claude-opus-5-5")
    assert settings["version"] == "5.3.3" and "normalization" in settings


@pytest.mark.parametrize("ref", ["latest", "5.3", "^5.3.3", "~5.3.3", "5.x", ">=5.3.3", "main"])
def test_a_floating_plugin_version_is_rejected(ref):
    data = tomllib.loads(DECLARATION.read_text())
    data["pin"]["ref"] = ref
    with pytest.raises(FloatingRefError, match="floating reference"):
        parse_engine(data, "ponytail-review.toml")


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda d: d["pin"].update(ref="5.3.4") or d, "a release the adapter knows: 5.3.3"),
        (lambda d: d | {"pin": {"kind": "git", "ref": "a" * 40}}, "kind 'version'"),
        (lambda d: d | {"output": "structured"}, "output must be 'markdown'"),
        (lambda d: d["cli"].pop("codex") and d, "needs both cli.claude and cli.codex"),
        (lambda d: d["roles"]["reviewer"].update(cli="claude") or d, "no cli"),
        (lambda d: d["roles"].update(judge=d["roles"]["reviewer"]) or d, "exactly one role"),
    ],
)
def test_declaration_shape_is_checked(edit, message):
    data = edit(tomllib.loads(DECLARATION.read_text()))
    with pytest.raises(DeclarationError, match=message):
        bind(parse_engine(data, "ponytail-review.toml"), "ponytail-review.toml")


def test_the_d13_matrix_runs_claude_models_on_claude_code_and_gpt_models_on_codex():
    found = configurations([*CLAUDE_MODELS, *GPT_MODELS])
    assert len(found) == 3 + 2
    clis = {c.settings["roles"]["reviewer"]["models"][0]: launch(c.settings, 7).cli for c in found}
    assert clis == {m: "claude" for m in CLAUDE_MODELS} | {m: "codex" for m in GPT_MODELS}


def test_claude_launch_loads_the_pinned_plugin_and_sends_its_command_on_stdin():
    settings = settings_of("claude-opus-5-5")
    run = launch(settings, 7)
    assert run.argv == ("claude", "-p", "--plugin-dir", PLUGIN_DIR, "--permission-mode",
                        "bypassPermissions", "--output-format", "json")  # fmt: skip
    assert run.stdin == "/ponytail:ponytail-review PR #7\n"
    assert (run.files, run.output) == ({}, None)
    assert setup(settings) == ()  # no install into the home: --plugin-dir loads it


def test_codex_launch_installs_the_plugin_from_the_image_then_mentions_its_skill():
    settings = settings_of("gpt-6.2-astra")
    run = launch(settings, 7)
    assert run.argv == ("codex", "exec", "--sandbox", "danger-full-access", "--json",
                        "--output-last-message", OUTPUT_PATH, "-")  # fmt: skip
    assert run.stdin == "$ponytail:ponytail-review PR #7\n"
    assert (run.files, run.output) == ({}, OUTPUT_PATH)
    assert (
        setup(settings)
        == CODEX_SETUP
        == (
            ("codex", "plugin", "marketplace", "add", "/opt/rbench/plugins"),
            ("codex", "plugin", "add", "ponytail@rbench"),
        )
    )


def test_neither_launch_sets_a_model_or_effort_of_its_own():
    for argv in (CLAUDE_ARGV, CODEX_ARGV):
        assert not {"--model", "-m", "--effort", "-c", "--config"} & set(argv)


@pytest.mark.parametrize("pr", [0, -1, True, "7", 7.0, None])
def test_launch_refuses_what_is_not_a_pr_number(pr):
    with pytest.raises(ValueError, match="PR number"):
        launch(settings_of("claude-opus-5-5"), pr)


@pytest.mark.parametrize(
    "settings",
    [{}, None, {"roles": {"reviewer": {"models": ["a", "b"], "effort": "high"}}},
     {"roles": {"reviewer": {"models": ["opus"], "effort": "high"}}}],
)  # fmt: skip
def test_requested_refuses_other_settings(settings):
    with pytest.raises(ValueError, match="ponytail-review"):
        requested(settings)


@pytest.mark.parametrize(("model", "effort"), [("claude-opus-5-5", "xhigh"), ("claude-sonnet-5-5", "medium")])  # fmt: skip
def test_claude_gets_the_plans_model_and_effort_through_the_shim(model, effort):
    settings = settings_of(model, effort)
    assert requested(settings) == Setting(model, effort)
    by_flag = shim_settings({"claude": requested(settings)})["claude"]

    final, added, session = shim.claude_launch(list(launch(settings, 7).argv[1:]), by_flag)

    assert added == [*CLAUDE_FLAGS, "--model", model, "--effort", effort, "--session-id", session]
    assert final == added + list(CLAUDE_ARGV[1:])


@pytest.mark.parametrize(("model", "effort"), [("gpt-6.2-sol", "xhigh"), ("gpt-6.2-astra", "medium")])  # fmt: skip
def test_codex_gets_the_plans_model_and_effort_through_the_shim(model, effort):
    settings = settings_of(model, effort)
    assert requested(settings) == Setting(model, effort)
    by_flag = shim_settings({"codex": requested(settings)})["codex"]

    final, added, rewrites = shim.codex_launch(list(launch(settings, 7).argv[1:]), by_flag)

    chosen = ["-c", f'model="{model}"', "-c", f'model_reasoning_effort="{effort}"']
    assert final == [*CODEX_FLAGS, *chosen, "exec", "--ignore-rules", *CODEX_ARGV[2:]]
    assert (added, rewrites) == ([*CODEX_FLAGS, *chosen, "--ignore-rules"], [])
    # The install calls are no sessions: the shim passes them unchanged.
    for argv in CODEX_SETUP:
        assert shim.codex_launch(list(argv[1:]), by_flag) == (list(argv[1:]), [], [])


def run(launch_, cwd: Path) -> str:
    done = subprocess.run(
        launch_.argv, input=launch_.stdin, capture_output=True, text=True, check=True, cwd=cwd
    )
    return done.stdout


EXPECTED = [{k: v for k, v in f.items() if v is not None} for f in ANSWER["findings"]]


def test_claude_default_runs_with_the_fake_claude_and_gives_normalized_findings(
    fake_bin, tmp_path: Path
):
    fake_bin.scenario("claude", stdout=claude_result())
    claude_answers(fake_bin, json.dumps(result()))  # the normalizer, through the fake docker
    settings = load_engine(DECLARATION).default_configuration()["reviewer"]
    launch_ = launch(settings_of(settings.model, settings.effort), 7)

    got = findings(project(tmp_path), launch_, run(launch_, tmp_path), None, "t-1")

    session, *steps = fake_bin.calls()
    assert session["tool"] == "claude" and session["argv"] == list(CLAUDE_ARGV[1:])
    assert session["stdin"] == "/ponytail:ponytail-review PR #7\n"
    (normalizer,) = [c for c in steps if " claude -p " in " ".join(c["argv"])]
    assert normalizer["tool"] == "docker" and REPORT in normalizer["stdin"]
    assert got.findings == EXPECTED
    assert got.unlocated == ["Missing tests: nothing covers the new branch."]


def test_codex_variant_runs_with_the_fake_codex_and_gives_normalized_findings(
    fake_bin, tmp_path: Path
):
    fake_bin.scenario("codex", stdout=CODEX_OK)
    claude_answers(fake_bin, json.dumps(result()))
    settings = settings_of("gpt-6.2-astra")
    launch_ = launch(settings, 7)

    for argv in setup(settings):
        subprocess.run(argv, check=True, cwd=tmp_path, stdin=subprocess.DEVNULL)
    stdout = run(launch_, tmp_path)
    got = findings(project(tmp_path), launch_, stdout, REPORT, "t-1")

    install, add, session, *steps = fake_bin.calls()
    assert [install["argv"], add["argv"]] == [list(a[1:]) for a in CODEX_SETUP]
    assert session["tool"] == "codex" and session["argv"] == list(CODEX_ARGV[1:])
    assert session["stdin"] == "$ponytail:ponytail-review PR #7\n"
    (normalizer,) = [c for c in steps if " claude -p " in " ".join(c["argv"])]
    assert REPORT in normalizer["stdin"]
    assert got.findings == EXPECTED


def test_a_blank_review_has_no_findings_and_runs_no_normalizer(fake_bin, tmp_path: Path):
    claude = launch(settings_of("claude-opus-5-5"), 7)
    codex = launch(settings_of("gpt-6.2-sol"), 7)
    for got in (
        findings(project(tmp_path), claude, claude_result(result="\n"), None, "t-1"),
        findings(project(tmp_path / "2"), codex, CODEX_OK, "", "t-1"),
    ):
        assert (got.findings, got.unlocated) == ([], [])
    assert fake_bin.calls() == []


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        (SECRET, "stdout is not JSON"),
        (json.dumps([SECRET]), "not a result object"),
        ('{"type": "result", "type": "result"}', "stdout is not JSON"),
        (claude_result(is_error=True, subtype="error_during_execution"), "error_during_execution"),
        (claude_result(subtype=SECRET), "an unknown subtype"),
        (claude_result(modelUsage={}), "made no model call"),
        (claude_result(result=None), "has no text"),
    ],
)
def test_claude_output_without_a_review_fails_the_trial_without_quoting_it(stdout, message):
    claude = launch(settings_of("claude-opus-5-5"), 7)
    with pytest.raises(PluginReviewOutputError, match=message) as e:
        report(claude, stdout, None, WHERE)
    assert str(e.value).startswith(WHERE) and SECRET not in str(e.value)


@pytest.mark.parametrize(
    ("stdout", "answer", "message"),
    [
        (SECRET + "\n", REPORT, "stdout line 1 is not JSON"),
        (CODEX_OK + "[1]\n", REPORT, "stdout line 5 is not an event"),
        (codex_events("turn.started", "turn.failed"), REPORT, "reported turn.failed"),
        (CODEX_OK + codex_events("error"), REPORT, "reported error"),
        (codex_events("thread.started", "turn.started"), REPORT, "completed no turn"),
        ("", REPORT, "completed no turn"),
        (CODEX_OK, None, f"wrote no {OUTPUT_PATH}"),
    ],
)
def test_codex_output_without_a_review_fails_the_trial_without_quoting_it(stdout, answer, message):
    codex = launch(settings_of("gpt-6.2-astra"), 7)
    with pytest.raises(PluginReviewOutputError, match=message) as e:
        report(codex, stdout, answer, WHERE)
    assert str(e.value).startswith(WHERE) and SECRET not in str(e.value)


def test_the_review_is_each_clis_markdown():
    claude = launch(settings_of("claude-opus-5-5"), 7)
    codex = launch(settings_of("gpt-6.2-astra"), 7)
    assert report(claude, claude_result(), None, WHERE) == REPORT
    assert report(codex, CODEX_OK, REPORT, WHERE) == REPORT
