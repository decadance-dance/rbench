import tomllib
from pathlib import Path

import pytest

from rbench.engines.declaration import parse_engine
from rbench.engines.pins import FloatingRefError
from rbench.matrix.expansion import (
    UNDECIDED,
    MatrixError,
    RoleSetting,
    can_reuse,
    load_matrix,
    parse_matrix,
)

MATRICES = Path(__file__).parents[1] / "matrices"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
PROMPT = "ab" * 32
CLI = {"claude": "2.1.316", "codex": "0.167.0"}


def flag(model, effort, cli=None):
    role = {"delivery": "flag", "default": {"model": model, "effort": effort}}
    return role | ({"cli": cli} if cli else {})


def engines(prompt=PROMPT):
    """Synthetic engines: a one-role baseline and a three-role panel engine."""
    plain = {
        "name": "plain",
        "pin": {"kind": "sha256", "ref": prompt},
        "cli": CLI,
        "roles": {"reviewer": flag("claude-opus-5-5", "high")},
    }
    panel = {
        "name": "panel",
        "pin": {"kind": "git", "ref": COMMIT},
        "cli": CLI,
        "roles": {
            "coordinator": flag("claude-opus-5-5", "high", "claude"),
            "judge": {
                "cli": "codex",
                "delivery": "config",
                "default": {"model": "gpt-6.2-sol", "effort": "medium"},
            },
            "reviewers": flag("gpt-6.2-sol", "high", "codex"),
        },
    }
    return {d["name"]: parse_engine(d, "engines") for d in (plain, panel)}


def matrix(*entries, stage="screening", budget=None, engines_=None, **top):
    data = {"stage": stage, "tasks": "core", "attempts": 1} | top
    data["rounds"] = [
        {
            "name": "r1",
            "budget": budget or {"claude": 1000, "codex": UNDECIDED},
            "entries": list(entries),
        }
    ]
    return parse_matrix(data, "m.toml", engines() if engines_ is None else engines_)


def configs(*entries, **kw):
    return matrix(*entries, **kw).rounds[0].configurations


def roles(config):
    return dict(config.roles)


# Matrix expansion


def test_model_and_effort_lists_expand_as_a_product_within_the_entry():
    models = ["claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5", "gpt-6.2-sol"]
    efforts = ["low", "medium", "high", "xhigh"]
    got = configs({"engine": "plain", "roles": {"reviewer": {"model": models, "effort": efforts}}})

    assert len(got) == 16
    assert {roles(c)["reviewer"] for c in got} == {
        RoleSetting((m,), e) for m in models for e in efforts
    }
    assert len({c.id for c in got}) == 16


def test_lists_are_never_crossed_with_other_entries():
    coordinators = {"coordinator": {"model": ["claude-opus-5-5", "claude-sonnet-5-5"]}}
    judges = {"judge": {"model": ["gpt-6.2-sol", "gpt-6.2-astra"], "effort": "high"}}
    got = configs({"engine": "panel", "roles": coordinators}, {"engine": "panel", "roles": judges})

    assert len(got) == 4
    first, second = got[:2], got[2:]
    assert all(roles(c)["judge"] == RoleSetting(("gpt-6.2-sol",), "medium") for c in first)
    assert all(roles(c)["coordinator"].models == ("claude-opus-5-5",) for c in second)


def test_lists_in_several_roles_of_one_entry_expand_together():
    entry = {
        "coordinator": {"effort": ["medium", "high"]},
        "judge": {"model": ["gpt-6.2-sol", "gpt-6.2-astra"]},
    }
    assert len(configs({"engine": "panel", "roles": entry})) == 4


def test_models_set_does_not_expand_and_its_order_does_not_matter():
    a = configs(
        {"engine": "panel", "roles": {"reviewers": {"models": ["gpt-6.2-sol", "gpt-6.2-astra"]}}}
    )
    b = configs(
        {"engine": "panel", "roles": {"reviewers": {"models": ["gpt-6.2-astra", "gpt-6.2-sol"]}}}
    )

    assert len(a) == 1
    assert roles(a[0])["reviewers"] == RoleSetting(("gpt-6.2-astra", "gpt-6.2-sol"), "high")
    assert a[0].id == b[0].id


def test_models_with_an_effort_list_expands_only_by_effort():
    entry = {"reviewers": {"models": ["gpt-6.2-sol", "gpt-6.2-astra"], "effort": ["low", "high"]}}
    got = configs({"engine": "panel", "roles": entry})

    assert [roles(c)["reviewers"].effort for c in got] == ["low", "high"]


