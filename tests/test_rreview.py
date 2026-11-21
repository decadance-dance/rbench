"""The `rreview` engine (spec engine-configurations, Engine adapters; design D5, D6, D13)."""

import copy
import json
import os
import subprocess

import pytest
from conftest import REPO

from rbench.engines.declaration import DeclarationError, Setting, load_engine
from rbench.engines.rreview import RreviewOutputError, findings, launch
from rbench.matrix.expansion import MatrixError, parse_matrix

ENGINES = REPO / "engines"
COMMIT = "f71d6e171bf47312fd6e564b6cc6a3dcc2d1fe94"  # rreview tag 0.1.1
SOL, ASTRA, OPUS = "gpt-6.2-sol", "gpt-6.2-astra", "claude-opus-5-5"
# A finding as rreview 0.1.1 writes it (`coordinator::Finding`), synthetic.
FINDING = {
    "title": "Divides by a count that can be zero",
    "body": "The loop ends with count 0 on an empty list.",
    "file": "src/app.py",
    "start_line": 3,
    "end_line": 4,
    "priority": 1,
    "confidence_score": 0.7,
    "stage": "coordinator",
    "candidate_ids": ["reliability-1"],
}


def result(gated: list[dict], ungated: list[dict] | None = None) -> dict:
    """A `result.json` of rreview 0.1.1 (`result::ReviewResult`), synthetic."""
    return {
        "findings": gated,
        "ungated_findings": gated if ungated is None else ungated,
        "dropped_candidates": [
            {
                "id": "security-1",
                "title": "Praise, not a defect",
                "panelist": "security",
                "file": "src/app.py",
                "line_range": {"start": 9, "end": 9},
                "priority": 3,
                "confidence_score": 0.2,
                "claim": "c",
                "drop_reason": "refuted",
                "evidence": "e",
            }
        ],
        "overall_correctness": "patch is incorrect",
        "overall_explanation": "x",
        "review_process": "y",
        "base": "a" * 40,
        "head": "b" * 40,
        "passes": [
            {
                "role": "judge",
                "agent": "codex",
                "model": SOL,
                "effort": "high",
                "status": "completed",
                "duration_ms": 5,
            },
        ],
        "degraded": False,
        "sandbox": "external",
    }


def settings(roles: dict | None = None) -> dict:
    """The frozen settings of one rreview configuration of `roles` (matrix entry roles)."""
    engine = load_engine(ENGINES / "rreview.toml")
    entry = {"engine": "rreview", "roles": roles or {}}
    if roles is None:
        entry = {"engine": "rreview", "default": True}
    data = {"stage": "screening", "tasks": "core", "attempts": 1}
    data["rounds"] = [{"name": "r1", "budget": {"claude": 1, "codex": 1}, "entries": [entry]}]
    (r,) = parse_matrix(data, "m.toml", {"rreview": engine}).rounds
    (c,) = r.configurations
    return c.settings


def test_declaration_pins_the_release_commit_and_d13_roles():
    engine = load_engine(ENGINES / "rreview.toml")

    assert (engine.pin_kind, engine.version) == ("git", COMMIT)
    assert {name: role.cli for name, role in engine.roles.items()} == {
        "panel": "codex",
        "coordinator": "claude",
        "judge": "codex",
    }


@pytest.mark.parametrize(
    "edit, error",
    [
        (lambda t: t.replace(COMMIT, "a" * 40), "a release this adapter knows"),
        (lambda t: t.replace(f'kind = "git", ref = "{COMMIT}"', 'kind = "version", ref = "0.1.1"'),
         "a release this adapter knows"),
        (lambda t: t.replace("[roles.judge]", "[roles.verifier]"), "exactly the roles"),
        (lambda t: t.replace('name = "rreview"', 'name = "rreview"\noutput = "markdown"'), "structured"),
        (lambda t: t.replace('gpt-6.2-sol", effort = "high" }\n\n[roles.coord',
                             'gpt-6.2-sol", effort = "ultra" }\n\n[roles.coord'),
         "accepts no effort 'ultra' on codex"),
    ],
)  # fmt: skip
def test_declaration_is_refused_off_what_the_adapter_supports(tmp_path, edit, error):
    path = tmp_path / "rreview.toml"
    path.write_text(edit((ENGINES / "rreview.toml").read_text()))

    with pytest.raises(DeclarationError, match=error):
        load_engine(path)


