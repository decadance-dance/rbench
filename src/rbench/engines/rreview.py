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
import re
from collections.abc import Callable, Mapping
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
# The default gate of rreview 0.1.1 (`Gate::DEFAULT` in src/result.rs): `findings` is exactly the
# `ungated_findings` with priority <= 2 and confidence >= 0.5.
GATE_PRIORITY, GATE_CONFIDENCE = 2, 0.5
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


# Checks of rreview 0.1.1's types (`ReviewResult`, `PassReport` in src/result.rs; `Finding`,
# `Dropped`, `LineRange` in src/coordinator.rs), with the rules its `finding` enforces. A check
# takes the value and its place, and raises naming the place and the rule, never the value: it
# is engine output, and may quote a hidden task.
Check = Callable[[object, str], None]
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _is(test: Callable[[object], bool], what: str) -> Check:
    def check(v: object, at: str) -> None:
        if not test(v):
            raise RreviewOutputError(f"{at}: expected {what}")

    return check


def _relative(v: object) -> bool:
    """A path in the repository as git names it: not absolute, no empty, `.` or `..` part."""
    if not (isinstance(v, str) and v.strip()):
        return False
    return all(p not in ("", ".", "..") for p in v.split("/"))


def _list(item: Check) -> Check:
    def check(v: object, at: str) -> None:
        if not isinstance(v, list):
            raise RreviewOutputError(f"{at}: expected a list")
        for i, x in enumerate(v):
            item(x, f"{at}[{i}]")

    return check


def _object(fields: dict[str, Check], optional: frozenset[str] = frozenset()) -> Check:
    def check(v: object, at: str) -> None:
        if not (isinstance(v, dict) and fields.keys() - optional <= v.keys() <= fields.keys()):
            extra = f" ({', '.join(sorted(optional))} optional)" if optional else ""
            raise RreviewOutputError(
                f"{at}: expected exactly the fields {', '.join(sorted(fields))}{extra}"
            )
        for key, field in fields.items():
            if key in v:
                field(v[key], f"{at}.{key}")

    return check


_STR = _is(lambda v: isinstance(v, str), "a string")
_TEXT = _is(lambda v: isinstance(v, str) and bool(v.strip()), "a non-blank string")
_U32 = _is(lambda v: type(v) is int and 0 <= v < 2**32, "an integer 0 to 2^32-1")
_U64 = _is(lambda v: type(v) is int and 0 <= v < 2**64, "an integer 0 to 2^64-1")
_PRIORITY = _is(lambda v: type(v) is int and 0 <= v <= 3, "an integer 0-3")
# serde writes an f64 as a JSON float; NaN and infinities fail the comparison.
_CONFIDENCE = _is(lambda v: type(v) is float and 0.0 <= v <= 1.0, "a number 0-1")
_FINDING = _object({
    "title": _TEXT, "body": _TEXT, "file": _is(_relative, "a path relative to the repository"),
    "start_line": _U32, "end_line": _U32, "priority": _PRIORITY, "confidence_score": _CONFIDENCE,
    "stage": _is(lambda v: v in STAGES, f"one of {', '.join(STAGES)}"),
    "candidate_ids": _list(_STR),
})  # fmt: skip
_DROPPED = _object({
    "id": _STR, "title": _STR, "panelist": _STR, "file": _STR,
    "line_range": _object({"start": _U32, "end": _U32}), "priority": _PRIORITY,
    "confidence_score": _CONFIDENCE, "claim": _STR, "drop_reason": _STR, "evidence": _STR,
})  # fmt: skip
_PASS = _object(
    {"role": _STR, "agent": _STR, "model": _STR, "effort": _STR, "status": _STR,
     "duration_ms": _U64, "error": _STR},
    optional=frozenset({"effort", "error"}),
)  # fmt: skip
_COMMIT_ID = _is(lambda v: isinstance(v, str) and bool(_COMMIT.fullmatch(v)), "a commit id")
_RESULT = _object({
    "findings": _list(_FINDING), "ungated_findings": _list(_FINDING),
    "dropped_candidates": _list(_DROPPED), "overall_correctness": _STR,
    "overall_explanation": _STR, "review_process": _STR, "base": _COMMIT_ID, "head": _COMMIT_ID,
    "passes": _list(_PASS), "degraded": _is(lambda v: type(v) is bool, "a boolean"),
    "sandbox": _is(lambda v: v == "external", "external (--sandbox external)"),
})  # fmt: skip


def _gated(f: dict) -> bool:
    return f["priority"] <= GATE_PRIORITY and f["confidence_score"] >= GATE_CONFIDENCE


def _finding(f: dict, at: str) -> dict:
    """One checked rreview finding as an rbench finding."""
    if f["start_line"] < 1 or f["end_line"] < f["start_line"]:
        raise RreviewOutputError(f"{at}: expected 1 <= start_line <= end_line")
    title, body = f["title"].strip(), f["body"].strip()
    return {
        "file": f["file"],
        "start_line": f["start_line"],
        "end_line": f["end_line"],
        "message": f"{title}\n\n{body}",
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
    _RESULT(result, "$")
    # Checked whole, so a malformed finding the gate left out still fails the trial.
    for i, f in enumerate(result["ungated_findings"]):
        _finding(f, f"$.ungated_findings[{i}]")
    gated = [_finding(f, f"$.findings[{i}]") for i, f in enumerate(result["findings"])]
    if result["findings"] != [f for f in result["ungated_findings"] if _gated(f)]:
        raise RreviewOutputError(
            "rreview: findings are not the ungated_findings at its default gate"
            f" (priority <= {GATE_PRIORITY}, confidence >= {GATE_CONFIDENCE})"
        )
    if (exit_code == EXIT_GATED) != bool(gated):
        raise RreviewOutputError(
            f"rreview: exit {exit_code} with {len(gated)} gated findings; exit 1 means some"
        )
    return gated