def test_unlisted_roles_and_fields_keep_the_engine_default():
    (got,) = configs({"engine": "panel", "roles": {"coordinator": {"effort": "max"}}})

    assert roles(got) == {
        "coordinator": RoleSetting(("claude-opus-5-5",), "max"),
        "judge": RoleSetting(("gpt-6.2-sol",), "medium"),
        "reviewers": RoleSetting(("gpt-6.2-sol",), "high"),
    }


def test_default_entry_uses_every_role_default():
    (got,) = configs({"engine": "panel", "default": True})

    declared = engines()["panel"].default_configuration()
    assert roles(got) == {r: RoleSetting((s.model,), s.effort) for r, s in declared.items()}


@pytest.mark.parametrize(
    "role",
    [{"model": "opus"}, {"model": ["claude-opus-5-5", "sonnet"]}],
    ids=["model", "model-list"],
)
def test_alias_is_rejected_asking_for_the_full_id(role):
    with pytest.raises(FloatingRefError, match="full model id") as e:
        configs({"engine": "plain", "roles": {"reviewer": role}})
    assert "m.toml: round 'r1': entry 1: role 'reviewer'" in str(e.value)


def test_alias_in_a_models_set_is_rejected():
    entry = {"reviewers": {"models": ["gpt-6.2-sol", "gpt"]}}
    with pytest.raises(FloatingRefError, match="'gpt' is an alias"):
        configs({"engine": "panel", "roles": entry})


def test_engine_checks_apply_to_overrides():
    with pytest.raises(ValueError, match="runs on claude, model 'gpt-6.2-sol' on codex"):
        configs({"engine": "panel", "roles": {"coordinator": {"model": "gpt-6.2-sol"}}})
    with pytest.raises(ValueError, match="effort 'ultra'"):
        configs({"engine": "plain", "roles": {"reviewer": {"effort": "ultra"}}})


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"engine": "nope", "default": True}, "unknown engine 'nope'; declared: panel, plain"),
        ({"engine": "plain", "roles": {"author": {}}}, "roles: unknown author"),
        ({"engine": "plain"}, "either default = true or roles"),
        ({"engine": "plain", "default": True, "roles": {}}, "either default = true or roles"),
        ({"engine": "plain", "default": False}, "default must be true"),
        ({"engine": "plain", "roles": {}}, "list at least one role"),
        ({"engine": "*", "roles": {"reviewer": {}}}, "only allowed with default = true"),
        ({"engine": "plain", "roles": {"reviewer": {"model": []}}}, "non-empty list"),
        ({"engine": "plain", "roles": {"reviewer": {"effort": ["high", "high"]}}}, "distinct"),
        ({"engine": "panel", "roles": {"reviewers": {"models": "gpt-6.2-sol"}}}, "a non-empty"),
        (
            {"engine": "panel", "roles": {"reviewers": {"model": "a", "models": ["b"]}}},
            "model or models, not both",
        ),
        ({"engine": "plain", "roles": {"reviewer": {"temperature": 1}}}, "unknown temperature"),
    ],
)
def test_malformed_entries_are_rejected(entry, message):
    with pytest.raises(MatrixError, match=message):
        configs(entry)


def test_wildcard_needs_declared_engines():
    with pytest.raises(MatrixError, match="no engine is declared"):
        configs({"engine": "*", "default": True}, engines_={})


def test_a_configuration_listed_twice_is_rejected():
    explicit = {"engine": "plain", "roles": {"reviewer": {"model": "claude-opus-5-5"}}}
    with pytest.raises(MatrixError, match=r"entry 2: configuration plain-.* repeats .*entry 1"):
        configs({"engine": "plain", "default": True}, explicit)


# Stable configuration ids


def test_id_is_a_readable_name_and_a_settings_hash():
    (one,) = configs({"engine": "plain", "default": True})
    (panel,) = configs({"engine": "panel", "default": True})

    name, digest = one.id.rsplit("-", 1)
    assert name == "plain-claude-opus-5-5-high"
    assert digest == one.hash and len(digest) == 64
    assert panel.id.startswith(
        "panel-coordinator-claude-opus-5-5-high-judge-gpt-6.2-sol-medium-"
        "reviewers-gpt-6.2-sol-high-"
    )


def test_settings_that_share_a_short_hash_prefix_get_different_ids():
    # These two versions give settings hashes with the same first 12 hex digits.
    def only_config(build):
        engine = parse_engine(
            {
                "name": "panel",
                "pin": {"kind": "version", "ref": f"1.2.3+build{build}"},
                "cli": {"codex": "0.167.0"},
                "roles": {"reviewer": flag("gpt-6.2-sol", "high")},
            },
            "engine.toml",
        )
        (config,) = load_matrix(MATRICES / "smoke.toml", {"panel": engine}).rounds[0].configurations
        return config

    a, b = only_config(4723561), only_config(14928273)
    assert a.hash[:12] == b.hash[:12]
    assert a.id != b.id