def test_d13_matrix_is_twelve_configurations():
    engine = load_engine(ENGINES / "rreview.toml")
    entry = {
        "engine": "rreview",
        "roles": {
            "panel": {"model": [SOL, ASTRA], "effort": "high"},
            "coordinator": {"model": ["claude-fable-5-2", OPUS, "claude-sonnet-5-5"]},
            "judge": {"model": [SOL, ASTRA], "effort": "high"},
        },
    }
    data = {"stage": "screening", "tasks": "core", "attempts": 1}
    data["rounds"] = [{"name": "r1", "budget": {"claude": 1, "codex": 1}, "entries": [entry]}]
    (r,) = parse_matrix(data, "m.toml", {"rreview": engine}).rounds

    assert len({c.id for c in r.configurations}) == 12
    for c in r.configurations:
        launch(c.settings)


def test_default_launch_runs_every_role_as_planned_with_sandbox_external():
    built = launch(settings())

    argv = list(built.argv)
    assert argv[:5] == ["rreview", "reviewed", "--sandbox", "external", "--json"]
    pairs = {(argv[i], argv[i + 1]) for i in range(5, len(argv), 2)}
    assert len(pairs) == (len(argv) - 5) // 2 == 15  # agent, model, effort for each of 5 roles
    for role in ("intent_contracts", "reliability", "security", "judge"):
        assert {("--role-agent", f"{role}=codex"), ("--role-model", f"{role}={SOL}")} <= pairs
        assert ("--role-effort", f"{role}=high") in pairs
    assert {
        ("--role-agent", "coordinator=claude"),
        ("--role-model", f"coordinator={OPUS}"),
    } <= pairs
    assert built.requested == {
        "coordinator": ("claude", Setting(OPUS, "high")),
        "intent_contracts": ("codex", Setting(SOL, "high")),
        "judge": ("codex", Setting(SOL, "high")),
        "reliability": ("codex", Setting(SOL, "high")),
        "security": ("codex", Setting(SOL, "high")),
    }


def test_each_role_gets_its_own_model_and_effort():
    built = launch(
        settings(
            {
                "panel": {"model": ASTRA, "effort": "medium"},
                "coordinator": {"effort": "xhigh"},
                "judge": {"model": SOL, "effort": "xhigh"},
            }
        )
    )

    assert built.requested["security"] == ("codex", Setting(ASTRA, "medium"))
    assert built.requested["coordinator"] == ("claude", Setting(OPUS, "xhigh"))
    assert built.requested["judge"] == ("codex", Setting(SOL, "xhigh"))
    argv = list(built.argv)
    pairs = {(argv[i], argv[i + 1]) for i in range(5, len(argv), 2)}
    for lens in ("intent_contracts", "reliability", "security"):
        assert {("--role-model", f"{lens}={ASTRA}"), ("--role-effort", f"{lens}=medium")} <= pairs
    assert {("--role-model", f"judge={SOL}"), ("--role-effort", "judge=xhigh")} <= pairs
    assert {
        ("--role-model", f"coordinator={OPUS}"),
        ("--role-effort", "coordinator=xhigh"),
    } <= pairs
    assert ("--role-model", f"judge={ASTRA}") not in pairs


@pytest.mark.parametrize(
    "roles, error",
    [
        ({"judge": {"effort": "max"}}, "accepts no effort 'max' on codex"),
        ({"panel": {"models": [SOL, ASTRA]}}, "exactly one model"),
    ],
)
def test_planning_refuses_what_the_adapter_cannot_launch(roles, error):
    # Review round 1 of PR #119: the matrix parser planned these, and only launch refused them.
    with pytest.raises(MatrixError, match=error):
        settings(roles)


@pytest.mark.parametrize(
    "edit, error",
    [
        (lambda s: s.update(version="c" * 40), "not a release"),
        (lambda s: s["roles"]["panel"].update(models=[ASTRA, SOL]), "exactly one model"),
        (lambda s: s["roles"].pop("judge"), "not the settings"),
        (lambda s: s["roles"]["judge"].update(effort="max"), "accepts no effort 'max' on codex"),
        (lambda s: s["roles"]["judge"].update(models=["claude-sonnet-5-5"]), "same CLI"),
    ],
)
def test_launch_refuses_settings_rreview_cannot_run(edit, error):
    s = copy.deepcopy(settings())
    edit(s)

    with pytest.raises(ValueError, match=error):
        launch(s)


def test_structured_result_becomes_findings_without_a_model_step():
    judge = FINDING | {"stage": "judge", "priority": 2, "body": " Restored. ", "file": "b.py"}

    out = findings(
        json.dumps(result([FINDING, judge], [FINDING, judge, FINDING | {"priority": 3}])), 1
    )

    assert out == [
        {
            "file": "src/app.py",
            "start_line": 3,
            "end_line": 4,
            "message": f"{FINDING['title']}\n\n{FINDING['body']}",
            "severity": "P1",
        },
        {
            "file": "b.py",
            "start_line": 3,
            "end_line": 4,
            "message": f"{FINDING['title']}\n\nRestored.",
            "severity": "P2",
        },
    ]


