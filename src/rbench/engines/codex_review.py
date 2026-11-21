"""The `codex-review` engine: Codex's built-in review (spec engine-configurations, Engine adapters;
design D13).

`engines/codex-review.toml` declares it, pinned by the Codex CLI version. A trial runs one launch,
`codex exec review --base base`, from the reviewed checkout, whose branch `base` is the PR's base
(`rbench.trial.repository`). The adapter passes the role's model twice, as the session model (`-m`)
and as `review_model`, which the review thread runs, and the effort, which both threads share;
the shim then adds only the policy flags (design D5).

Codex 0.171.0 writes the review's structured result into the parent thread's rollout: an
`event_msg` whose payload is `item_completed` for an `ExitedReviewMode` item, holding
`review_output` (spike #22). Each finding there has a file and a line range, so findings come from
it without a model step (spec Findings normalization, Structured output). The rendered text (`-o`)
and the stdout JSONL are kept for diagnostics only.
"""

import json
import math
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath

from rbench.engines.declaration import DeclarationError, Engine, Setting
from rbench.engines.normalization import NormalizationError, parse_findings
from rbench.engines.plain import Launch
from rbench.ledger.trials import no_repeated_keys
from rbench.trial.hermetic import POLICY
from rbench.trial.repository import REPO_DIR
from rbench.trial.sessions import _find

NAME = "codex-review"
ROLE = "reviewer"
# The rendered review Codex writes with `-o`: outside the reviewed repository.
OUTPUT_PATH = "/home/node/codex-review.md"
# Codex's built-in review prompt asks for priorities 0 to 3 (P0 most severe).
PRIORITIES = range(4)
# The pinned `ReviewLineRange` holds `u32` lines.
U32 = range(2**32)


class ReviewOutputError(ValueError):
    """The review gave no usable structured result: the trial is `failed`."""


def bind(engine: Engine) -> Engine:
    """Check the `codex-review` declaration: pinned by its Codex CLI, one reviewer on Codex."""
    where = f"engine {NAME!r}"
    if (engine.pin_kind, engine.pin_ref) != ("cli", "codex"):
        raise DeclarationError(f"{where}: pin must be {{ kind = 'cli', ref = 'codex' }}")
    role = engine.roles.get(ROLE)
    if (
        engine.roles.keys() != {ROLE}
        or role is None
        or (role.cli, role.delivery) != ("codex", "flag")
    ):
        raise DeclarationError(
            f"{where}: needs exactly one role {ROLE!r} with cli = 'codex' and delivery = 'flag'"
        )
    if engine.output != "structured":
        raise DeclarationError(f"{where}: output must be structured (the review's own result)")
    return engine


