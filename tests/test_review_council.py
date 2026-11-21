"""The `review-council` adapter (design D5, D9, D13): declaration, the trial's homes, the launch,
the shim log and the report."""

import json
import shutil
from pathlib import Path

import pytest
from test_hermetic import exec_argv, project
from test_shims import CLAUDE_FLAGS, CODEX_FLAGS, shim

from rbench.engines import review_council
from rbench.engines.declaration import DeclarationError, Setting, load_engine, parse_engine
from rbench.engines.review_council import ReviewCouncilOutputError
from rbench.matrix.expansion import Configuration, RoleSetting
from rbench.trial import hermetic, prepare

ENGINES = Path(__file__).parents[1] / "engines"
CLAUDE_MODEL, CODEX_MODEL = "claude-sonnet-5-5", "gpt-6.2-astra"
ARGV = [
    "-p", "/review-council:run", "--plugin-dir", review_council.PLUGIN_DIR,
    "--setting-sources", "user", "--allowedTools", "Bash Read Grep Glob Agent Skill Write",
    "--output-format", "stream-json", "--verbose",
]  # fmt: skip
# The Codex reviewer's launch in review-council 0.14.0 (scripts/rc-invoke-provider.sh).
CODEX_ARGV = ["exec", "--sandbox", "read-only", "--skip-git-repo-check", "-o", "/tmp/rc.last"]


def settings(claude: str = CLAUDE_MODEL, codex: str = CODEX_MODEL, effort: str = "medium") -> dict:
    engine = load_engine(ENGINES / "review-council.toml")
    roles = (
        ("claude_side", RoleSetting((claude,), effort)),
        ("codex_side", RoleSetting((codex,), effort)),
    )
    return Configuration(engine.name, engine.version, roles, markdown=True).settings


def shim_record(cli: str, argv: list[str], homes: dict) -> dict:
    """The launch record the real shim writes for `argv` under the trial's shim settings."""
    if cli == "claude":
        final, added, session = shim.claude_launch(argv, homes["claude"])
        extra = {"session_id": session}
    else:
        final, added, rewrites = shim.codex_launch(argv, homes["codex"])
        extra = {"rewrites": rewrites}
    return {"kind": "launch", "cli": cli, "argv": argv, "added": added, "final_argv": final} | extra


def shim_log(s: dict, *codex: list[str]) -> list[dict]:
    """A trial's shim log: the council's Claude session, a `codex --version` probe and each of
    the `codex` launches."""
    homes, _ = review_council.trial_homes(s)
    records = [shim_record("claude", ARGV, homes), shim_record("codex", ["--version"], homes)]
    return records + [shim_record("codex", argv, homes) for argv in codex]


def test_declaration_is_pinned_by_the_plugin_version_and_outputs_markdown():
    engine = load_engine(ENGINES / "review-council.toml")

    assert (engine.pin_kind, engine.version, engine.output) == ("version", "0.14.0", "markdown")
    assert engine.default_configuration() == {
        "claude_side": Setting("claude-opus-5-5", "high"),
        "codex_side": Setting("gpt-6.2-sol", "high"),
    }
    assert review_council.RELEASES["0.14.0"] == "c9a73575503c12277cd46409c7cbdbe452e7c582"


def _declaration(**edit: object) -> dict:
    side = {"default": {"model": CLAUDE_MODEL, "effort": "high"}}
    return {
        "name": "review-council",
        "pin": {"kind": "version", "ref": "0.14.0"},
        "output": "markdown",
        "cli": {"claude": "2.1.316", "codex": "0.171.0"},
        "roles": {
            "claude_side": side | {"cli": "claude", "delivery": "flag"},
            "codex_side": {"cli": "codex", "delivery": "config",
                           "default": {"model": CODEX_MODEL, "effort": "high"}},
        },
    } | edit  # fmt: skip


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ({"pin": {"kind": "version", "ref": "0.13.0"}}, "pin must be"),
        ({"pin": {"kind": "cli", "ref": "claude"}}, "pin must be"),
        ({"output": "structured"}, "markdown"),
        ({"roles": {"claude_side": {"cli": "claude", "delivery": "flag",
                                    "default": {"model": CLAUDE_MODEL, "effort": "high"}}}},
         "exactly the roles"),
        ({"roles": {"claude_side": {"cli": "claude", "delivery": "flag",
                                    "default": {"model": CLAUDE_MODEL, "effort": "high"}},
                    "codex_side": {"cli": "codex", "delivery": "flag",
                                   "default": {"model": CODEX_MODEL, "effort": "high"}}}},
         "exactly the roles"),
    ],
)  # fmt: skip
def test_declaration_shape_is_checked(edit: dict, message: str):
    with pytest.raises(DeclarationError, match=message):
        review_council.bind(parse_engine(_declaration(**edit), "review-council.toml"), ENGINES)


