import json
import random
import re
import tomllib
from pathlib import Path

import pytest
from test_tasks import HIDDEN, write_task

from rbench.cli import main
from rbench.engines.declaration import load_engine, parse_engine
from rbench.matrix.expansion import Configuration, RoleSetting
from rbench.plan.estimate import (
    DEFAULT_ENGINE_TOKENS,
    DEFAULT_JUDGE_TOKENS,
    DEFAULT_NORMALIZATION_TOKENS,
    SmokeError,
    load_smoke,
    parse_smoke,
    suggested_budget,
    trial_estimate,
)
from rbench.tasks import HIDDEN_CONFIG_FILE, HIDDEN_DIR_ENV, HIDDEN_ID_PREFIX

PROMPT = "ab" * 32
COMMIT = "0123456789abcdef0123456789abcdef01234567"
ENGINES = {
    "plain": f"""
name = "plain"
pin = {{ kind = "sha256", ref = "{PROMPT}" }}
cli = {{ claude = "2.1.316", codex = "0.167.0" }}
[roles.reviewer]
delivery = "flag"
default = {{ model = "claude-opus-5-5", effort = "high" }}
""",
    "panel": f"""
name = "panel"
pin = {{ kind = "git", ref = "{COMMIT}" }}
cli = {{ claude = "2.1.316", codex = "0.167.0" }}
[roles.coordinator]
cli = "claude"
delivery = "flag"
default = {{ model = "claude-opus-5-5", effort = "high" }}
[roles.reviewers]
cli = "codex"
delivery = "flag"
default = {{ model = "gpt-6.2-sol", effort = "high" }}
""",
}
SCREENING = """
stage = "screening"
tasks = "core"
attempts = 1

[[rounds]]
name = "r1"
budget = { claude = BUDGET_CLAUDE, codex = BUDGET_CODEX }
[[rounds.entries]]
engine = "plain"
roles.reviewer = { model = ["claude-opus-5-5", "gpt-6.2-sol"], effort = "high" }
[[rounds.entries]]
engine = "panel"
default = true

[[rounds]]
name = "r2"
budget = { claude = "undecided" }
[[rounds.entries]]
engine = "plain"
roles.reviewer = { model = "claude-sonnet-5-5", effort = "medium" }
"""

DECLARED = {name: parse_engine(tomllib.loads(text), name) for name, text in ENGINES.items()}


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def manifest(task_set: str, ids: list[str]) -> str:
    tasks = {i: {"sha256": "0" * 64} for i in ids}
    changed = {i: "0" * 64 for i in ids}
    log = [{"version": 1, "note": "synthetic", "changed": changed}]
    return json.dumps({"set": task_set, "version": 1, "tasks": tasks, "changelog": log})


@pytest.fixture
def bench(tmp_path: Path) -> Path:
    for name, text in ENGINES.items():
        write(tmp_path / "engines" / f"{name}.toml", text)
    core = {"version": 3, "tasks": [f"task-{i}" for i in range(10)]}
    write(tmp_path / "tasks" / "core.json", json.dumps(core))
    write(tmp_path / "tasks" / "reviewbench" / "manifest.json", manifest("reviewbench", ["rb-1"]))
    write(tmp_path / "tasks" / "ours" / "manifest.json", manifest("ours", ["o-1", "o-2"]))
    return tmp_path


def screening(bench: Path, claude: object = "undecided", codex: object = "undecided") -> Path:
    text = SCREENING.replace("BUDGET_CLAUDE", json.dumps(claude))
    return write(bench / "screening.toml", text.replace("BUDGET_CODEX", json.dumps(codex)))


def plan(bench: Path, matrix: Path, *extra: str) -> list[str]:
    root = ["--engines", str(bench / "engines"), "--tasks-root", str(bench / "tasks")]
    return ["plan", str(matrix), *root, *extra]


def default_config(bench: Path, name: str) -> Configuration:
    e = load_engine(bench / "engines" / f"{name}.toml")
    roles = sorted(e.default_configuration().items())
    return Configuration(
        e.name, e.version, tuple((r, RoleSetting((s.model,), s.effort)) for r, s in roles)
    )


def plain(bench: Path, model: str, effort: str) -> Configuration:
    e = load_engine(bench / "engines" / "plain.toml")
    return Configuration("plain", e.version, (("reviewer", RoleSetting((model,), effort)),))


