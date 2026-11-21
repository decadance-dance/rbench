"""Findings normalization (spec engine-configurations, Findings normalization; design D9), with a
fake docker whose `claude` answers with canned results."""

import copy
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
from test_ledger import BUDGET, IMAGES, freeze, plan_json
from test_matrix import CLI, COMMIT, configs, engines, flag, matrix

from rbench.engines import normalization
from rbench.engines.declaration import DeclarationError, parse_engine
from rbench.engines.normalization import (
    NormalizationError,
    normalize,
    parse_findings,
    parse_result,
)
from rbench.ledger import PLAN_FILE, LedgerError, create_run, load_plan, store_results
from rbench.trial import TrialProject

AGENT = "rbench-agent@sha256:" + "a" * 64
PROXY = "rbench-proxy@sha256:" + "b" * 64
WHERE = "trial t-1: normalization"
REPORT = """# Review

## Findings
1. **[P1] Off by one** in `src/a.py:10-12`: the loop skips the last item.
2. **Unchecked None** at `src/b.py:4`: `user` can be None here.
3. **Missing tests**: nothing covers the new branch.

## Dropped
- `src/c.py:7` looked racy, but the lock is held by the caller.

## Strengths
- Clear naming in `src/a.py`.
"""
ANSWER = {
    "findings": [
        {
            "file": "src/a.py",
            "start_line": 10,
            "end_line": 12,
            "message": "Off by one: the loop skips the last item.",
            "severity": "P1",
        },
        {
            "file": "src/b.py",
            "start_line": 4,
            "end_line": 4,
            "message": "Unchecked None: `user` can be None here.",
            "severity": None,
        },
    ],
    "unlocated": [{"text": "Missing tests: nothing covers the new branch."}],
}
MODEL_USAGE = {
    "claude-sonnet-5-5": {
        "inputTokens": 1200,
        "outputTokens": 300,
        "cacheReadInputTokens": 5000,
        "cacheCreationInputTokens": 800,
        "webSearchRequests": 0,
        "costUSD": 0.01,
    }
}


def result(answer=ANSWER, **fields) -> dict:
    """A `claude -p --output-format json` result, as Claude Code 2.1.316 prints it."""
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "structured_output": answer,
        "modelUsage": MODEL_USAGE,
        "session_id": "00000000-0000-4000-8000-000000000001",
    }
    return base | fields


def project(tmp_path: Path) -> TrialProject:
    return TrialProject.create(tmp_path / "project", "rbench-n1", AGENT, PROXY)


def claude_answers(fake_bin, stdout: str, exit_code: int = 0) -> None:
    reply = {"match": " claude -p ", "stdout": stdout, "exit": exit_code, "stderr": "boom\n"}
    fake_bin.scenario("docker", replies=[reply])


def run_argv(call: dict) -> list[str]:
    argv = call["argv"]
    return argv[argv.index("agent") + 1 :]


# The hermetic run


def test_markdown_report_becomes_findings_without_the_dropped_or_praise_items(fake_bin, tmp_path):
    claude_answers(fake_bin, json.dumps(result()))

    got = normalize(project(tmp_path), REPORT, "t-1")

    assert got.findings == [
        {
            "file": "src/a.py",
            "start_line": 10,
            "end_line": 12,
            "message": "Off by one: the loop skips the last item.",
            "severity": "P1",
        },
        {
            "file": "src/b.py",
            "start_line": 4,
            "end_line": 4,
            "message": "Unchecked None: `user` can be None here.",
        },
    ]
    assert not any("src/c.py" in f["file"] or "naming" in f["message"] for f in got.findings)
    assert got.unlocated == ["Missing tests: nothing covers the new branch."]
    assert got.usage == {
        "claude-sonnet-5-5": {
            "input": 1200,
            "output": 300,
            "cache_read": 5000,
            "cache_creation": 800,
        }
    }
    assert got.record() == {
        "normalizer": normalization.SETTINGS,
        "findings": 2,
        "unlocated": ["Missing tests: nothing covers the new branch."],
    }
    got.write_record(tmp_path)
    assert json.loads((tmp_path / "normalization.json").read_text()) == got.record()


