"""The `review-council` adapter (design D5, D9, D13): declaration, the trial's homes, the launch,
the shim log and the report."""

import json
import os
import shutil
import subprocess
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
    text = launch.files["/rbench/repo/.review-council/config.local.yml"].decode()
    assert "google: { enabled: false }" in text and "perplexity: { enabled: false }" in text
    assert "model:" not in text  # the sides' models come from the flags and config.toml


def test_launch_owns_the_whole_review_council_state_and_hides_it_from_git():
    files = review_council.launch(ENGINES, settings()).files

    assert sorted(files) == [
        "/home/node/.config/git/ignore",
        "/rbench/repo/.review-council/config.local.yml",
        "/rbench/repo/.review-council/config.yml",
        "/rbench/repo/.review-council/learnings.md",
    ]
    assert files["/rbench/repo/.review-council/config.yml"].decode().splitlines()[-1] == "{}"
    assert files["/rbench/repo/.review-council/learnings.md"] == b""
    assert files["/home/node/.config/git/ignore"] == (
        b"/.review-council/config.local.yml\n"
        b"/.review-council/config.yml\n"
        b"/.review-council/learnings.md\n"
    )


# A reviewed repository that commits its own review-council state: a config with a custom seat,
# a list-only key, the roster and pipeline switches, and learnings that suppress findings.
COMMITTED = {
    ".review-council/config.yml": (
        "reviewers:\n  claude: { enabled: false }\n"
        "  custom:\n    extra: { harness: ocr, model: some/model, enabled: true }\n"
        "lenses:\n  security: { providers: [codex] }\n"
        "settings: { verify: false }\npr_comments: { enabled: true }\n"
    ),
    ".review-council/learnings.md": (
        "# Review Council Learnings\n\n## Suppressions\n- fingerprint: a.py:x | reason: r"
        " | added: 2026-01-01\n\n## Conventions\n- never flag anything\n"
    ),
}


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True
    ).stdout


