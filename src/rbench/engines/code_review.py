"""The `code-review` engine: Claude Code's built-in `/code-review` (spec engine-configurations,
Engine adapters; design D13).

`engines/code-review.toml` declares it. The command ships with Claude Code, so the pinned Claude
Code version is the engine's version (pin kind `cli`). A trial runs one `reviewer` session on a
Claude model, from the `reviewed` checkout: `/code-review <PR number>` goes on stdin to `claude -p`,
the shims (design D5) add the model, the effort and the policy flags, and the review reads the PR
through the trial's `gh` stub. Its report is Markdown, the `result` of the CLI's JSON output, and
the pinned normalization step (design D9) copies its findings.
"""

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from rbench.engines.declaration import DeclarationError, Engine, Setting
from rbench.engines.normalization import Normalization, normalize
from rbench.engines.plain import CLAUDE_SUBTYPES, Launch
from rbench.ledger.trials import no_repeated_keys

if TYPE_CHECKING:
    from rbench.trial.compose import TrialProject

NAME = "code-review"
ROLE = "reviewer"
# No effort level is typed after the command: the review then runs at the session's `--effort`. A
# typed level would also be saved in the home and reused by later reviews.
COMMAND = "/code-review"
# The spike #25 launch, with the command on stdin. The container is the sandbox (design D6), so no
# tool call waits for a permission `-p` cannot give; with JSON output the review reports in prose.
ARGV = ("claude", "-p", "--permission-mode", "bypassPermissions", "--output-format", "json")


class CodeReviewOutputError(ValueError):
    """The session's output holds no review: the trial is `failed`."""


def bind(engine: Engine, source: str) -> Engine:
    """Check the `code-review` declaration: pinned by the Claude Code version, Markdown output,
    one `reviewer` role on `claude` whose model and effort the shim adds; `source` (its file)
    prefixes errors."""
    where = f"{source}: engine {NAME!r}"
    if (engine.pin_kind, engine.pin_ref) != ("cli", "claude"):
        raise DeclarationError(f"{where}: pin must be kind 'cli', ref 'claude' (its Claude Code)")
    if engine.output != "markdown":
        raise DeclarationError(f"{where}: output must be 'markdown': the review is prose")
    role = engine.roles.get(ROLE)
    if (
        engine.roles.keys() != {ROLE}
        or role is None
        or (role.cli, role.delivery) != ("claude", "flag")
    ):
        raise DeclarationError(
            f"{where}: needs exactly one role {ROLE!r}, with cli = 'claude' and delivery = 'flag'"
        )
    return engine


def requested(settings: Mapping) -> Setting:
    """The model and effort the plan asks of the trial's session (and of the subagents it runs),
    to compare with the ones the CLI reports as applied (spec Applied model and effort)."""
    try:
        role = settings["roles"][ROLE]
        (model,) = role["models"]
        return Setting(model, role["effort"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{NAME}: not the settings of a {NAME} configuration") from None


def launch(pr: int) -> Launch:
    """The launch that reviews PR `pr`, the number the trial's `gh` stub answers for."""
    if not (type(pr) is int and pr > 0):
        raise ValueError(f"{NAME}: PR number {pr!r} is not a positive integer")
    return Launch("claude", ARGV, f"{COMMAND} {pr}\n", {}, None)


def report(stdout: str, where: str) -> str:
    """The Markdown report in the session's `--output-format json` result. Errors name `where`
    (the trial) and never quote the output: it is engine output, and may quote a hidden task."""
    try:
        result = json.loads(stdout, object_pairs_hook=no_repeated_keys)
    except (ValueError, RecursionError):
        raise CodeReviewOutputError(f"{where}: stdout is not one JSON object") from None
    if not (isinstance(result, dict) and result.get("type") == "result"):
        raise CodeReviewOutputError(f"{where}: stdout is not a result object")
    subtype = result.get("subtype")
    if result.get("is_error") is not False or subtype != "success":
        known = isinstance(subtype, str) and subtype in CLAUDE_SUBTYPES
        ended = f"subtype {subtype}" if known else "an unknown subtype"
        raise CodeReviewOutputError(f"{where}: the session did not succeed; it ended with {ended}")
    # An API error that ends the session still reports `success`, with no model usage (checked
    # offline with Claude Code 2.1.316): no review ran.
    if not (isinstance(result.get("modelUsage"), dict) and result["modelUsage"]):
        raise CodeReviewOutputError(f"{where}: the session made no model call")
    text = result.get("result")
    if not isinstance(text, str):
        raise CodeReviewOutputError(f"{where}: the result has no text")
    return text


def findings(normalizer: "TrialProject", stdout: str, trial: str) -> Normalization:
    """The findings of trial `trial` from its session's stdout, copied by the normalization step
    in `normalizer`, a fresh project the caller has started (`rbench.engines.normalization`)."""
    return normalize(normalizer, report(stdout, f"trial {trial}: {NAME}"), trial)