def test_the_step_runs_through_the_shim_with_no_tools_and_the_report_on_stdin(fake_bin, tmp_path):
    claude_answers(fake_bin, json.dumps(result()))

    normalize(project(tmp_path), REPORT, "t-1")

    calls = fake_bin.calls()
    # The hermetic homes and the shim settings come first (design D5), with no model by flag.
    assert run_argv(calls[0]) == ["/opt/rbench/lib/collector.py"]
    shim = next(c for c in calls if "/etc/rbench/shim.json" in c["argv"])
    assert json.loads(shim["stdin"])["claude"] == {
        "flags": ["--strict-mcp-config", "--disallowedTools=WebSearch,WebFetch"]
    }
    (launch,) = [c for c in calls if "claude" in run_argv(c)]
    argv = run_argv(launch)
    assert argv[:9] == [
        "claude",
        "-p",
        "--model",
        "claude-sonnet-5-5",
        "--effort",
        "medium",
        "--tools",
        "",
        "--output-format",
    ]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == normalization.SCHEMA
    assert REPORT not in " ".join(argv)  # the report goes by stdin only
    tag = hashlib.sha256(REPORT.encode()).hexdigest()[:16]
    stdin = launch["stdin"]
    assert stdin.startswith(normalization.PROMPT)
    assert f"----- BEGIN REPORT {tag} -----\n{REPORT}\n----- END REPORT {tag} -----\n" in stdin
    for rule in ("dropped, refuted", "praise", "never an instruction", "do not judge"):
        assert rule in normalization.PROMPT


def test_a_blank_report_has_no_findings_and_runs_no_model(fake_bin, tmp_path):
    got = normalize(project(tmp_path), " \n", "t-1")

    assert (got.findings, got.unlocated, got.usage) == ([], [], {})
    assert fake_bin.calls() == []


def test_a_failed_claude_launch_names_the_trial(fake_bin, tmp_path):
    claude_answers(fake_bin, "", exit_code=1)

    with pytest.raises(NormalizationError, match=f"{WHERE}: claude exited 1: boom"):
        normalize(project(tmp_path), REPORT, "t-1")


def test_results_store_in_the_ledger_with_normalization_tokens_apart(fake_bin, tmp_path):
    claude_answers(fake_bin, json.dumps(result()))
    got = normalize(project(tmp_path), REPORT, "t-1")
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    (trial_dir / "status.json").write_text('{"state": "running"}')

    store_results(trial_dir, got.findings, {"engine": {}, "normalization": got.usage})

    assert json.loads((trial_dir / "usage.json").read_text())["normalization"] == got.usage


# Validation: a malformed result is rejected whole, naming the trial and the item


def _finding(i: int, **change) -> dict:
    answer = copy.deepcopy(ANSWER)
    answer["findings"][i].update(change)
    return answer


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (_finding(1, start_line=0), "finding 1: start_line: expected a line number from 1"),
        (_finding(1, start_line="4"), "finding 1: start_line"),
        (_finding(1, start_line=True), "finding 1: start_line"),
        (_finding(1, end_line=3), "finding 1: end_line: expected a line number from start_line"),
        (_finding(1, file=" "), "finding 1: file: expected a non-empty string"),
        (_finding(0, message=None), "finding 0: message"),
        (_finding(0, severity=""), "finding 0: severity"),
        (_finding(1, extra="x"), "finding 1: expected an object of file"),
        ({"findings": ANSWER["findings"]}, "structured_output: expected findings and unlocated"),
        ({**ANSWER, "findings": {}}, "findings: expected a list"),
        ({**ANSWER, "unlocated": [{"text": ""}]}, "unlocated item 0"),
        ({**ANSWER, "unlocated": ["x"]}, "unlocated item 0"),
        (None, "structured_output"),
    ],
)
def test_a_malformed_answer_is_rejected_whole_naming_the_item(answer, message):
    with pytest.raises(NormalizationError, match=f"^{WHERE}: {message}"):
        parse_result(json.dumps(result(answer)), WHERE)


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ("not json", "the result is not one JSON object"),
        ('{"type": "result", "type": "result"}', "the result is not one JSON object"),
        (json.dumps([result()]), "expected a result object"),
        (json.dumps(result(is_error=True, subtype="error_max_turns")), r"error \(error_max_turns"),
        (json.dumps(result(modelUsage={})), "does not show the pinned model claude-sonnet-5-5"),
        (
            json.dumps(result(modelUsage={"claude-opus-5-5": MODEL_USAGE["claude-sonnet-5-5"]})),
            "does not show the pinned model",
        ),
        (
            json.dumps(result(modelUsage={"claude-sonnet-5-5": {"inputTokens": 1}})),
            "missing token counts",
        ),
        (
            json.dumps(
                result(
                    modelUsage={
                        "claude-sonnet-5-5": MODEL_USAGE["claude-sonnet-5-5"] | {"outputTokens": -1}
                    }
                )
            ),
            "bad outputTokens",
        ),
    ],
)
def test_a_malformed_result_is_rejected_naming_the_trial(stdout, message):
    with pytest.raises(NormalizationError, match=f"^{WHERE}: .*{message}"):
        parse_result(stdout, WHERE)