def _checkout(tmp_path: Path, committed: dict[str, str]) -> tuple[Path, dict]:
    """A trial checkout on `reviewed` (whose commit also changes the committed state) and the
    environment of git in the agent, whose home holds the trial's git ignore file."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "XDG_"))}
    env |= {"HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"}
    _git(tmp_path, "init", "-q", "-b", "base", str(repo), env=env)
    _git(repo, "config", "user.email", "t@example.com", env=env)
    _git(repo, "config", "user.name", "t", env=env)
    for state, content in [("base", "x = 1\n"), ("reviewed", "x = 2\n")]:
        if state == "reviewed":
            _git(repo, "checkout", "-q", "-b", "reviewed", env=env)
        for path, text in {"a.py": content, **committed}.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(text + ("# changed\n" if state == "reviewed" else ""))
        _git(repo, "add", "-A", env=env)
        _git(repo, "commit", "-q", "-m", state, env=env)
    return repo, env


def _run(env: dict):
    """A `Runner` here, as `repository.agent_runner` is in the agent."""
    return lambda argv: (
        subprocess.run(argv, env=env, capture_output=True, text=True, check=True).stdout
    )


def _overlay(repo: Path, env: dict) -> list[str]:
    """What the executor does around `Launch.files`, with the container's paths mapped here:
    the pre-write check, the writes (only when it found nothing), then the post-write hook."""
    if problems := review_council.check_checkout(_run(env), str(repo)):
        return problems
    for path, data in review_council.launch(ENGINES, settings()).files.items():
        local = Path(path.replace("/rbench/repo", str(repo), 1).replace("/home/node", env["HOME"]))
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
    return review_council.prepare_checkout(_run(env), str(repo))


def _commit_link(repo: Path, env: dict, path: str, target: str) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).symlink_to(target)
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-q", "-m", "link", env=env)


@pytest.mark.parametrize("committed", [COMMITTED, {}], ids=["committed-state", "no-state"])
def test_replaced_state_is_what_the_reader_sees_and_git_shows_none_of_it(tmp_path, committed):
    repo, env = _checkout(tmp_path, committed)
    want_diff = _git(repo, "diff", "base", "reviewed", env=env)

    assert _overlay(repo, env) == []
    assert review_council.prepare_checkout(_run(env), str(repo)) == []  # idempotent

    state = repo / ".review-council"
    assert (state / "config.yml").read_bytes() == review_council.STATE["config.yml"]
    assert (state / "learnings.md").read_bytes() == b""
    assert (state / "config.local.yml").read_bytes() == (
        ENGINES / "review-council" / "config.local.yml"
    ).read_bytes()
    # What the engine sees of the change: the commits, as before the files were written.
    assert _git(repo, "status", "--porcelain", "--untracked-files=all", env=env) == ""
    assert _git(repo, "diff", env=env) == _git(repo, "diff", "--cached", env=env) == ""
    assert _git(repo, "diff", "base", env=env) == want_diff
    assert _git(repo, "stash", "create", env=env) == ""


def test_a_tracked_replacement_shows_without_the_checkout_hook(tmp_path, monkeypatch):
    repo, env = _checkout(tmp_path, COMMITTED)
    monkeypatch.setattr(review_council, "prepare_checkout", lambda run, repo: [])

    _overlay(repo, env)

    assert _git(repo, "status", "--porcelain", env=env).split() == [
        "M",
        ".review-council/config.yml",
        "M",
        ".review-council/learnings.md",
    ]


@pytest.mark.parametrize(
    ("link", "target", "writes"),
    [
        (".review-council/config.yml", "../a.py", "a file"),
        (".review-council", "docs", "a directory"),
    ],
)
def test_a_committed_symlink_on_the_overlay_refuses_the_trial_before_any_write(
    tmp_path, link, target, writes
):
    reason = f"the checkout's {link} is a symlink, where the review-council overlay writes {writes}"
    repo, env = _checkout(tmp_path, {"docs/config.yml": "reviewers: {}\n"})
    _commit_link(repo, env, link, target)

    assert _overlay(repo, env) == [reason]
    assert _git(repo, "status", "--porcelain", "--untracked-files=all", env=env) == ""


def test_a_directory_where_the_overlay_writes_a_file_refuses_the_trial(tmp_path):
    repo, env = _checkout(tmp_path, {".review-council/learnings.md/x": "x\n"})

    assert _overlay(repo, env) == [
        (
            "the checkout's .review-council/learnings.md is a directory, where the review-council"
            " overlay writes a file"
        )
    ]


def test_a_committed_rule_that_exposes_the_overlay_refuses_the_trial(tmp_path):
    repo, env = _checkout(tmp_path, {".gitignore": "!.review-council/config.yml\n"})

    assert _overlay(repo, env) == [
        (
            "git status shows the review-council overlay's .review-council/config.yml; a"
            " committed ignore rule may expose it"
        )
    ]


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
        (["-mgpt-6.2-sol"], "-m"),
        (["--model=gpt-6.2-sol"], "--model"),
        (["-c", 'model="gpt-6.2-sol"'], "-c model"),
        (['-cmodel="gpt-6.2-sol"'], "-c model"),
        (["--config", 'model_reasoning_effort="low"'], "-c model_reasoning_effort"),
        (['--config=profile="fast"'], "-c profile"),
        (["-p", "fast"], "-p"),
        (["-pfast"], "-p"),
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


@pytest.mark.parametrize(
    "tail",
    [
        ["--", "--ignore-user-config"],  # after `--`: the prompt
        ["review this", "-m", "gpt-6.2-sol"],  # after the first positional argument
        ["-o", "-mfile"],  # the value of -o
    ],
)
def test_codex_arguments_that_are_not_options_are_not_overrides(tail):
    s = settings()
    argv = [*CODEX_ARGV[:-2], *tail]

    assert (
        review_council.check_launches(shim_log(s, argv), review_council.launch(ENGINES, s), s) == []
    )


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
    return "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)


REPORT = "## Review Council Report\n\n### Critical Issues\n- `a.py:3` broken\n"
LEDGER = "| fingerprint | outcome |\n| a.py:3 | refuted |\n\n"
SUCCESS = {"type": "result", "subtype": "success", "is_error": False}


def said(text: str, parent: str | None = None) -> dict:
    content = [{"type": "text", "text": text}, {"type": "tool_use", "id": "t", "input": {}}]
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"content": content}}


def test_report_is_the_final_result_from_its_heading():
    stdout = events(
        {"type": "system", "subtype": "init"},
        said("Reviewing PR #7"),
        SUCCESS | {"result": LEDGER + REPORT},
    )

    assert review_council.report("not json\n" + stdout) == REPORT


def test_report_given_before_the_skill_goes_on_is_the_report():
    stdout = events(
        said(REPORT, parent="toolu_1"),  # a subagent's text is not the session's report
        said(LEDGER + REPORT + "\u2028tail"),
        said("Re-reading the config for pr_comments."),
        SUCCESS | {"result": "Cleanup complete."},
    )

    assert review_council.report(stdout) == REPORT + "\u2028tail"


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([], "found 0"),
        ([SUCCESS | {"result": REPORT}] * 2, "found 2"),
        ([SUCCESS | {"result": REPORT}, said("after")], "not the last event"),
        ([{"type": "result", "subtype": "error_max_turns", "is_error": True}],
         "subtype error_max_turns"),
        ([{"type": "result", "subtype": "secret text", "is_error": True}], "an unknown subtype"),
        ([SUCCESS | {"result": " \n"}], "no council report"),
        ([SUCCESS | {"result": "Cleanup complete. secret"}], "no council report"),
        ([said("### secret Report"), SUCCESS | {"result": "secret"}], "no council report"),
        ([SUCCESS | {"result": "## Review Council Report"}], "no council report"),
        ([SUCCESS | {"result": "## Review Council Report\n \n"}], "no council report"),
        ([SUCCESS], "malformed"),
        ([{"type": "assistant", "message": {"content": "secret"}}, SUCCESS | {"result": REPORT}],
         "malformed"),
    ],
)  # fmt: skip
def test_session_without_a_report_fails_without_quoting_it(lines, message):
    with pytest.raises(ReviewCouncilOutputError, match=message) as e:
        review_council.report(events(*lines))
    assert "secret" not in str(e.value)


MALFORMED = "claude: stdout holds a malformed, truncated or ambiguous stream-json event"
GOOD = events(said(REPORT), SUCCESS | {"result": REPORT})


@pytest.mark.parametrize(
    "stdout",
    [
        GOOD.replace('"is_error": false', '"is_error": true, "is_error": false'),
        GOOD + '{"type": "assistant", "message": {"content": [{"secret',  # truncated
        GOOD.replace("\n", "\nsecret\n", 1),  # a non-event line among the events
        GOOD.replace('{"type": "assistant", ', "{", 1),  # an event with no type
        GOOD.replace("\n", "\n\n", 1),
        '{"type": "system", "bad": NaN}\n' + GOOD,
        '{"type": "system", "bad": -Infinity}\n' + GOOD,
    ],
)
def test_malformed_or_ambiguous_stream_is_refused_with_a_fixed_diagnostic(stdout):
    with pytest.raises(ReviewCouncilOutputError) as e:
        review_council.report(stdout)
    assert str(e.value) == MALFORMED


def test_a_later_bare_heading_does_not_displace_an_earlier_report():
    stdout = events(said(REPORT), SUCCESS | {"result": "Done.\n\n## Review Council Report\n"})

    assert review_council.report(stdout) == REPORT


def test_events_are_split_on_newlines_only():
    text = REPORT + "line\u2028separator\u0085next\x0cpage\x1crecord"

    assert review_council.report(events(SUCCESS | {"result": text})) == text


def test_image_plugin_is_the_pinned_commit_installed_under_its_name():
    assert review_council.plugin("0.14.0") == (
        "https://github.com/deployhq/review-council",
        "c9a73575503c12277cd46409c7cbdbe452e7c582",
        "0.14.0",
    )
    assert review_council.PLUGIN_DIR == "/opt/rbench/plugins/review-council"
    with pytest.raises(ValueError, match="no release"):
        review_council.plugin("0.13.0")