def test_declaration_needs_its_config_file(tmp_path: Path):
    shutil.copy(ENGINES / "review-council.toml", tmp_path)

    with pytest.raises(DeclarationError, match="config.local.yml"):
        load_engine(tmp_path / "review-council.toml")


def test_codex_model_and_effort_go_to_config_toml_and_only_claude_to_the_shim():
    homes, config = review_council.trial_homes(settings(effort="xhigh"))

    assert config == f'model = "{CODEX_MODEL}"\nmodel_reasoning_effort = "xhigh"\n'
    assert homes["claude"] == hermetic.POLICY["claude"] | {
        "model": CLAUDE_MODEL,
        "effort": "xhigh",
    }
    assert homes["codex"] == hermetic.POLICY["codex"]  # no model or effort from the shim
    assert review_council.requested(settings(effort="xhigh")) == {
        "claude": Setting(CLAUDE_MODEL, "xhigh"),
        "codex": Setting(CODEX_MODEL, "xhigh"),
    }


def test_trial_writes_the_codex_config_and_the_shim_settings(fake_bin, tmp_path: Path):
    homes, config = review_council.trial_homes(settings())

    prepare(project(tmp_path), homes, config)

    calls = {exec_argv(c)[-2]: c["stdin"] for c in fake_bin.calls() if "-c" in exec_argv(c)}
    assert calls["/home/node/.codex/config.toml"] == (
        f'model = "{CODEX_MODEL}"\nmodel_reasoning_effort = "medium"\n'
    )
    written = json.loads(calls[hermetic.SHIM_SETTINGS])
    assert (written["claude"]["model"], written["claude"]["effort"]) == (CLAUDE_MODEL, "medium")
    assert "model" not in written["codex"] and "effort" not in written["codex"]


def test_launch_runs_the_plugin_command_with_no_task_data_and_writes_the_seat_config():
    launch = review_council.launch(ENGINES, settings())

    assert launch.cli == "claude" and launch.argv == ("claude", *ARGV)
    assert (launch.stdin, launch.output) == ("", None)
    assert list(launch.files) == ["/rbench/repo/.review-council/config.local.yml"]
    text = launch.files["/rbench/repo/.review-council/config.local.yml"].decode()
    assert "google: { enabled: false }" in text and "perplexity: { enabled: false }" in text
    assert "model:" not in text  # the sides' models come from the flags and config.toml


def test_launch_refuses_settings_of_another_engine_or_release():
    s = settings()
    with pytest.raises(ValueError, match="release"):
        review_council.launch(ENGINES, s | {"version": "0.13.0"})
    two = s | {
        "roles": s["roles"] | {"codex_side": {"models": [CODEX_MODEL] * 2, "effort": "high"}}
    }
    with pytest.raises(ValueError, match="exactly one model"):
        review_council.requested(two)


def test_shim_log_of_a_run_as_configured_passes():
    s = settings()
    log = shim_log(s, CODEX_ARGV, CODEX_ARGV)  # the Codex reviewer and the Codex verifier

    assert review_council.check_launches(log, review_council.launch(ENGINES, s), s) == []
    claude, _, codex, _ = log
    assert claude["added"][: len(CLAUDE_FLAGS)] == CLAUDE_FLAGS
    assert ["--model", CLAUDE_MODEL, "--effort", "medium"] == claude["added"][2:6]
    # Codex gets the policy flags and the sandbox rewrite, and no model: config.toml sets it.
    assert codex["added"] == [*CODEX_FLAGS, "--ignore-rules"]
    assert codex["rewrites"] == [{"index": 2, "from": "read-only", "to": "danger-full-access"}]
    assert not {"-m", "--model"} & set(codex["final_argv"])


