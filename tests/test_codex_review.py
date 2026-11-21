"""The `codex-review` adapter (design D13): declaration, launch, shim merge and findings."""

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
from test_shims import CODEX, CODEX_FLAGS, collector, fake_proc, shim

from rbench.engines import codex_review
from rbench.engines.codex_review import ReviewOutputError
from rbench.engines.declaration import DeclarationError, Setting, load_engine, parse_engine
from rbench.matrix.expansion import Configuration, RoleSetting
from rbench.trial import unbound_sessions
from rbench.trial.repository import REPO_DIR

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


def launch_record(**change) -> dict:
    """The launch record the real shim logs for the default launch, run from the checkout."""
    final, added, rewrites = shim.codex_launch(ARGV, CODEX)
    record = {"kind": "launch", "cli": "codex", "argv": ARGV, "cwd": REPO_DIR, "added": added}
    record |= {"rewrites": rewrites, "final_argv": final}
    record["homes"] = {"CLAUDE_CONFIG_DIR": None, "CODEX_HOME": None}
    return collector.check(record | change, ("launch",))


def records(*bindings: tuple[str, dict]) -> list[dict]:
    log = [{"seq": 1, "launch": 1, **launch_record()}]
    for n, (path, meta) in enumerate(bindings, 2):
        log.append({"seq": n, "kind": "binding", "launch": 1, "path": path, "meta": meta})
    return log


ROOT_META = {"id": "t1", "source": "exec"}
# Codex 0.171.0's review thread: its parent at the top level, which the shim log's meta drops.
CHILD_META = {"id": "t2", "parent_thread_id": "t1", "source": {"subagent": "review"}}


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


def rollout(transcripts: Path, path: str, *outputs: object, meta: dict = ROOT_META) -> Path:
    lines = [json.dumps({"type": "session_meta", "payload": meta})]
    lines.append(json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}}))
    lines += ["[1]", '{"cut short']  # a line that is no record, or no JSON, is skipped
    for output in outputs:
        item = {"type": "ExitedReviewMode", "id": "r", "review_output": output}
        payload = {"type": "item_completed", "item": item}
        lines.append(json.dumps({"type": "event_msg", "payload": payload}))
    file = transcripts / path.lstrip("/")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("".join(line + "\n" for line in lines))
    return file


TWO_FINDINGS = [
    {"file": "src/app.py", "start_line": 3, "end_line": 4, "severity": "P1",
     "message": "[P1] Off-by-one in the loop\n\nThe loop skips the last item."},
    {"file": "lib/b.py", "start_line": 7, "end_line": 7, "severity": "P0",
     "message": "[P1] Off-by-one in the loop\n\nThe loop skips the last item."},
]  # fmt: skip


def test_findings_come_from_the_parent_rollouts_review_output(tmp_path: Path):
    rollout(tmp_path, ROOT, review(finding(), finding("lib/b.py", 7, 7, priority=0)))
    rollout(tmp_path, CHILD, meta=CHILD_META)  # the review thread's own rollout holds no result
    log = records((ROOT, ROOT_META), (CHILD, {"id": "t2", "source": {"subagent": "review"}}))

    output = codex_review.review_output(log, tmp_path, "trial x")

    assert codex_review.findings(output, "trial x") == TWO_FINDINGS


def test_default_launch_carries_from_the_shim_records_to_findings(tmp_path: Path, monkeypatch):
    """The pinned review delegate's rollout, bound by the real shim watcher and accepted by the
    collector: the review thread still counts as a child, so the root's review is scored."""
    container, transcripts = tmp_path / "container", tmp_path / "transcripts"
    root = rollout(
        container, "/home/node/.codex/sessions/2026/11/21/rollout-root.jsonl",
        review(finding(), finding("lib/b.py", 7, 7, priority=0)),
    )  # fmt: skip
    child = rollout(
        container, "/home/node/.codex/sessions/2026/11/21/rollout-child.jsonl", meta=CHILD_META
    )
    append = 0o2102002  # O_RDWR | O_APPEND | O_CLOEXEC
    fake_proc(
        tmp_path / "proc", {10: 1, 11: 10}, {10: {"3": (root, append)}, 11: {"3": (child, append)}}
    )
    monkeypatch.setattr(shim, "PROC", str(tmp_path / "proc"))
    ours, theirs = socket.socketpair()
    stop, go = os.pipe()
    os.close(go)  # the CLI has exited: the watcher reports once and returns
    with ours, theirs:
        shim.watch(10, ours, stop)
        ours.shutdown(socket.SHUT_WR)
        sent = [json.loads(line) for line in theirs.makefile("r", encoding="utf-8")]
    os.close(stop)
    bindings = [collector.check(r, ("binding",)) for r in sent]
    log = [{"seq": 1, "launch": 1, **launch_record()}]
    log += [{"seq": n, "launch": 1, **r} for n, r in enumerate(bindings, 2)]
    exit_ = collector.check({"kind": "exit", "status": 0}, ("exit",))
    log.append({"seq": len(log) + 1, "launch": 1, **exit_})
    for file in (root, child):
        target = transcripts / str(file).lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(file, target)

    assert [b["meta"] for b in bindings if b["path"] == str(child)] == [
        {"id": "t2", "source": {"subagent": "review"}}
    ]
    assert unbound_sessions(log, transcripts) == []
    assert codex_review.check_launches(log, codex_review.launch(settings())) == []
    output = codex_review.review_output(log, transcripts, "trial x")
    assert codex_review.findings(output, "trial x") == TWO_FINDINGS