def smoke_file(bench: Path, configurations: dict) -> Path:
    return write(bench / "smoke.json", json.dumps({"version": 1, "configurations": configurations}))


def measured(bench: Path) -> dict:
    """Smoke medians of both engines' defaults."""
    return {
        default_config(bench, "plain").id: {
            "engine": "plain",
            "tokens": {"engine": {"claude": 1000}, "normalization": {}, "judge": {"claude": 100}},
        },
        default_config(bench, "panel").id: {
            "engine": "panel",
            "tokens": {
                "engine": {"claude": 2000, "codex": 3000},
                "normalization": {"claude": 50},
                "judge": {"claude": 200},
            },
        },
    }


def row(out: str, round_: str, provider: str) -> list[str]:
    """The table row of `provider` under `round <round_>:`."""
    section = out.split(f"round {round_}:")[1]
    return next(line.split() for line in section.splitlines() if line.split()[:1] == [provider])


# Estimate uses smoke medians when given a smoke run, a documented default otherwise


def test_smoke_median_of_the_same_configuration(bench):
    smoke = parse_smoke(
        json.loads(smoke_file(bench, measured(bench)).read_text()), "smoke.json", DECLARED
    )
    config = default_config(bench, "panel")

    tokens, source = trial_estimate(config, smoke)

    assert source == "smoke"
    assert tokens["engine"] == {"claude": 2000, "codex": 3000}


def test_unmeasured_configuration_takes_its_engines_median_on_its_own_provider(bench):
    smoke = parse_smoke(
        json.loads(smoke_file(bench, measured(bench)).read_text()), "smoke.json", DECLARED
    )
    gpt = plain(bench, "gpt-6.2-sol", "high")

    tokens, source = trial_estimate(gpt, smoke)

    assert source == f"smoke: {default_config(bench, 'plain').id}, split inferred"
    assert tokens == {"engine": {"codex": 1000}, "normalization": {}, "judge": {"claude": 100}}


def test_engine_tokens_split_evenly_when_providers_differ(bench):
    data = measured(bench)
    data[default_config(bench, "plain").id]["tokens"]["engine"] = {"claude": 1001}
    smoke = parse_smoke({"version": 1, "configurations": data}, "smoke.json", DECLARED)
    two = Configuration(
        "plain", "x", (("reviewer", RoleSetting(("claude-opus-5-5", "gpt-6.2-sol"), "high")),)
    )

    tokens, source = trial_estimate(two, smoke)

    assert tokens["engine"] == {"claude": 501, "codex": 501}
    assert source == f"smoke: {default_config(bench, 'plain').id}, split inferred"


def test_no_smoke_run_uses_the_labelled_default(bench, capsys):
    assert main(plan(bench, screening(bench), "--round", "r1")) == 0

    out = capsys.readouterr().out
    assert "estimate: DEFAULT, no smoke run given" in out
    assert f"DEFAULT per trial: engine {DEFAULT_ENGINE_TOKENS}" in out
    # r1: plain on claude and on codex, panel on both; 10 core tasks, 1 attempt
    trials = 3 * 10
    claude_engine = 2 * DEFAULT_ENGINE_TOKENS * 10
    claude_norm = DEFAULT_NORMALIZATION_TOKENS["claude"] * 10  # panel only; plain has none
    claude_judge = DEFAULT_JUDGE_TOKENS["claude"] * trials
    assert row(out, "r1", "claude")[:6] == [
        "claude",
        str(trials),  # every trial is judged on claude
        str(claude_engine),
        str(claude_norm),
        str(claude_judge),
        str(claude_engine + claude_norm + claude_judge),
    ]
    assert all(
        line.endswith("DEFAULT") for line in out.split("(estimate source):")[1].splitlines()[1:4]
    )


def test_smoke_run_numbers_replace_the_default(bench, capsys):
    smoke = smoke_file(bench, measured(bench))

    assert main(plan(bench, screening(bench), "--round", "r1", "--smoke", str(smoke))) == 0

    out = capsys.readouterr().out
    assert f"estimate: smoke medians from {smoke}" in out
    assert "DEFAULT" not in out
    # per trial on codex: plain-gpt 1000 (engine median moved), panel 3000; x 10 tasks
    assert row(out, "r1", "codex")[:6] == ["codex", "20", "40000", "0", "0", "40000"]