@pytest.mark.parametrize(
    ("own", "named"),
    [
        (["-m", "gpt-6.2-sol"], "-m"),
        (["--model=gpt-6.2-sol"], "--model"),
        (["-c", 'model="gpt-6.2-sol"'], "-c model"),
        (["--config", 'model_reasoning_effort="low"'], "-c model_reasoning_effort"),
        (["-p", "fast"], "-p"),
        (["--ignore-user-config"], "--ignore-user-config"),
    ],
)
def test_codex_session_that_cannot_run_the_config_model_is_misconfigured(own, named):
    s = settings()
    argv = ["exec", *own, *CODEX_ARGV[1:]]

    problems = review_council.check_launches(
        shim_log(s, argv), review_council.launch(ENGINES, s), s
    )

    assert problems == [
        f"Codex session 0 sets {named}, so the trial's config.toml model and effort did not apply"
    ]


def test_claude_session_problems_are_named():
    s = settings()
    launch = review_council.launch(ENGINES, s)
    homes, _ = review_council.trial_homes(settings(claude="claude-opus-5-5"))
    other = shim_record("claude", ARGV, homes)

    assert review_council.check_launches([], launch, s) == [
        "expected one Claude session launch in the shim log, found 0"
    ]
    assert review_council.check_launches([other], launch, s) == [
        f"the Claude session did not get --model {CLAUDE_MODEL}"
    ]
    changed = shim_record("claude", [*ARGV, "--max-turns", "3"], review_council.trial_homes(s)[0])
    assert review_council.check_launches([changed], launch, s) == [
        "the Claude launch is not the argv the review-council adapter built"
    ]


def test_a_rewrite_other_than_the_sandbox_is_named():
    s = settings()
    log = shim_log(s, CODEX_ARGV)
    log[-1]["rewrites"].append({"index": 4, "from": "-o", "to": "-x"})

    assert review_council.check_launches(log, review_council.launch(ENGINES, s), s) == [
        "the shim rewrote more than the sandbox of Codex session 0"
    ]


def test_codex_fallback_to_its_default_model_is_a_model_mismatch():
    applied = {
        "/home/node/.claude/projects/-rbench-repo/s.jsonl": ("claude", CLAUDE_MODEL),
        "/home/node/.codex/sessions/rollout-a.jsonl": ("codex", CODEX_MODEL),
        "/home/node/.codex/sessions/rollout-b.jsonl": ("codex", "gpt-6.2-sol"),
    }

    assert review_council.model_mismatches(settings(), applied) == [
        (
            "session /home/node/.codex/sessions/rollout-b.jsonl: codex ran model 'gpt-6.2-sol',"
            f" the configuration requested '{CODEX_MODEL}'"
        )
    ]
    assert review_council.model_mismatches(settings(), dict(list(applied.items())[:2])) == []


def events(*lines: object) -> str:
    return "".join(json.dumps(line) + "\n" for line in lines)


def test_report_is_the_final_result_of_the_stream():
    report = "## Review Council Report\n\n### Critical Issues\n- `a.py:3` broken\n"
    stdout = events(
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": []}},
        {"type": "result", "subtype": "success", "is_error": False, "result": report},
    )

    assert review_council.report("not json\n" + stdout) == report


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([], "found 0"),
        ([{"type": "result", "subtype": "success", "is_error": False, "result": "a"}] * 2,
         "found 2"),
        ([{"type": "result", "subtype": "error_max_turns", "is_error": True}],
         "subtype error_max_turns"),
        ([{"type": "result", "subtype": "secret text", "is_error": True}], "an unknown subtype"),
        ([{"type": "result", "subtype": "success", "is_error": False, "result": " \n"}],
         "no report"),
    ],
)  # fmt: skip
def test_session_without_a_report_fails_without_quoting_it(lines, message):
    with pytest.raises(ReviewCouncilOutputError, match=message) as e:
        review_council.report(events(*lines))
    assert "secret" not in str(e.value)


def test_image_plugin_is_the_pinned_commit_installed_under_its_name():
    assert review_council.plugin("0.14.0") == (
        "https://github.com/deployhq/review-council",
        "c9a73575503c12277cd46409c7cbdbe452e7c582",
        "0.14.0",
    )
    assert review_council.PLUGIN_DIR == "/opt/rbench/plugins/review-council"
    with pytest.raises(ValueError, match="no release"):
        review_council.plugin("0.13.0")