def test_same_settings_give_the_same_id_across_matrix_files(tmp_path):
    smoke = tmp_path / "a.toml"
    smoke.write_text((MATRICES / "smoke.toml").read_text())
    other = tmp_path / "b.toml"
    other.write_text(
        'stage = "screening"\ntasks = "core"\nattempts = 1\n'
        '[[rounds]]\nname = "high"\nbudget = { claude = 5, codex = 5 }\n'
        '[[rounds.entries]]\nengine = "panel"\n'
        'roles.coordinator = { model = "claude-opus-5-5", effort = ["medium", "high"] }\n'
        '[[rounds.entries]]\nengine = "plain"\n'
        'roles.reviewer = { model = ["gpt-6.2-sol", "claude-opus-5-5"], effort = "high" }\n'
    )
    defaults = {c.engine: c.id for c in load_matrix(smoke, engines()).rounds[0].configurations}
    ids = {c.id for c in load_matrix(other, engines()).rounds[0].configurations}

    assert defaults["panel"] in ids
    assert defaults["plain"] in ids
    assert len(ids) == 4


def test_any_setting_change_changes_the_id():
    def ids(**kw):
        return [
            c.id
            for c in configs(*[{"engine": n, "default": True} for n in ("plain", "panel")], **kw)
        ]

    base = ids()
    # A new plain prompt hash is a new engine version (spec Plain baseline, Prompt edited).
    assert ids(engines_=engines(prompt="cd" * 32))[0] != base[0]
    (judge,) = configs({"engine": "panel", "roles": {"judge": {"effort": "high"}}})
    assert judge.id != base[1]
    assert judge.id.rsplit("-", 1)[1] != base[1].rsplit("-", 1)[1]


# Rounds


def test_rounds_keep_file_order_and_per_provider_budgets():
    data = {
        "stage": "screening",
        "tasks": "core",
        "attempts": 1,
        "rounds": [
            {
                "name": "second-name-first",
                "budget": {"claude": 300, "codex": UNDECIDED},
                "entries": [{"engine": "panel", "default": True}],
            },
            {
                "name": "alpha",
                "budget": {"claude": 100},
                "entries": [{"engine": "plain", "default": True}],
            },
        ],
    }
    got = parse_matrix(data, "m.toml", engines())

    assert [r.name for r in got.rounds] == ["second-name-first", "alpha"]
    assert got.rounds[0].budget == {"claude": 300, "codex": None}
    assert got.rounds[1].budget == {"claude": 100}
    assert [c.engine for c in got.rounds[1].configurations] == ["plain"]


@pytest.mark.parametrize(
    ("budget", "message"),
    [
        ({"claude": 100}, "no budget for codex, which it uses"),
        ({"claude": 0, "codex": 1}, "claude: expected a positive token count"),
        ({"claude": 1, "codex": "tbd"}, "codex: expected a positive token count"),
        ({"claude": True, "codex": 1}, "claude: expected a positive token count"),
        ({"claude": 1, "codex": 1, "gemini": 1}, "unknown gemini"),
    ],
)
def test_round_budget_is_checked_per_provider(budget, message):
    with pytest.raises(MatrixError, match=message):
        configs({"engine": "panel", "default": True}, budget=budget)


def test_round_names_are_unique_and_rounds_are_not_empty():
    round_ = {
        "name": "r",
        "budget": {"claude": 1},
        "entries": [{"engine": "plain", "default": True}],
    }
    base = {"stage": "screening", "tasks": "core", "attempts": 1}
    with pytest.raises(MatrixError, match="listed twice"):
        parse_matrix(base | {"rounds": [round_, round_ | {"entries": []}]}, "m", engines())
    with pytest.raises(MatrixError, match="non-empty list of entries"):
        parse_matrix(base | {"rounds": [round_ | {"entries": []}]}, "m", engines())


# Stages


def test_stage_fixes_tasks_and_attempts():
    with pytest.raises(MatrixError, match="attempts: the full stage needs 3, got 1"):
        matrix({"engine": "plain", "default": True}, stage="full", tasks="all")
    with pytest.raises(MatrixError, match="max_tasks: the smoke stage needs 1, got None"):
        matrix({"engine": "plain", "default": True}, stage="smoke")
    with pytest.raises(MatrixError, match="attempts: the screening stage needs 1, got True"):
        matrix({"engine": "plain", "default": True}, attempts=True)
    with pytest.raises(
        MatrixError, match="stage: expected one of smoke, screening, full, got 'final'"
    ):
        matrix({"engine": "plain", "default": True}, stage="final")