def test_engine_missing_from_smoke_run_is_labelled_default(bench, capsys):
    only_plain = {k: v for k, v in measured(bench).items() if v["engine"] == "plain"}
    smoke = smoke_file(bench, only_plain)

    assert main(plan(bench, screening(bench), "--round", "r1", "--smoke", str(smoke))) == 0

    out = capsys.readouterr().out
    assert "1 configuration(s) use the DEFAULT estimate" in out
    assert row(out, "r1", default_config(bench, "panel").id) == [
        default_config(bench, "panel").id,
        "DEFAULT",
    ]


# Round plan compares the estimate with the round budget


def test_round_over_budget_is_refused(bench, capsys):
    matrix = screening(bench, claude=10**9, codex=1)

    assert main(plan(bench, matrix, "--round", "r1")) == 1

    captured = capsys.readouterr()
    assert row(captured.out, "r1", "codex")[-2:] == ["OVER", "BUDGET"]
    assert row(captured.out, "r1", "claude")[-2:] == ["within", "budget"]
    assert "refusing round 'r1': the codex estimate" in captured.err
    assert "claude estimate" not in captured.err


def test_round_within_budget_passes_and_shows_o6_budget(bench, capsys):
    matrix = screening(bench, claude=10**9, codex=10**9)

    assert main(plan(bench, matrix, "--round", "r1")) == 0

    claude = row(capsys.readouterr().out, "r1", "claude")
    assert claude[6] == str(10**9)
    assert claude[7] == str(suggested_budget(int(claude[5])))


def test_undecided_budget_is_shown_not_refused(bench, capsys):
    assert main(plan(bench, screening(bench), "--round", "r2")) == 0

    out = capsys.readouterr().out
    assert row(out, "r2", "claude")[6:] == [
        "undecided",
        row(out, "r2", "claude")[7],
        "budget",
        "undecided",
    ]
    assert "round r1" not in out


def test_whole_plan_refuses_any_round_over_budget(bench, capsys):
    assert main(plan(bench, screening(bench, claude=1, codex=10**9))) == 1

    out = capsys.readouterr().out
    assert "round r1:" in out and "round r2:" in out
    assert "all rounds: 40 trials" in out


CODEX_ONLY = """
stage = "screening"
tasks = "core"
attempts = 1
[[rounds]]
name = "r1"
budget = BUDGET
[[rounds.entries]]
engine = "plain"
roles.reviewer = { model = "gpt-6.2-sol", effort = "high" }
"""


def test_judge_provider_is_budgeted_and_thresholded(bench, capsys):
    # Review repro: a Codex-only round still spends Claude judge tokens.
    budget = "{ claude = 1, codex = 4000000 }"
    matrix = write(bench / "codex.toml", CODEX_ONLY.replace("BUDGET", budget))

    assert main(plan(bench, matrix)) == 1

    captured = capsys.readouterr()
    judge = DEFAULT_JUDGE_TOKENS["claude"] * 10
    assert row(captured.out, "r1", "claude")[:6] == [
        "claude",
        "10",
        "0",
        "0",
        str(judge),
        str(judge),
    ]
    assert row(captured.out, "r1", "claude")[-2:] == ["OVER", "BUDGET"]
    assert "  claude: not enforced; its CLI reports no used share" in captured.out
    assert f"the claude estimate {judge} exceeds its budget 1" in captured.err


def test_provider_without_a_budget_refuses_the_round(bench, capsys):
    matrix = write(bench / "codex.toml", CODEX_ONLY.replace("BUDGET", "{ codex = 4000000 }"))

    assert main(plan(bench, matrix)) == 1

    captured = capsys.readouterr()
    assert row(captured.out, "r1", "claude")[-4:] == ["none", "720000", "NO", "BUDGET"]
    assert "the claude estimate 600000 has no claude budget in the round" in captured.err
    assert "codex" not in captured.err


def test_budget_margin_rounds_up():
    assert suggested_budget(100) == 120
    assert suggested_budget(101) == 122  # 121.2 rounded up


# Providers without used-share reporting are named as not enforced


def test_soft_threshold_not_enforced_for_claude(bench, capsys):
    assert main(plan(bench, screening(bench), "--round", "r1")) == 0

    out = capsys.readouterr().out
    assert "soft threshold: 80% of every subscription window the CLI reports" in out
    assert "  claude: not enforced; its CLI reports no used share" in out
    assert "  codex: no new trial once the 5-hour or weekly window is 80% used" in out