def test_clean_review_has_no_findings_and_dropped_candidates_never_become_any():
    assert findings(json.dumps(result([])), 0) == []


@pytest.mark.parametrize(
    "stdout, code, error",
    [
        (json.dumps(result([FINDING])), 2, r"failed \(exit 2\)"),
        ("not json", 0, "not JSON"),
        ('{"findings": [], "findings": []}', 0, "repeats a key"),
        (json.dumps(result([FINDING])), 0, "exit 0 with 1 gated"),
        (json.dumps(result([])), 1, "exit 1 with 0 gated"),
        (json.dumps(result([]) | {"sandbox": "os"}), 0, "--sandbox external"),
        (json.dumps(result([]) | {"extra": 1}), 0, "exactly the fields"),
        # Review round 1 of PR #119: values rreview 0.1.1 never writes.
        (json.dumps(result([FINDING | {"file": "/tmp/unrelated.py"}])), 1, r"\.file: .*relative"),
        (json.dumps(result([FINDING | {"file": "a/../b.py"}])), 1, r"\.file: .*relative"),
        (json.dumps(result([FINDING | {"end_line": 2**32}])), 1, r"\.end_line: .*2\^32"),
        (json.dumps(result([FINDING | {"body": ""}])), 1, r"\.body: .*non-blank"),
        (json.dumps(result([]) | {"base": None}), 0, r"\$\.base: expected a commit id"),
        (json.dumps(result([]) | {"passes": [42]}), 0, r"\$\.passes\[0\]"),
        (json.dumps(result([]) | {"dropped_candidates": [False]}), 0, r"dropped_candidates\[0\]"),
        (json.dumps(result([FINDING | {"confidence_score": 10**309}])), 1, "confidence_score"),
        (json.dumps(result([FINDING | {"confidence_score": 1}])), 1, "confidence_score"),
        # The gated findings must be the default gate applied to the ungated ones.
        (json.dumps(result([FINDING | {"priority": 3}])), 1, "default gate"),
        (json.dumps(result([], [FINDING])), 0, "default gate"),
        (json.dumps(result([FINDING | {"priority": 4}])), 1, r"findings\[0\].priority"),
        (json.dumps(result([FINDING | {"priority": True}])), 1, "priority"),
        (json.dumps(result([FINDING | {"start_line": 5}])), 1, "start_line <= end_line"),
        (json.dumps(result([FINDING | {"confidence_score": 1.5}])), 1, "confidence_score"),
        (json.dumps(result([FINDING | {"stage": "panel"}])), 1, "stage"),
        (json.dumps(result([FINDING | {"file": " "}])), 1, "relative"),
        (json.dumps(result([{k: v for k, v in FINDING.items() if k != "body"}])), 1, "fields"),
        # A malformed finding below the gate still fails the trial.
        (json.dumps(result([], [FINDING | {"end_line": "4"}])), 0, r"ungated_findings\[0\]"),
        (json.dumps(result([]) | {"degraded": "no"}), 0, "degraded"),
    ],
)
def test_result_off_rreview_format_fails_the_trial(stdout, code, error):
    with pytest.raises(RreviewOutputError, match=error):
        findings(stdout, code)


def test_errors_never_quote_engine_output():
    secret = "SECRET-TEXT"
    with pytest.raises(RreviewOutputError) as e:
        findings(json.dumps(result([FINDING | {"stage": secret}])), 1)

    assert secret not in str(e.value)


@pytest.fixture
def fake_rreview(fake_bin, tmp_path, monkeypatch):
    """`rreview` first on PATH: the fake tool shim under another name."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rreview").symlink_to(REPO / "tests" / "fixtures" / "bin" / "fake")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return fake_bin


def test_launch_through_a_fake_rreview_end_to_end(fake_rreview, tmp_path):
    fake_rreview.scenario("rreview", stdout=json.dumps(result([FINDING])), exit=1)
    built = launch(settings())

    run = subprocess.run(
        built.argv,
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )

    assert [f["severity"] for f in findings(run.stdout, run.returncode)] == ["P1"]
    (call,) = fake_rreview.calls()
    assert (call["tool"], call["argv"]) == ("rreview", list(built.argv[1:]))


def test_gh_stub_answers_the_pr_selector_rreview_gets():
    stub = (REPO / "images" / "agent" / "gh").read_text()
    assert '"reviewed"' in stub and launch(settings()).argv[1] == "reviewed"