def test_a_review_with_no_findings_has_none(tmp_path: Path):
    rollout(tmp_path, ROOT, review(correctness="patch is correct"))

    output = codex_review.review_output(records((ROOT, ROOT_META)), tmp_path, "trial x")

    assert codex_review.findings(output, "trial x") == []


@pytest.mark.parametrize(
    ("outputs", "child", "bindings", "message"),
    [
        ([None], CHILD_META, [ROOT], "record 2: the review ended without a result"),
        ([], CHILD_META, [ROOT], "found 0"),
        ([review(), review()], CHILD_META, [ROOT], "found 2"),
        ([review()], {"id": "t3"}, [ROOT, CHILD], "trial x: expected one root .*, found 2"),
        ([review()], CHILD_META, [], "found 0"),
        ([review()], CHILD_META, [CHILD], "found 0"),  # only the review thread was bound
    ],
)
def test_no_single_completed_review_fails_the_trial(tmp_path, outputs, child, bindings, message):
    rollout(tmp_path, ROOT, *outputs)
    rollout(tmp_path, CHILD, meta=child)
    log = records(*((path, {}) for path in bindings))

    with pytest.raises(ReviewOutputError, match=message):
        codex_review.review_output(log, tmp_path, "trial x")


def test_a_repeated_key_in_the_review_line_fails_the_trial(tmp_path: Path):
    file = rollout(tmp_path, ROOT, review(finding()))
    line = json.dumps({"type": "event_msg", "payload": {"type": "item_completed"}})
    # A second `findings` key: json would keep the last one and score it.
    file.write_text(file.read_text().replace('{"findings": [', '{"findings": [], "findings": ['))
    with file.open("a") as f:
        f.write(line + "\n")

    with pytest.raises(ReviewOutputError, match=r"^trial x: rollout bound by shim log record 2, "
                       r"line 5: a key is repeated$"):  # fmt: skip
        codex_review.review_output(records((ROOT, ROOT_META)), tmp_path, "trial x")


def test_an_uncollected_rollout_fails_without_naming_its_file(tmp_path: Path):
    path = "/home/node/.codex/sessions/rollout-secret-name.jsonl"

    with pytest.raises(ReviewOutputError) as e:
        codex_review.review_output(records((path, ROOT_META)), tmp_path, "trial x")

    assert (
        str(e.value)
        == "trial x: rollout bound by shim log record 2: the bound rollout was not collected"
    )


@pytest.mark.parametrize(
    ("output", "message"),
    [
        # Codex's fallback for an answer it cannot parse: the text, no findings, no verdict.
        (review(correctness="") | {"overall_explanation": "prose"}, "not a structured review"),
        (review(finding("/etc/passwd")), "finding 0: the file is not inside"),
        (review(finding(), finding("../x.py")), "finding 1: the file is not inside"),
        (review(finding("/rbench/repo/a\0b.py")), "finding 0: the file is not inside"),
        (review(finding(start=0, end=0)), "finding 0: start_line"),
        (review(finding(start=5, end=4)), "finding 0: end_line"),
        (review(finding(start=2**32, end=2**32)), "finding 0: line_range: expected .* u32"),
        (review(finding(start=-1, end=3)), "finding 0: line_range"),
        (review(finding(end=4.0)), "finding 0: line_range"),
        (review(finding(priority=4)), "priority"),
        (review(finding(priority=True)), "priority"),
        (review(finding(code_location=None)), "code_location"),
        (review(finding(body=None)), "body"),
        (review(finding(confidence_score="high")), "finding 0: expected .*confidence_score"),
        (review(finding(confidence_score=float("nan"))), "confidence_score"),
        (review("not an object"), "code_location"),
        ({"findings": None}, "review_output: expected findings"),
        ({"findings": []}, "review_output: expected findings"),
        (review() | {"overall_confidence_score": None}, "overall_confidence_score"),
        (review() | {"overall_explanation": 1}, "overall_explanation"),
        ({k: v for k, v in review().items() if k != "overall_correctness"}, "correctness"),
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
        "the launch is not the argv the codex-review adapter built",
        "the CLI did not get the codex-review argv with only the policy flags added",
    ]
    rewritten = [records()[0] | {"rewrites": [{"index": 1}]}]
    assert codex_review.check_launches(rewritten, launch) == [
        "the shim rewrote the codex-review launch's argv"
    ]


def test_launch_check_reads_the_argv_the_cli_got_and_its_directory():
    launch = codex_review.launch(settings())
    final = launch_record()["final_argv"]
    other_model = [a.replace(f'review_model="{MODEL}"', 'review_model="other"') for a in final]

    assert codex_review.check_launches([launch_record(final_argv=other_model)], launch) == [
        "the CLI did not get the codex-review argv with only the policy flags added"
    ]
    assert codex_review.check_launches([launch_record(cwd="/rbench")], launch) == [
        "the codex-review launch did not run in the reviewed checkout /rbench/repo"
    ]