def test_soft_threshold_lists_only_planned_providers(bench, capsys):
    assert main(plan(bench, screening(bench), "--round", "r2")) == 0

    out = capsys.readouterr().out
    assert "claude: not enforced" in out
    assert "codex:" not in out.split("soft threshold:")[1]


# Output is deterministic for the same inputs


def test_output_is_deterministic(bench, capsys):
    data = measured(bench)
    items = list(data.items())
    random.Random(1).shuffle(items)
    argv = plan(bench, screening(bench, claude=5, codex=5), "--smoke")

    outputs = []
    for configurations in (data, dict(reversed(items)), dict(items)):
        main([*argv, str(smoke_file(bench, configurations))])
        outputs.append(capsys.readouterr())

    assert outputs[0].out and all(o == outputs[0] for o in outputs)


# Inputs


def test_task_count_from_core_list_and_stage(bench, capsys):
    smoke_matrix = write(
        bench / "smoke.toml",
        'stage = "smoke"\ntasks = "core"\nmax_tasks = 1\nattempts = 1\n'
        '[[rounds]]\nname = "defaults"\nbudget = { claude = "undecided", codex = "undecided" }\n'
        '[[rounds.entries]]\nengine = "*"\ndefault = true\n',
    )

    assert main(plan(bench, smoke_matrix)) == 0

    out = capsys.readouterr().out
    assert "stage smoke, 1 tasks (core list v3), 1 attempt per task" in out
    assert "round defaults: 2 trials (2 configurations x 1 tasks x 1 attempt)" in out


FULL = (
    'stage = "full"\ntasks = "all"\nattempts = 3\n'
    '[[rounds]]\nname = "f1"\nbudget = { claude = "undecided" }\n'
    '[[rounds.entries]]\nengine = "plain"\ndefault = true\n'
)


def test_full_stage_counts_every_set_including_hidden(bench, capsys, monkeypatch):
    private = bench / "private"
    write(private / HIDDEN_CONFIG_FILE, f'canary_prefix = "{HIDDEN}"\n')
    for i in range(4):
        write_task(private, f"{HIDDEN_ID_PREFIX}{i}", "hidden")
    monkeypatch.setenv(HIDDEN_DIR_ENV, str(private))

    assert main(plan(bench, write(bench / "full.toml", FULL))) == 0

    out = capsys.readouterr().out
    assert "7 tasks (sets reviewbench, ours, hidden), 3 attempts per task" in out
    assert "round f1: 21 trials" in out
    assert HIDDEN_ID_PREFIX + "0" not in out and str(private) not in out


def test_full_stage_without_hidden_set_refuses_and_says_how(bench, capsys, monkeypatch):
    monkeypatch.delenv(HIDDEN_DIR_ENV, raising=False)

    assert main(plan(bench, write(bench / "full.toml", FULL))) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"set {HIDDEN_DIR_ENV} to the hidden task directory" in captured.err


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--round", "nope"], "no round 'nope'; rounds: r1, r2"),
        (["--smoke", "missing.json"], "missing.json"),
    ],
)
def test_bad_input_fails_with_a_reason(bench, capsys, extra, message):
    assert main(plan(bench, screening(bench), *extra)) == 1
    assert message in capsys.readouterr().err


def test_matrix_without_rounds_fails(bench, capsys):
    empty = write(
        bench / "empty.toml", 'stage = "screening"\ntasks = "core"\nattempts = 1\nrounds = []\n'
    )

    assert main(plan(bench, empty)) == 1
    assert "no rounds; rounds: none" in capsys.readouterr().err


def test_planning_runs_nothing(bench, fake_bin, capsys):
    smoke = smoke_file(bench, measured(bench))

    assert main(plan(bench, screening(bench), "--smoke", str(smoke))) == 0
    assert fake_bin.calls() == []


# Smoke medians file validation