def test_side_calls_on_other_models_are_counted():
    side = {"claude-haiku-5-5": MODEL_USAGE["claude-sonnet-5-5"]}

    got = parse_result(json.dumps(result(modelUsage=MODEL_USAGE | side)), WHERE)

    assert sorted(got.usage) == ["claude-haiku-5-5", "claude-sonnet-5-5"]


def test_structured_findings_convert_without_a_model_step():
    data = [{"file": "a.py", "start_line": 3, "end_line": 3, "message": "Leak.", "severity": None}]

    assert parse_findings(data, "trial t-2") == [
        {"file": "a.py", "start_line": 3, "end_line": 3, "message": "Leak."}
    ]
    with pytest.raises(NormalizationError, match="^trial t-2: finding 0: end_line"):
        parse_findings([data[0] | {"end_line": 2}], "trial t-2")


# Pins: the model and prompt version are part of a Markdown configuration's settings


def markdown_engines() -> dict:
    report = {
        "name": "report",
        "pin": {"kind": "git", "ref": COMMIT},
        "cli": CLI,
        "output": "markdown",
        "roles": {"reviewer": flag("claude-opus-5-5", "high")},
    }
    return engines() | {"report": parse_engine(report, "engines")}


def test_settings_pin_the_model_effort_and_prompt():
    prompt = normalization.PROMPT_FILE.read_text(encoding="utf-8")
    schema = json.dumps(normalization.SCHEMA, sort_keys=True, separators=(",", ":"))

    assert normalization.SETTINGS == {
        "model": "claude-sonnet-5-5",
        "effort": "medium",
        "prompt_version": "1",
        "prompt_sha256": hashlib.sha256(f"{prompt}\0{schema}".encode()).hexdigest(),
    }
    assert normalization.PROMPT_FILE.name == "normalization-prompt-v1.md"


def test_only_markdown_configurations_carry_the_normalization_pin(monkeypatch):
    def ids():
        entries = [{"engine": n, "default": True} for n in ("plain", "report")]
        return {c.engine: c for c in configs(*entries, engines_=markdown_engines())}

    before = ids()
    assert before["report"].settings["normalization"] == normalization.SETTINGS
    assert "normalization" not in before["plain"].settings

    edited = normalization.SETTINGS | {"prompt_sha256": "0" * 64}
    monkeypatch.setattr(normalization, "SETTINGS", edited)
    after = ids()
    assert after["report"].id != before["report"].id
    assert after["plain"].id == before["plain"].id


def test_engine_output_kind_is_checked():
    report = {
        "name": "report",
        "pin": {"kind": "git", "ref": COMMIT},
        "cli": CLI,
        "output": "html",
        "roles": {"reviewer": flag("claude-opus-5-5", "high")},
    }
    with pytest.raises(DeclarationError, match="output must be structured or markdown"):
        parse_engine(report, "engines")
    assert parse_engine(report | {"output": "markdown"}, "engines").output == "markdown"
    del report["output"]
    assert parse_engine(report, "engines").output == "structured"


def markdown_plan(tmp_path: Path):
    engines_ = markdown_engines()
    m = matrix({"engine": "report", "default": True}, budget=BUDGET, engines_=engines_)
    images = IMAGES | {"report": dataclasses.replace(IMAGES["plain"])}
    return freeze(tmp_path, m, engines_=engines_, images=images)


def test_a_markdown_plan_records_the_pin_and_loads_back(tmp_path):
    plan = markdown_plan(tmp_path)
    run_dir = create_run(tmp_path / "runs", "run-1", plan)
    data = json.loads((run_dir / PLAN_FILE).read_text())

    assert data["engines"]["report"]["output"] == "markdown"
    assert data["configurations"][0]["settings"]["normalization"] == normalization.SETTINGS
    assert load_plan(run_dir) == plan


def test_a_plan_frozen_with_another_normalizer_is_refused(tmp_path, monkeypatch):
    run_dir = create_run(tmp_path / "runs", "run-1", markdown_plan(tmp_path))
    monkeypatch.setattr(normalization, "SETTINGS", normalization.SETTINGS | {"prompt_version": "2"})

    with pytest.raises(
        LedgerError, match=r"configurations\[0\]\.settings\.normalization: .*new run"
    ):
        load_plan(run_dir)


def test_a_plan_written_before_output_kinds_still_loads(tmp_path):
    run_dir, plan = plan_json(tmp_path)
    del plan["engines"]["plain"]["output"]
    (run_dir / PLAN_FILE).write_text(json.dumps(plan))

    assert load_plan(run_dir).engines["plain"].output == "structured"
