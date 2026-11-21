"""The `ponytail-review` engine: a review plugin on Claude Code and on Codex (spec
engine-configurations, Engine adapters; design D5, Plugins at the pin; D13).

`engines/ponytail-review.toml` declares it, pinned by the plugin's release. `RELEASES` records
which releases the adapter was written against, with the source repository and the commit of each
release's tag; the agent image holds the plugin at that commit under `PLUGIN_DIR`. A trial runs
one `reviewer` session on the CLI of its model, from the `reviewed` checkout:

- Claude Code loads the plugin with `--plugin-dir` and gets the plugin's review command on stdin.
- Codex first installs the plugin into the trial's `CODEX_HOME` from the image's local marketplace
  (`setup`, offline), then gets the same request as a skill mention on stdin.

The shims (design D5) add the model, the effort and the policy flags. The review is Markdown, and
the pinned normalization step (design D9) copies its findings.

On Codex the plugin's lifecycle hooks are skipped as untrusted, as in #25: the trial home does not
pre-trust them, so that variant runs the review skill without them (design Risks; #125).
"""

import json
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from rbench.engines.declaration import DeclarationError, Engine, Setting
from rbench.engines.normalization import Normalization, normalize
from rbench.engines.pins import MODELS
from rbench.engines.plain import CLAUDE_SUBTYPES, Launch
from rbench.ledger.trials import no_repeated_keys
from rbench.trial.images import PLUGINS_DIR

if TYPE_CHECKING:
    from rbench.trial.compose import TrialProject

NAME = "ponytail-review"
ROLE = "reviewer"
PLUGIN = "ponytail"  # the plugin's own name, which namespaces its review skill
SOURCE = "https://github.com/DietrichGebert/ponytail"
# Release -> (tag, commit): the releases this adapter was written against. Another version is
# refused until its launch is checked, so that a new release is never benchmarked unseen.
RELEASES = {"5.3.3": ("refs/tags/v5.3.3", "7801d061243d7b753bad0e0b3c057cd250b11755")}
# Where the agent image holds the pinned plugin; that directory is also the local Codex marketplace
# that lists it (images/agent/Dockerfile).
MARKETPLACE_ROOT = PLUGINS_DIR
MARKETPLACE = "rbench"
PLUGIN_DIR = f"{MARKETPLACE_ROOT}/{PLUGIN}"
# The review of the trial's PR: the skill reviews what it is named, here the PR the `gh` stub
# answers for. The same request on both CLIs, in each CLI's syntax for a plugin's skill.
SKILL = f"{PLUGIN}:{PLUGIN}-review"
REQUEST = {"claude": f"/{SKILL} PR #{{pr}}\n", "codex": f"${SKILL} PR #{{pr}}\n"}
# The spike #25 launches, with the request on stdin. The container is the sandbox (design D6).
CLAUDE_ARGV = ("claude", "-p", "--plugin-dir", PLUGIN_DIR, "--permission-mode", "bypassPermissions",
               "--output-format", "json")  # fmt: skip
OUTPUT_PATH = "/home/node/review.md"  # outside the reviewed repository
CODEX_ARGV = ("codex", "exec", "--sandbox", "danger-full-access", "--json",
              "--output-last-message", OUTPUT_PATH, "-")  # fmt: skip
# Codex installs the plugin into CODEX_HOME from the image, with no network. The shims pass these
# non-session calls unchanged and log them.
CODEX_SETUP = (
    ("codex", "plugin", "marketplace", "add", MARKETPLACE_ROOT),
    ("codex", "plugin", "add", f"{PLUGIN}@{MARKETPLACE}"),
)


# The token counts that show a session's model calls: Claude Code 2.1.316's `modelUsage` entry, and
# the `usage` that Codex 0.171.0 requires in `turn.completed`.
CLAUDE_TOKENS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
CODEX_TOKENS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


class PluginReviewOutputError(ValueError):
    """The session's output holds no review: the trial is `failed`."""