GOOD = {"engine": "plain", "tokens": {"engine": {"claude": 1}, "normalization": {}, "judge": {}}}
ID = "plain-claude-opus-5-5-high-" + "0" * 64


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "expected an object"),
        ({"version": 2, "configurations": {ID: GOOD}}, "version: expected 1"),
        ({"version": True, "configurations": {ID: GOOD}}, "version: expected 1"),
        ({"version": 1, "configurations": {}}, "non-empty object"),
        ({"version": 1, "configurations": {ID: GOOD}, "extra": 1}, "unknown extra"),
        (
            {"version": 1, "configurations": {ID: GOOD | {"engine": "panel"}}},
            "of engine 'panel'",
        ),
        ({"version": 1, "configurations": {ID: {"engine": "plain"}}}, "missing tokens"),
        (
            {"version": 1, "configurations": {ID: GOOD | {"tokens": {"engine": {}, "judge": {}}}}},
            "missing normalization",
        ),
        (
            {
                "version": 1,
                "configurations": {
                    ID: GOOD | {"tokens": {"engine": [], "normalization": {}, "judge": {}}}
                },
            },
            "provider -> tokens",
        ),
        (
            {
                "version": 1,
                "configurations": {
                    ID: GOOD
                    | {"tokens": {"engine": {"gemini": 1}, "normalization": {}, "judge": {}}}
                },
            },
            "unknown provider 'gemini'",
        ),
        (
            {
                "version": 1,
                "configurations": {
                    ID: GOOD
                    | {"tokens": {"engine": {"claude": -1}, "normalization": {}, "judge": {}}}
                },
            },
            "expected a token count",
        ),
        (
            {
                "version": 1,
                "configurations": {
                    ID: GOOD
                    | {"tokens": {"engine": {"claude": 1.5}, "normalization": {}, "judge": {}}}
                },
            },
            "expected a token count",
        ),
        (
            {"version": 1, "configurations": {ID: GOOD, ID.replace("high", "low"): GOOD}},
            "engine 'plain' is already measured",
        ),
    ],
)
def test_malformed_smoke_medians_are_rejected(data, message):
    with pytest.raises(SmokeError, match="^smoke.json") as error:
        parse_smoke(data, "smoke.json", DECLARED)
    assert message in str(error.value)


H = "0" * 64
PLUS = f"plain-+-{H}"
GPT = f"plain-gpt-6.2-sol-high-{H}"
PAIR = ("claude-opus-5-5", "gpt-6.2-sol")
TWO = f"plain-{'+'.join(PAIR)}-high-{H}"


def bad(config_id: str = ID, **record) -> dict:
    tokens = {"engine": {"claude": 1}, "normalization": {}, "judge": {}} | record.pop("tokens", {})
    return {
        "version": 1,
        "configurations": {config_id: {"engine": "plain", "tokens": tokens} | record},
    }


@pytest.mark.parametrize(
    ("data", "config_id", "message"),
    [
        (bad("plain-"), "plain-", "not a configuration id of engine 'plain'"),
        (bad("plain-claude-opus-5-5-high"), "plain-claude-opus-5-5-high", "64 lowercase hex"),
        (bad(ID[:-1]), ID[:-1], "64 lowercase hex"),
        (bad(ID[:-1] + "A"), ID[:-1] + "A", "64 lowercase hex"),
        (bad(ID.replace("plain", "panel")), ID.replace("plain", "panel"), "of engine 'plain'"),
        (bad(engine=7), ID, "engine: expected an engine name"),
        (bad(engine="Plain"), ID, "engine: expected an engine name"),
        (bad(tokens={"engine": {}}), ID, "no engine usage measured"),
        (bad(tokens={"engine": {"claude": 0, "codex": 0}}), ID, "no engine usage measured"),
        # Readable names no configuration of the engine has (review round 2, finding 1).
        (bad(PLUS), PLUS, "of engine 'plain'"),
        (bad(f"plain-claude-opus-5-5-{H}"), f"plain-claude-opus-5-5-{H}", "of engine 'plain'"),
        (bad(f"plain-opus-high-{H}"), f"plain-opus-high-{H}", "of engine 'plain'"),
        (bad(f"plain-claude-opus-5-5-ultra-{H}"), "plain-claude-opus-5-5-ultra-", "of engine"),
        (bad(TWO.replace("opus", "fable")), "plain-claude-fable", "of engine"),
        (bad(f"plain-{'+'.join(reversed(PAIR))}-high-{H}"), "plain-gpt", "of engine"),
        (bad(f"plain-claude-opus-5-5+claude-opus-5-5-high-{H}"), "plain-claude", "of engine"),
        (bad(f"panel-claude-opus-5-5-high-{H}", engine="panel"), "panel-claude", "of engine"),
        (bad(engine="other"), ID, "engine 'other' is not declared"),
        # Engine usage on a provider the id's roles do not run on (finding 2).
        (bad(GPT), GPT, "usage on claude, which the configuration's roles do not run on (codex)"),
    ],
)
def test_each_malformed_smoke_field_rejects_the_file_naming_the_configuration(
    data, config_id, message
):
    data["configurations"]["plain-claude-sonnet-5-5-high-" + "1" * 64] = GOOD
    with pytest.raises(
        SmokeError, match=f"^smoke.json: configuration '{re.escape(config_id)}"
    ) as error:
        parse_smoke(data, "smoke.json", DECLARED)
    assert message in str(error.value)