def test_smoke_stage_runs_defaults_only():
    with pytest.raises(MatrixError, match="smoke stage runs engine defaults only"):
        matrix(
            {"engine": "plain", "roles": {"reviewer": {"effort": "low"}}},
            stage="smoke",
            max_tasks=1,
        )


def test_smoke_matrix_runs_one_core_task_each_engine_default_one_attempt():
    declared = engines()
    smoke = load_matrix(MATRICES / "smoke.toml", declared)

    assert smoke.stage == "smoke"
    assert smoke.select_tasks(core=["c1", "c2", "c3"], all_tasks=["a1", "c1", "c2", "c3"]) == ["c1"]
    assert smoke.attempts == 1
    (round_,) = smoke.rounds
    assert [(c.engine, roles(c)) for c in round_.configurations] == [
        (n, {r: RoleSetting((s.model,), s.effort) for r, s in e.default_configuration().items()})
        for n, e in sorted(declared.items())
    ]
    assert round_.budget == {"claude": None, "codex": None}


def test_screening_and_full_files_fix_tasks_and_attempts():
    screening = load_matrix(MATRICES / "screening.toml", engines())
    full = load_matrix(MATRICES / "full.toml", engines())

    assert (screening.select_tasks(["c1", "c2"], ["a1", "c1", "c2"]), screening.attempts) == (
        ["c1", "c2"],
        1,
    )
    assert (full.select_tasks(["c1"], ["a1", "c1"]), full.attempts) == (["a1", "c1"], 3)


def test_each_matrix_file_is_named_after_its_stage():
    for path in sorted(MATRICES.glob("*.toml")):
        assert tomllib.loads(path.read_text())["stage"] == path.stem


def test_full_stage_never_reuses_trials_of_an_earlier_run():
    assert not can_reuse("full")
    assert can_reuse("screening")
    assert can_reuse("smoke")
    with pytest.raises(MatrixError, match="unknown stage 'final'"):
        can_reuse("final")
    with pytest.raises(MatrixError, match=r"unknown stage \['full'\]"):
        can_reuse(["full"])


def test_task_selection_must_not_be_empty():
    smoke = load_matrix(MATRICES / "smoke.toml", engines())
    with pytest.raises(MatrixError, match="the smoke stage selects core tasks, but there are none"):
        smoke.select_tasks([], ["a1"])


def test_smoke_stage_needs_a_round_with_every_engine_default():
    with pytest.raises(MatrixError, match="rounds: the smoke stage needs a round"):
        parse_matrix(
            {"stage": "smoke", "tasks": "core", "max_tasks": 1, "attempts": 1, "rounds": []},
            "smoke.toml",
            engines(),
        )
    with pytest.raises(MatrixError, match="round 'r1': .*every engine's default; missing panel"):
        matrix({"engine": "plain", "default": True}, stage="smoke", max_tasks=1)


@pytest.mark.parametrize(
    ("top", "entry", "message"),
    [
        (
            {"stage": ["screening"]},
            {"engine": "plain", "default": True},
            r"m.toml: stage: expected",
        ),
        (
            {},
            {"engine": {"name": "plain"}, "default": True},
            r"m.toml: round 'r1': entry 1: engine: expected an engine name",
        ),
        ({}, {"engine": ["plain"], "default": True}, r"entry 1: engine: expected an engine name"),
    ],
)
def test_non_scalar_selectors_are_rejected_with_their_location(top, entry, message):
    with pytest.raises(MatrixError, match=message):
        matrix(entry, **top)


def test_budget_for_refuses_an_undecided_or_absent_budget():
    (round_,) = matrix(
        {"engine": "plain", "default": True}, budget={"claude": 1000, "codex": UNDECIDED}
    ).rounds

    assert round_.budget_for("claude") == 1000
    with pytest.raises(MatrixError, match="round 'r1': the codex budget is still 'undecided'"):
        round_.budget_for("codex")
    (no_codex,) = matrix({"engine": "plain", "default": True}, budget={"claude": 5}).rounds
    with pytest.raises(MatrixError, match="round 'r1': no budget for codex"):
        no_codex.budget_for("codex")


def test_unreadable_matrix_names_the_file(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("stage = ")
    with pytest.raises(MatrixError, match="bad.toml: not a valid UTF-8 TOML file"):
        load_matrix(path, engines())