def bind(engine: Engine, source: str) -> Engine:
    """Check the declaration: pinned by a known plugin release, Markdown output, both CLIs, one
    `reviewer` role whose model picks the CLI and whose model and effort the shims add; `source`
    (its file) prefixes errors."""
    where = f"{source}: engine {NAME!r}"
    if engine.pin_kind != "version" or engine.pin_ref not in RELEASES:
        raise DeclarationError(
            f"{where}: pin must be kind 'version' with a release the adapter knows:"
            f" {', '.join(sorted(RELEASES))} (from {SOURCE})"
        )
    if engine.output != "markdown":
        raise DeclarationError(f"{where}: output must be 'markdown': the review is prose")
    if set(engine.clis) != {"claude", "codex"}:
        raise DeclarationError(f"{where}: needs both cli.claude and cli.codex: it runs on both")
    role = engine.roles.get(ROLE)
    if engine.roles.keys() != {ROLE} or role is None or (role.cli, role.delivery) != (None, "flag"):
        raise DeclarationError(
            f"{where}: needs exactly one role {ROLE!r}, with no cli (the model picks it) and"
            " delivery = 'flag'"
        )
    return engine


def plugin(engine: Engine) -> tuple[str, str, str]:
    """(source repository, commit, release) of the plugin the engine's image must hold."""
    return SOURCE, RELEASES[engine.pin_ref][1], engine.pin_ref


