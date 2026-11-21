"""The `codex-review` adapter (design D13): declaration, launch, shim merge and findings."""

import json
import subprocess
from pathlib import Path

import pytest
from test_shims import CODEX, CODEX_FLAGS, shim

from rbench.engines import codex_review
from rbench.engines.codex_review import ReviewOutputError
from rbench.engines.declaration import DeclarationError, Setting, load_engine, parse_engine
from rbench.matrix.expansion import Configuration, RoleSetting

ENGINES = Path(__file__).parents[1] / "engines"
MODEL, EFFORT = "gpt-6.2-astra", "medium"
ARGV = [
    "exec", "-m", MODEL, "-c", f'review_model="{MODEL}"', "-c", f'model_reasoning_effort="{EFFORT}"',
    "--sandbox", "danger-full-access", "--json", "-o", codex_review.OUTPUT_PATH,
    "review", "--base", "base",
]  # fmt: skip
ROOT = "/home/node/.codex/sessions/2026/11/21/rollout-root.jsonl"
CHILD = "/home/node/.codex/sessions/2026/11/21/rollout-child.jsonl"


def settings(model: str = MODEL, effort: str = EFFORT) -> dict:
    engine = load_engine(ENGINES / "codex-review.toml")
    roles = (("reviewer", RoleSetting((model,), effort)),)
    return Configuration(engine.name, engine.version, roles).settings


def test_declaration_is_pinned_by_the_codex_cli():
    engine = load_engine(ENGINES / "codex-review.toml")

    assert (engine.pin_kind, engine.version, engine.output) == ("cli", "0.171.0", "structured")
    assert engine.default_configuration() == {"reviewer": Setting("gpt-6.2-sol", "high")}


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ({"pin": {"kind": "version", "ref": "1.0.0"}}, "pin must be"),
        ({"roles": {"reviewer": {"delivery": "config", "cli": "codex",
                                 "default": {"model": MODEL, "effort": "high"}}}}, "exactly one"),
        ({"output": "markdown", "cli": {"codex": "0.171.0", "claude": "2.1.316"}}, "structured"),
    ],
)  # fmt: skip
def test_declaration_shape_is_checked(edit: dict, message: str):
    data = {
        "name": "codex-review",
        "pin": {"kind": "cli", "ref": "codex"},
        "cli": {"codex": "0.171.0"},
        "roles": {
            "reviewer": {
                "cli": "codex",
                "delivery": "flag",
                "default": {"model": MODEL, "effort": "high"},
            }
        },
    } | edit
    with pytest.raises(DeclarationError, match=message):
        codex_review.bind(parse_engine(data, "codex-review.toml"))


def test_launch_is_the_d13_review_with_model_review_model_and_effort():
    launch = codex_review.launch(settings())

    assert launch.cli == "codex" and launch.argv == ("codex", *ARGV)
    assert (launch.stdin, launch.files, launch.output) == ("", {}, codex_review.OUTPUT_PATH)
    assert codex_review.requested(settings()) == Setting(MODEL, EFFORT)


def test_requested_refuses_settings_of_another_shape():
    with pytest.raises(ValueError, match="not the settings of a codex-review configuration"):
        codex_review.requested({"roles": {"coordinator": {}}})


def test_shim_adds_only_the_policy_flags_and_ignore_rules_after_exec():
    final, added, rewrites = shim.codex_launch(ARGV, CODEX | {"model": "x", "effort": "low"})

    # The engine's model and effort win; the shim's would duplicate them (design D5).
    assert added == [*CODEX_FLAGS, "--ignore-rules"] and rewrites == []
    assert final == [*CODEX_FLAGS, "exec", "--ignore-rules", *ARGV[1:]]


def test_fake_codex_gets_the_launch_unchanged_from_the_checkout(fake_bin, tmp_path: Path):
    message = {"type": "item.completed", "item": {"type": "agent_message", "text": "review"}}
    fake_bin.scenario("codex", stdout=json.dumps(message) + "\n")
    launch = codex_review.launch(settings())

    done = subprocess.run(
        launch.argv, input=launch.stdin, cwd=tmp_path, capture_output=True, text=True, check=True
    )

    assert json.loads(done.stdout)["item"]["type"] == "agent_message"
    (call,) = fake_bin.calls()
    assert (call["argv"], call["stdin"], call["cwd"]) == (ARGV, "", str(tmp_path))


def records(*bindings: tuple[str, dict]) -> list[dict]:
    log = [{"seq": 1, "kind": "launch", "launch": 1, "cli": "codex", "argv": ARGV, "rewrites": []}]
    for n, (path, meta) in enumerate(bindings, 2):
        log.append({"seq": n, "kind": "binding", "launch": 1, "path": path, "meta": meta})
    return log


ROOT_META = {"id": "t1", "source": "exec"}
CHILD_META = {"id": "t2", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "t1"}}}}