def requested(settings: Mapping) -> Setting:
    """The model and effort every session of the trial requests: the parent thread (`-m`) and its
    review thread (`review_model`), both at the role's effort. For recording them as applied."""
    try:
        (model,) = settings["roles"][ROLE]["models"]
        return Setting(model, settings["roles"][ROLE]["effort"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{NAME}: not the settings of a {NAME} configuration") from None


def launch(settings: Mapping) -> Launch:
    """The launch of the configuration with these frozen `settings` (design D13). It runs in the
    reviewed checkout, with no prompt on stdin."""
    s = requested(settings)
    argv = ("codex", "exec", "-m", s.model, "-c", f"review_model={json.dumps(s.model)}")
    argv += ("-c", f"model_reasoning_effort={json.dumps(s.effort)}")
    argv += ("--sandbox", "danger-full-access", "--json", "-o", OUTPUT_PATH)
    return Launch("codex", (*argv, "review", "--base", "base"), "", {}, OUTPUT_PATH)


def check_launches(records: list[dict], launch: Launch) -> list[str]:
    """What in the shim log shows that the trial did not run `launch` as built: exactly one
    launch, with the argv the adapter built, nothing rewritten, the CLI given that argv with only
    the policy flags added (design D5), and run from the reviewed checkout, where `base` is the
    PR's base."""
    launches = [r for r in records if r.get("kind") == "launch"]
    if len(launches) != 1:
        return [f"expected one launch in the shim log, found {len(launches)}"]
    (record,) = launches
    problems = []
    if record.get("cli") != launch.cli or record.get("argv") != list(launch.argv[1:]):
        problems.append(f"the launch is not the argv the {NAME} adapter built")
    if record.get("rewrites"):
        problems.append(f"the shim rewrote the {NAME} launch's argv")
    policy = POLICY["codex"]
    final = [*policy["flags"], launch.argv[1], *policy["exec_flags"], *launch.argv[2:]]
    if record.get("final_argv") != final:
        problems.append(f"the CLI did not get the {NAME} argv with only the policy flags added")
    if record.get("cwd") != REPO_DIR:
        problems.append(f"the {NAME} launch did not run in the reviewed checkout {REPO_DIR}")
    return problems


def _records(file: Path, where: str) -> Iterator[tuple[int, dict]]:
    """The JSON object lines of a collected rollout, numbered from 1. A line that is not JSON is
    skipped (a write cut short); one that repeats a key is ambiguous and fails the trial."""
    try:
        with file.open(encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                try:
                    record = json.loads(line, object_pairs_hook=no_repeated_keys)
                except json.JSONDecodeError:
                    continue
                except ValueError:
                    raise ReviewOutputError(f"{where}, line {n}: a key is repeated") from None
                if isinstance(record, dict):
                    yield n, record
    except (OSError, UnicodeDecodeError):
        raise ReviewOutputError(f"{where}: cannot read the collected rollout") from None


def _collected(transcripts: Path, path: str, where: str) -> Path:
    """The collected copy of the rollout at container `path`: a regular file below `transcripts`."""
    p = PurePosixPath(path)
    file = Path(transcripts, *p.parts[1:])
    if not p.is_absolute() or ".." in p.parts or file.is_symlink() or not file.is_file():
        raise ReviewOutputError(f"{where}: the bound rollout was not collected")
    return file


def review_output(records: list[dict], transcripts: Path, where: str) -> dict:
    """The `review_output` of the trial's review, from the rollout of the root thread its launch
    bound (`transcripts` holds the collected rollouts at their container paths, as `collect` lays
    them out). Raises `ReviewOutputError`, naming `where` (the trial) and the shim log record or
    rollout line, when there is no single completed review.

    A thread is a root when the session_meta of its collected rollout names no `parent_thread_id`:
    Codex 0.171.0's review thread names its parent at the top level (source `subagent: review`),
    which the shim log's binding metadata does not keep."""
    roots: dict[str, str] = {}  # container path -> where it was bound
    for r in records:
        if r.get("kind") != "binding" or not isinstance(r.get("path"), str):
            continue
        at = f"{where}: rollout bound by shim log record {r.get('seq')}"
        file = _collected(transcripts, r["path"], at)
        first = next(_records(file, at), (0, {}))[1]
        meta = first.get("payload") if first.get("type") == "session_meta" else None
        if not isinstance(meta, dict):
            raise ReviewOutputError(f"{at}: no session_meta on its first line")
        if _find(meta, "parent_thread_id") is None:
            roots.setdefault(r["path"], at)
    if len(roots) != 1:
        raise ReviewOutputError(
            f"{where}: expected one root thread bound to the launch, found {len(roots)}"
        )
    ((path, at),) = roots.items()
    outputs = []
    for _, record in _records(_collected(transcripts, path, at), at):
        payload = record.get("payload")
        if (
            record.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "item_completed"
            and isinstance(payload.get("item"), dict)
            and payload["item"].get("type") == "ExitedReviewMode"
        ):
            outputs.append(payload["item"].get("review_output"))
    if len(outputs) != 1:
        raise ReviewOutputError(
            f"{at}: expected one completed review in the parent rollout, found {len(outputs)}"
        )
    (output,) = outputs
    if not isinstance(output, dict):
        raise ReviewOutputError(f"{at}: the review ended without a result (interrupted or aborted)")
    return output


def _relative(path: object) -> str | None:
    """A finding's file, relative to the reviewed checkout; None when it is not inside it."""
    if not isinstance(path, str):
        return None
    p = PurePosixPath(path)
    if p.is_absolute():
        try:
            p = p.relative_to(REPO_DIR)
        except ValueError:
            return None
    return None if ".." in p.parts or str(p) == "." else str(p)


def _number(value: object) -> bool:
    """A finite JSON number, as the pinned `f32` confidence scores are."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def findings(output: dict, where: str) -> list[dict]:
    """The trial's findings from `review_output`, in the ledger's format: the file relative to the
    checkout, the line range, the title and body as the message, and the priority as the label
    `P0`..`P3`. Engine output is untrusted: the result must have the pinned `ReviewOutputEvent`
    shape, and one bad item rejects the whole result, naming `where` (the trial) and the item."""
    if not (
        isinstance(output, dict)
        and isinstance(output.get("findings"), list)
        and isinstance(output.get("overall_correctness"), str)
        and isinstance(output.get("overall_explanation"), str)
        and _number(output.get("overall_confidence_score"))
    ):
        raise ReviewOutputError(
            f"{where}: review_output: expected findings (a list), overall_correctness, "
            "overall_explanation and overall_confidence_score"
        )
    items = output["findings"]
    if not items and output["overall_correctness"] == "":
        # Codex keeps an answer it cannot parse as text, with no findings and no verdict.
        raise ReviewOutputError(f"{where}: the reviewer's answer was not a structured review")
    converted = []
    for i, item in enumerate(items):
        at = f"{where}: review finding {i}"
        location = item.get("code_location") if isinstance(item, dict) else None
        lines = location.get("line_range") if isinstance(location, dict) else None
        if not (
            isinstance(location, dict)
            and isinstance(lines, dict)
            and isinstance(item.get("title"), str)
            and isinstance(item.get("body"), str)
            and _number(item.get("confidence_score"))
        ):
            raise ReviewOutputError(
                f"{at}: expected title, body, confidence_score and a code_location line_range"
            )
        start, end = lines.get("start"), lines.get("end")
        if not all(type(n) is int and n in U32 for n in (start, end)):
            raise ReviewOutputError(f"{at}: line_range: expected start and end as u32 integers")
        path = location.get("absolute_file_path")
        file = _relative(path) if isinstance(path, str) and "\0" not in path else None
        if file is None:
            raise ReviewOutputError(f"{at}: the file is not inside the reviewed checkout")
        priority = item.get("priority")
        if type(priority) is not int or priority not in PRIORITIES:
            raise ReviewOutputError(f"{at}: priority: expected an integer from 0 to 3")
        message = "\n\n".join(t for t in (item["title"].strip(), item["body"].strip()) if t)
        converted.append({
            "file": file, "start_line": start, "end_line": end,
            "message": message, "severity": f"P{priority}",
        })  # fmt: skip
    try:
        return parse_findings(converted, where)
    except NormalizationError as e:
        raise ReviewOutputError(str(e)) from None