def test_malformed_smoke_record_cannot_zero_a_round(bench, capsys):
    # Review repro: an id without a hash and an empty engine map once estimated zero tokens.
    matrix = write(bench / "codex.toml", CODEX_ONLY.replace("BUDGET", "{ claude = 1, codex = 1 }"))
    empty = {"engine": "plain", "tokens": {"engine": {}, "normalization": {}, "judge": {}}}
    smoke = smoke_file(bench, {"plain-": empty})

    assert main(plan(bench, matrix, "--smoke", str(smoke))) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "configuration 'plain-'" in captured.err


def test_smoke_ids_of_every_engine_form_are_accepted(bench):
    panel = default_config(bench, "panel")
    records = {
        TWO: {
            "engine": "plain",
            "tokens": {"engine": {"codex": 5}, "normalization": {}, "judge": {}},
        },
        panel.id: {
            "engine": "panel",
            "tokens": {"engine": {"codex": 5}, "normalization": {}, "judge": {}},
        },
    }

    smoke = parse_smoke({"version": 1, "configurations": records}, "smoke.json", DECLARED)

    assert set(smoke.configurations) == {TWO, panel.id}


def round_two(bench: Path, budget: str, model: str = "gpt-6.2-sol") -> Path:
    text = CODEX_ONLY.replace("BUDGET", budget).replace("gpt-6.2-sol", model)
    return write(bench / "round2.toml", text)


def smoke_record(bench: Path, config_id: str, engine: dict, normalization: dict) -> Path:
    tokens = {"engine": engine, "normalization": normalization, "judge": {"claude": 100}}
    return smoke_file(bench, {config_id: {"engine": "plain", "tokens": tokens}})


def test_impossible_smoke_id_is_refused(bench, capsys):
    # Review round 2 repro 1: `plain-+-<hex>` once stood in for every plain configuration.
    smoke = smoke_record(bench, PLUS, {"claude": 1}, {})

    matrix = round_two(bench, "{ claude = 1000000, codex = 10 }")
    assert main(plan(bench, matrix, "--smoke", str(smoke))) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"configuration {PLUS!r}: not a configuration id of engine 'plain'" in captured.err


def test_exact_smoke_record_on_the_wrong_provider_is_refused(bench, capsys):
    # Review round 2 repro 2: a Codex plain configuration's engine tokens recorded on Claude.
    config_id = plain(bench, "gpt-6.2-sol", "high").id
    smoke = smoke_record(bench, config_id, {"claude": 1000}, {})

    matrix = round_two(bench, "{ claude = 1000000, codex = 10 }")
    assert main(plan(bench, matrix, "--smoke", str(smoke))) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"configuration {config_id!r}: tokens: engine: usage on claude" in captured.err


def test_zero_estimate_asks_for_no_budget(bench, capsys):
    # Review round 2 repro 3: a zero normalization entry on Codex once demanded a Codex budget.
    config_id = plain(bench, "claude-opus-5-5", "high").id
    smoke = smoke_record(bench, config_id, {"claude": 1000}, {"codex": 0})

    matrix = round_two(bench, "{ claude = 1000000 }", model="claude-opus-5-5")
    assert main(plan(bench, matrix, "--smoke", str(smoke))) == 0

    out = capsys.readouterr().out
    assert row(out, "r1", "claude")[:3] == ["claude", "10", "10000"]
    assert "codex" not in out


def test_smoke_file_with_repeated_key_is_rejected(tmp_path):
    path = write(tmp_path / "smoke.json", '{"version": 1, "version": 1, "configurations": {}}')

    with pytest.raises(SmokeError, match="repeated key 'version'"):
        load_smoke(path, DECLARED)