def finding(path: str = "/rbench/repo/src/app.py", start: int = 3, end: int = 4, **extra) -> dict:
    return {
        "title": "[P1] Off-by-one in the loop",
        "body": "The loop skips the last item.",
        "confidence_score": 0.8,
        "priority": 1,
        "code_location": {"absolute_file_path": path, "line_range": {"start": start, "end": end}},
    } | extra


def review(*findings: object, correctness: str = "patch is incorrect") -> dict:
    return {
        "findings": list(findings),
        "overall_correctness": correctness,
        "overall_explanation": "One defect.",
        "overall_confidence_score": 0.7,
    }


def rollout(transcripts: Path, path: str, *outputs: object) -> None:
    lines = [{"type": "session_meta", "payload": {"id": "t1"}}]
    lines.append({"type": "event_msg", "payload": {"type": "agent_message", "message": "x"}})
    lines.append([1])  # a line that is no record is skipped
    for output in outputs:
        item = {"type": "ExitedReviewMode", "id": "r", "review_output": output}
        lines.append({"type": "event_msg", "payload": {"type": "item_completed", "item": item}})
    file = transcripts / path.lstrip("/")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("".join(json.dumps(line) + "\n" for line in lines))


def test_findings_come_from_the_parent_rollouts_review_output(tmp_path: Path):
    rollout(tmp_path, ROOT, review(finding(), finding("lib/b.py", 7, 7, priority=0)))
    rollout(tmp_path, CHILD)  # the review thread's own rollout holds no result
    log = records((ROOT, ROOT_META), (CHILD, CHILD_META))

    output = codex_review.review_output(log, tmp_path)

    assert codex_review.findings(output, "trial x") == [
        {"file": "src/app.py", "start_line": 3, "end_line": 4, "severity": "P1",
         "message": "[P1] Off-by-one in the loop\n\nThe loop skips the last item."},
        {"file": "lib/b.py", "start_line": 7, "end_line": 7, "severity": "P0",
         "message": "[P1] Off-by-one in the loop\n\nThe loop skips the last item."},
    ]  # fmt: skip


def test_a_review_with_no_findings_has_none(tmp_path: Path):
    rollout(tmp_path, ROOT, review(correctness="patch is correct"))

    output = codex_review.review_output(records((ROOT, ROOT_META)), tmp_path)

    assert codex_review.findings(output, "trial x") == []


@pytest.mark.parametrize(
    ("outputs", "bindings", "message"),
    [
        ([None], [(ROOT, ROOT_META)], "without a result"),
        ([], [(ROOT, ROOT_META)], "found 0"),
        ([review(), review()], [(ROOT, ROOT_META)], "found 2"),
        ([review()], [(ROOT, ROOT_META), (CHILD, {"id": "t3"})], "found 2"),
        ([review()], [], "found 0"),
    ],
)
def test_no_single_completed_review_fails_the_trial(tmp_path, outputs, bindings, message):
    rollout(tmp_path, ROOT, *outputs)
    rollout(tmp_path, CHILD)

    with pytest.raises(ReviewOutputError, match=message):
        codex_review.review_output(records(*bindings), tmp_path)


@pytest.mark.parametrize(
    ("output", "message"),
    [
        # Codex's fallback for an answer it cannot parse: the text, no findings, no verdict.
        (review(correctness="") | {"overall_explanation": "prose"}, "not a structured review"),
        (review(finding("/etc/passwd")), "finding 0: the file is not inside"),
        (review(finding(), finding("../x.py")), "finding 1: the file is not inside"),
        (review(finding(start=0, end=0)), "finding 0: start_line"),
        (review(finding(start=5, end=4)), "finding 0: end_line"),
        (review(finding(priority=4)), "priority"),
        (review(finding(priority=True)), "priority"),
        (review(finding(code_location=None)), "code_location"),
        (review(finding(body=None)), "body"),
        (review("not an object"), "code_location"),
        ({"findings": None}, "expected a list"),
    ],
)
def test_malformed_review_output_is_rejected_whole(output: dict, message: str):
    with pytest.raises(ReviewOutputError, match=message):
        codex_review.findings(output, "trial x")


def test_argv_log_check():
    launch = codex_review.launch(settings())

    assert codex_review.check_launches(records(), launch) == []
    assert codex_review.check_launches(records() * 2, launch) == [
        "expected one launch in the shim log, found 2"
    ]
    other = codex_review.launch(settings(effort="high"))
    assert codex_review.check_launches(records(), other) == [
        "the launch is not the argv the codex-review adapter built"
    ]
    rewritten = [records()[0] | {"rewrites": [{"index": 1}]}]
    assert codex_review.check_launches(rewritten, launch) == [
        "the shim rewrote the codex-review launch's argv"
    ]