def requested(settings: Mapping) -> Setting:
    """The model and effort the plan asks of the trial's session, to compare with the ones the CLI
    reports as applied (spec Applied model and effort)."""
    try:
        role = settings["roles"][ROLE]
        (model,) = role["models"]
        setting = Setting(model, role["effort"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{NAME}: not the settings of a {NAME} configuration") from None
    if model not in MODELS:
        raise ValueError(f"{NAME}: model {model!r} is not a known full model id")
    return setting


def setup(settings: Mapping) -> tuple[tuple[str, ...], ...]:
    """The commands to run in the trial before the launch: on Codex, installing the pinned plugin
    into the trial's `CODEX_HOME`; none on Claude Code, which loads it with `--plugin-dir`."""
    return CODEX_SETUP if MODELS[requested(settings).model] == "codex" else ()


def launch(settings: Mapping, pr: int) -> Launch:
    """The launch that reviews PR `pr`, the number the trial's `gh` stub answers for, on the CLI
    of the configuration's model."""
    cli = MODELS[requested(settings).model]
    if not (type(pr) is int and pr > 0):
        raise ValueError(f"{NAME}: PR number {pr!r} is not a positive integer")
    stdin = REQUEST[cli].format(pr=pr)
    if cli == "claude":
        return Launch(cli, CLAUDE_ARGV, stdin, {}, None)
    return Launch(cli, CODEX_ARGV, stdin, {}, OUTPUT_PATH)


def _refuse(constant: str) -> object:
    raise ValueError(f"{constant} is not JSON")  # NaN and the infinities Python would accept


def _json(text: str, where: str, what: str) -> object:
    try:
        return json.loads(text, object_pairs_hook=no_repeated_keys, parse_constant=_refuse)
    except (ValueError, RecursionError):
        raise PluginReviewOutputError(f"{where}: {what} is not JSON") from None


def _claude_report(stdout: str, where: str) -> str:
    result = _json(stdout, where, "stdout")
    if not (isinstance(result, dict) and result.get("type") == "result"):
        raise PluginReviewOutputError(f"{where}: stdout is not a result object")
    subtype = result.get("subtype")
    if result.get("is_error") is not False or subtype != "success":
        known = isinstance(subtype, str) and subtype in CLAUDE_SUBTYPES
        ended = f"subtype {subtype}" if known else "an unknown subtype"
        raise PluginReviewOutputError(
            f"{where}: the session did not succeed; it ended with {ended}"
        )
    # An API error that ends the session still reports `success`, with no model usage (#48).
    usage = result.get("modelUsage")
    if not (isinstance(usage, dict) and usage):
        raise PluginReviewOutputError(f"{where}: the session made no model call")
    # By position: the model key is engine output too.
    for n, counts in enumerate(usage.values(), 1):
        if not (isinstance(counts, dict) and _counts(counts, CLAUDE_TOKENS)):
            raise PluginReviewOutputError(f"{where}: modelUsage entry {n} has no token counts")
    text = result.get("result")
    if not isinstance(text, str):
        raise PluginReviewOutputError(f"{where}: the result has no text")
    return text


def _counts(counts: dict, keys: Iterable[str]) -> bool:
    return all(type(counts.get(k)) is int and counts[k] >= 0 for k in keys)


def _codex_report(stdout: str, answer: str | None, where: str) -> str:
    """The `--output-last-message` file, once `--json`'s events on stdout show a completed turn
    with its usage and no failed one, and the file holds the session's last message. A top-level
    `error` event alone fails nothing: Codex also emits it for an error it retries, and only its
    exit status tells the two apart (`report`)."""
    completed, last = False, None  # `last`: the text of the last agent message
    # JSONL records end at LF only: a JSON string may hold U+2028, which splitlines() splits on.
    for i, line in enumerate(stdout.split("\n")):
        if not line.strip():
            continue
        event = _json(line, where, f"stdout line {i + 1}")
        if not isinstance(event, dict):
            raise PluginReviewOutputError(f"{where}: stdout line {i + 1} is not an event")
        kind, item = event.get("type"), event.get("item")
        if not isinstance(kind, str):
            raise PluginReviewOutputError(f"{where}: stdout line {i + 1}: the event has no type")
        if kind == "turn.failed":
            raise PluginReviewOutputError(f"{where}: the session reported turn.failed")
        if kind == "turn.completed":
            usage = event.get("usage")
            if not (isinstance(usage, dict) and _counts(usage, CODEX_TOKENS)):
                raise PluginReviewOutputError(
                    f"{where}: stdout line {i + 1}: turn.completed has no token usage"
                )
            completed = True
        if (
            kind == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
        ):
            last = item.get("text")
            if not isinstance(last, str):
                raise PluginReviewOutputError(f"{where}: stdout line {i + 1}: message has no text")
    if not completed:
        raise PluginReviewOutputError(f"{where}: the session completed no turn")
    if answer is None:
        raise PluginReviewOutputError(f"{where}: the session wrote no {OUTPUT_PATH}")
    # With no last message the CLI writes an empty file: that is no review, not a blank one (D9).
    # A blank file is a blank review only when the last message is blank too.
    if not answer.strip():
        if last is None:
            raise PluginReviewOutputError(f"{where}: the session gave no final message")
        if last.strip():
            raise PluginReviewOutputError(
                f"{where}: {OUTPUT_PATH} is blank but the last message is not blank"
            )
    return answer


def report(launch: Launch, returncode: int, stdout: str, answer: str | None, where: str) -> str:
    """The Markdown review of a session of `launch` that exited with `returncode`: Claude's
    `--output-format json` result on stdout, or the file Codex wrote with `--output-last-message`
    (`answer`, None when missing). Errors name `where` (the trial) and never quote the output: it
    is engine output, and may quote a hidden task."""
    if returncode != 0:
        raise PluginReviewOutputError(f"{where}: the session exited with status {returncode}")
    if launch.cli == "claude":
        return _claude_report(stdout, where)
    return _codex_report(stdout, answer, where)


def findings(
    normalizer: "TrialProject",
    launch: Launch,
    returncode: int,
    stdout: str,
    answer: str | None,
    trial: str,
) -> Normalization:
    """The findings of trial `trial` from its session's exit status and output (see `report`),
    copied by the normalization step in `normalizer`, a fresh project the caller has started
    (`rbench.engines.normalization`)."""
    return normalize(
        normalizer, report(launch, returncode, stdout, answer, f"trial {trial}: {NAME}"), trial
    )
