"""The `rreview` engine (spec engine-configurations, Engine adapters; design D5, D6, D13).

`engines/rreview.toml` declares it, pinned by the commit of an rreview release, and the agent
image builds that commit (`rbench.trial.build_agent_image(rreview=...)`). Its three rbench roles
map to rreview's five: `panel` gives its one model and effort to the lenses `intent_contracts`,
`reliability` and `security`, and `coordinator` and `judge` are rreview's roles of those names.

A trial runs one `rreview` process from the trial's repository with `--sandbox external` (D6) and
`--json`. The adapter gives every rreview role its agent, model and effort on the command line,
so rreview passes `--model`/`--effort` (Claude) or `-m`/`-c model_reasoning_effort` (Codex) to
every pass itself and the shims add none (D5). rreview resolves the PR with `gh pr view`, which
the trial's gh stub answers by the head branch `reviewed`. Its `result.json` on stdout is
structured, and becomes findings with no model step: the findings rreview reports at its default
gate, each with rreview's own priority label. A result off rreview's format fails the trial, and
nothing repairs it.
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from rbench.engines.declaration import DeclarationError, Engine, Setting
from rbench.engines.pins import MODELS

NAME = "rreview"
# The rreview releases this adapter was written against, by commit: their CLI and `result.json`
# were read at that commit. A new release joins here after its CLI and format are checked.
RELEASES = {"f71d6e171bf47312fd6e564b6cc6a3dcc2d1fe94": "0.1.1"}  # tag 0.1.1, with rreview PR #55
PANEL = ("intent_contracts", "reliability", "security")
ROLES = {"panel": PANEL, "coordinator": ("coordinator",), "judge": ("judge",)}
# The efforts rreview 0.1.1 accepts per agent (`Settings::new` in src/review.rs); it refuses any
# other before the review starts.
EFFORTS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("minimal", "low", "medium", "high", "xhigh"),
}
# The gh stub's selector for the trial's PR (images/agent/gh): its head branch. The argv then
# holds nothing of the task.
PR = "reviewed"
EXIT_CLEAN, EXIT_GATED = 0, 1  # exit 2: the review failed
RESULT_KEYS = {
    "findings", "ungated_findings", "dropped_candidates", "overall_correctness",
    "overall_explanation", "review_process", "base", "head", "passes", "degraded", "sandbox",
}  # fmt: skip
FINDING_KEYS = {
    "title", "body", "file", "start_line", "end_line", "priority", "confidence_score", "stage",
    "candidate_ids",
}  # fmt: skip
STAGES = ("coordinator", "judge")


class RreviewOutputError(ValueError):
    """rreview failed, or its output does not follow its format: the trial is `failed`."""


@dataclass(frozen=True)
class Launch:
    """The trial's one rreview process: run `argv` in the trial's repository."""

    argv: tuple[str, ...]
    # rreview role -> the CLI, model and effort it was asked to run (spec Applied model and
    # effort are recorded: the audit compares what each session applied with this).
    requested: dict[str, tuple[str, Setting]]


def bind(engine: Engine, engines_dir: Path) -> Engine:
    """Check the `rreview` declaration against what this adapter supports."""
    where = f"{Path(engines_dir) / (NAME + '.toml')}: engine {NAME!r}"
    if engine.pin_kind != "git" or engine.pin_ref not in RELEASES:
        known = ", ".join(f"{c} ({v})" for c, v in RELEASES.items())
        raise DeclarationError(
            f"{where}: pin must be the git commit of a release this adapter knows: {known}"
        )
    if engine.output != "structured":
        raise DeclarationError(f"{where}: output must be 'structured' (rreview's result.json)")
    if engine.roles.keys() != ROLES.keys():
        raise DeclarationError(f"{where}: needs exactly the roles {', '.join(ROLES)}")
    for name, role in engine.roles.items():
        if role.delivery != "flag":
            raise DeclarationError(
                f"{where}: role {name!r}: delivery must be 'flag' (rreview passes model and"
                " effort as flags)"
            )
        try:
            _effort(f"{where}: role {name!r}: default", MODELS[role.default.model], role.default)
        except ValueError as e:
            raise DeclarationError(str(e)) from None
    return engine


def _effort(where: str, cli: str, setting: Setting) -> None:
    if setting.effort not in EFFORTS[cli]:
        raise ValueError(
            f"{where}: rreview accepts no effort {setting.effort!r} on {cli};"
            f" it accepts {', '.join(EFFORTS[cli])}"
        )


def launch(settings: Mapping) -> Launch:
    """The launch of the rreview configuration with these frozen `settings`."""
    where = f"rreview configuration {settings.get('roles')}"
    if settings.get("version") not in RELEASES:
        raise ValueError(f"{where}: not a release of rreview this adapter knows")
    roles = settings.get("roles")
    if not (isinstance(roles, Mapping) and roles.keys() == ROLES.keys()):
        raise ValueError(f"{where}: not the settings of an rreview configuration")
    argv = ["rreview", PR, "--sandbox", "external", "--json"]
    requested = {}
    for role, inner in sorted(ROLES.items()):
        try:
            (model,) = roles[role]["models"]
            setting = Setting(model, roles[role]["effort"])
        except (KeyError, TypeError, ValueError):
            # limit: one model per role; mixed panels are later rounds (design D13).
            raise ValueError(
                f"{where}: role {role!r} needs exactly one model and an effort"
            ) from None
        cli = MODELS.get(setting.model)
        if cli is None:
            raise ValueError(f"{where}: role {role!r}: unknown model")
        _effort(f"{where}: role {role!r}", cli, setting)
        for name in inner:
            argv += ["--role-agent", f"{name}={cli}", "--role-model", f"{name}={setting.model}"]
            argv += ["--role-effort", f"{name}={setting.effort}"]
            requested[name] = (cli, setting)
    if requested["coordinator"][0] == requested["judge"][0]:
        raise ValueError(f"{where}: rreview refuses a coordinator and judge on the same CLI")
    return Launch(tuple(argv), dict(sorted(requested.items())))


class _RepeatedKey(ValueError):
    pass


def _unique(pairs: list[tuple[str, object]]) -> dict:
    """A JSON object, refused when it repeats a key: `json.loads` would keep only the last."""
    if len({k for k, _ in pairs}) != len(pairs):
        raise _RepeatedKey
    return dict(pairs)


def _finding(f: object, at: str) -> dict:
    """One rreview finding as an rbench finding. Errors name the place, never the value: it is
    engine output, and may quote a hidden task."""
    if not (isinstance(f, dict) and f.keys() == FINDING_KEYS):
        raise RreviewOutputError(
            f"{at}: expected exactly the fields {', '.join(sorted(FINDING_KEYS))}"
        )
    for key in ("title", "body", "file", "stage"):
        if not isinstance(f[key], str):
            raise RreviewOutputError(f"{at}.{key}: expected a string")
    if not (f["title"].strip() and f["file"].strip()):
        raise RreviewOutputError(f"{at}: title and file must not be blank")
    lines = (f["start_line"], f["end_line"])
    if not (all(type(n) is int for n in lines) and 1 <= lines[0] <= lines[1]):
        raise RreviewOutputError(f"{at}: expected integers 1 <= start_line <= end_line")
    if not (type(f["priority"]) is int and 0 <= f["priority"] <= 3):
        raise RreviewOutputError(f"{at}.priority: expected an integer 0-3")
    confidence = f["confidence_score"]
    if not (
        type(confidence) in (int, float) and math.isfinite(confidence) and 0 <= confidence <= 1
    ):
        raise RreviewOutputError(f"{at}.confidence_score: expected a number 0-1")
    if f["stage"] not in STAGES:
        raise RreviewOutputError(f"{at}.stage: not one of {', '.join(STAGES)}")
    ids = f["candidate_ids"]
    if not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
        raise RreviewOutputError(f"{at}.candidate_ids: expected a list of strings")
    body = f["body"].strip()
    message = f"{f['title'].strip()}\n\n{body}" if body else f["title"].strip()
    return {
        "file": f["file"],
        "start_line": lines[0],
        "end_line": lines[1],
        "message": message,
        "severity": f"P{f['priority']}",
    }


def findings(stdout: str, exit_code: int) -> list[dict]:
    """The trial's findings from rreview's exit code and its `--json` stdout (`result.json`):
    the findings at rreview's default gate. Dropped candidates never become findings. Raises
    `RreviewOutputError` when the review failed or the result is off rreview's format."""
    if exit_code not in (EXIT_CLEAN, EXIT_GATED):
        raise RreviewOutputError(f"rreview: the review failed (exit {exit_code})")
    try:
        result = json.loads(stdout, object_pairs_hook=_unique)
    except _RepeatedKey:
        raise RreviewOutputError("rreview: an object in result.json repeats a key") from None
    except (ValueError, RecursionError):
        raise RreviewOutputError("rreview: stdout is not JSON") from None
    if not (isinstance(result, dict) and result.keys() == RESULT_KEYS):
        raise RreviewOutputError(
            f"rreview: expected result.json with exactly the fields {', '.join(sorted(RESULT_KEYS))}"
        )
    if result["sandbox"] != "external":
        raise RreviewOutputError("rreview: the result does not say --sandbox external")
    for key in ("findings", "ungated_findings", "dropped_candidates", "passes"):
        if not isinstance(result[key], list):
            raise RreviewOutputError(f"$.{key}: expected a list")
    if not isinstance(result["degraded"], bool):
        raise RreviewOutputError("$.degraded: expected a boolean")
    # Checked whole, so a malformed finding the gate left out still fails the trial.
    for i, f in enumerate(result["ungated_findings"]):
        _finding(f, f"$.ungated_findings[{i}]")
    gated = [_finding(f, f"$.findings[{i}]") for i, f in enumerate(result["findings"])]
    if (exit_code == EXIT_GATED) != bool(gated):
        raise RreviewOutputError(
            f"rreview: exit {exit_code} with {len(gated)} gated findings; exit 1 means some"
        )
    return gated
