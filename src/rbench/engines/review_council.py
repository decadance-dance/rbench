"""The `review-council` engine (spec engine-configurations, Engine adapters; design D5, D9, D13).

`engines/review-council.toml` declares it, pinned by the plugin version; `RELEASES` records the
source commit of each version this adapter was written against. The agent image holds the plugin
at that commit in `PLUGIN_DIR` (`plugin`).

A trial runs one Claude Code session, `claude -p "/review-council:run"`, from the trial's
repository. The command has no argument: review-council then finds the PR of the current branch
with `gh pr view`, which the trial's gh stub answers, so the argv holds nothing of the task.

- The Claude side (role `claude_side`) is that session, orchestrator and judge, and its `model:
  inherit` subagents. The shim adds the role's `--model` and `--effort` to the launch.
- The Codex side (role `codex_side`) is the Codex reviewer and verifier. review-council 0.14.0
  launches them as `codex exec --sandbox read-only --skip-git-repo-check -o <file>` with no model
  flag, so their model and effort come only from the trial's `config.toml` (`trial_homes`). The
  shim adds no model to Codex, and rewrites the read-only sandbox (design D5).
- review-council runs as a fresh install: the adapter owns the `.review-council/` state the pinned
  skill reads from the checkout. `config.local.yml` (`engines/review-council/config.local.yml`)
  turns the Google and Perplexity seats off, `config.yml` sets nothing and `learnings.md` is
  empty, so committed copies change neither the configuration nor the judge's suppressions. They
  replace committed copies in the work tree only, and git does not show them (`prepare_checkout`
  and the trial home's git ignore file).

The report is the session's last text that holds the pinned report heading, Markdown from that
heading on, which the pinned normalization step copies into findings (design D9). A Codex session that did not run the Codex side's model (a fallback to
Codex's default) makes the trial `misconfigured`: `check_launches` finds a Codex launch that
overrides the config's model in the shim log, and `model_mismatches` compares the models each
session applied (read from the transcripts, #46) with `requested`.
"""

import functools
import importlib.util
import itertools
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

from rbench.engines.declaration import DeclarationError, Engine, Setting
from rbench.engines.plain import CLAUDE_SUBTYPES, Launch
from rbench.ledger.trials import no_repeated_keys
from rbench.trial.hermetic import codex_config, shim_settings
from rbench.trial.images import IMAGES_DIR, PLUGINS_DIR
from rbench.trial.repository import REPO_DIR, Runner

NAME = "review-council"
REPOSITORY = "https://github.com/deployhq/review-council"
# Plugin version -> the commit of REPOSITORY it was read at. A new version joins here after its
# launches and config keys are checked.
RELEASES = {"0.14.0": "c9a73575503c12277cd46409c7cbdbe452e7c582"}
ROLES = {"claude_side": "claude", "codex_side": "codex"}
# Where `build_agent_image(plugin=plugin(version))` installs it: the name in its plugin.json.
PLUGIN_DIR = f"{PLUGINS_DIR}/review-council"
COMMAND = "/review-council:run"
# The tools of the session spike #25 ran; the plugin dispatches its reviewers with Agent and runs
# its scripts (the Codex reviewer among them) with Bash.
CLAUDE_TOOLS = "Bash Read Grep Glob Agent Skill Write"
CONFIG_FILE = "config.local.yml"
STATE_DIR = ".review-council"
CONFIG_PATH = f"{REPO_DIR}/{STATE_DIR}/{CONFIG_FILE}"
# What the pinned skill reads from the checkout besides config.local.yml: rc-config.sh merges
# config.yml under it (list keys and custom seats included, which config.local.yml cannot undo),
# and Step 0.5 recall reads learnings.md into the reviewers' context and the judge's suppressions.
STATE = {
    "config.yml": b"# Written by rbench: no committed configuration applies (design D13).\n{}\n",
    "learnings.md": b"",
}
STATE_PATHS = tuple(f"{STATE_DIR}/{name}" for name in (CONFIG_FILE, *STATE))
# Git's default core.excludesFile in the trial home: keeps the files out of `git status` while
# the checkout does not track them. `prepare_checkout` hides the tracked ones.
# limit: a committed .gitignore that negates these paths outranks this file.
GIT_IGNORE = "/home/node/.config/git/ignore"
REPORT_HEADING = re.compile(r"^## Review Council Report[ \t]*$", re.MULTILINE)
_MALFORMED = "claude: stdout holds a malformed, truncated or ambiguous stream-json event"
# Codex options that would set the model or effort over the trial's config.toml, or skip it.
_CODEX_OWN = ("-m", "--model", "-p", "--profile", "--ignore-user-config")
_CODEX_KEYS = ("model", "model_reasoning_effort", "model_provider", "profile")


class ReviewCouncilOutputError(ValueError):
    """The session gave no report: the trial is `failed`."""


def bind(engine: Engine, engines_dir: Path) -> Engine:
    """Check the `review-council` declaration against what this adapter supports."""
    where = f"{Path(engines_dir) / (NAME + '.toml')}: engine {NAME!r}"
    if engine.pin_kind != "version" or engine.pin_ref not in RELEASES:
        raise DeclarationError(
            f"{where}: pin must be the plugin version of a release this adapter knows:"
            f" {', '.join(RELEASES)}"
        )
    if engine.output != "markdown":
        raise DeclarationError(f"{where}: output must be 'markdown' (the council's report)")
    roles = {name: (r.cli, r.delivery) for name, r in engine.roles.items()}
    if roles != {"claude_side": ("claude", "flag"), "codex_side": ("codex", "config")}:
        raise DeclarationError(
            f"{where}: needs exactly the roles claude_side (cli = 'claude', delivery = 'flag')"
            " and codex_side (cli = 'codex', delivery = 'config')"
        )
    config = Path(engines_dir) / NAME / CONFIG_FILE
    if not config.is_file():
        raise DeclarationError(f"{where}: no {config}")
    return engine


def plugin(version: str) -> tuple[str, str, str]:
    """The plugin `rbench.trial.build_agent_image` installs for this version of the engine."""
    if version not in RELEASES:
        raise ValueError(f"{NAME}: no release {version!r} this adapter knows")
    return REPOSITORY, RELEASES[version], version


def requested(settings: Mapping) -> dict[str, Setting]:
    """CLI -> the model and effort every session on it requests: the Claude session and its
    subagents, and every Codex session. For recording them as applied (spec Applied model and
    effort are recorded)."""
    where = f"{NAME} configuration {settings.get('roles')}"
    if settings.get("version") not in RELEASES:
        raise ValueError(f"{where}: not a release of review-council this adapter knows")
    try:
        result = {}
        for role, cli in ROLES.items():
            (model,) = settings["roles"][role]["models"]
            result[cli] = Setting(model, settings["roles"][role]["effort"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(
            f"{where}: not the settings of a review-council configuration; each role needs"
            " exactly one model and an effort"
        ) from None
    return result


def trial_homes(settings: Mapping) -> tuple[dict, str]:
    """The shim settings and the `config.toml` of the trial (`rbench.trial.hermetic.prepare`):
    the shim gives Claude its model and effort, the config gives them to Codex."""
    want = requested(settings)
    codex = want["codex"]
    config = codex_config({"model": codex.model, "model_reasoning_effort": codex.effort})
    return shim_settings({"claude": want["claude"]}), config


def launch(engines_dir: Path, settings: Mapping) -> Launch:
    """The launch of the configuration with these frozen `settings`, run in the trial's
    repository after `config.local.yml` is written there."""
    requested(settings)
    config = Path(engines_dir) / NAME / CONFIG_FILE
    try:
        files = {CONFIG_PATH: config.read_bytes()}
    except OSError as e:
        raise ValueError(f"{NAME}: cannot read {config}: {e.strerror}") from None
    files |= {f"{REPO_DIR}/{STATE_DIR}/{name}": data for name, data in STATE.items()}
    files[GIT_IGNORE] = "".join(f"/{path}\n" for path in STATE_PATHS).encode()
    argv = ("claude", "-p", COMMAND, "--plugin-dir", PLUGIN_DIR, "--setting-sources", "user")
    argv += ("--allowedTools", CLAUDE_TOOLS, "--output-format", "stream-json", "--verbose")
    return Launch("claude", argv, "", files, None)


def prepare_checkout(run: Runner, repo: str = REPO_DIR) -> None:
    """The adapter's checkout hook: run in the agent after `launch().files` are written and
    before the engine starts. Marks the `.review-council/` files the checkout tracks
    skip-worktree, so `git status` and `git diff` do not show that the launch replaced them.
    Idempotent, and touches only those paths."""
    listed = run(["git", "-C", repo, "ls-files", "-z", "--", *STATE_PATHS])
    if tracked := [path for path in listed.split("\0") if path]:
        run(["git", "-C", repo, "update-index", "--skip-worktree", "--", *tracked])


@functools.cache
def _shim() -> ModuleType:
    """The agent image's shim (`images/agent/lib/shim.py`), for its Codex option scan."""
    spec = importlib.util.spec_from_file_location(
        "rbench_agent_shim", IMAGES_DIR / "agent" / "lib" / "shim.py"
    )
    module = importlib.util.module_from_spec(spec)
    dont_write, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # keep the image context
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = dont_write
    return module


def _codex_override(argv: list) -> str | None:
    """The option of a Codex argv that sets the model or effort over config.toml, or skips it.
    Reads options as Codex and the shim do: up to `--` or the first positional argument, with
    `--name=value` and attached short values (`-mgpt`, `-ckey=value`)."""
    if not all(isinstance(a, str) for a in argv):
        return "a non-string argument"
    shim = _shim()
    options = shim.codex_options(argv)
    i = 0
    while i < len(options):
        a = options[i]
        if a in shim.CODEX_VALUE_OPTIONS:
            name, value = a, options[i + 1] if i + 1 < len(options) else ""
            i += 2
        else:
            i += 1
            if a.startswith("--"):
                name, _, value = a.partition("=")
            elif a.startswith("-"):
                name, value = a[:2], a[2:]
            else:
                continue  # a command
        if name in _CODEX_OWN:
            return name
        key = value.split("=", 1)[0].strip()
        if name in ("-c", "--config") and key in _CODEX_KEYS:
            return f"-c {key}"
    return None


def _sandbox_rewrite(old: object, new: object) -> bool:
    """The shim's one rewrite of an engine flag: the read-only sandbox (design D5)."""
    return (
        isinstance(old, str)
        and "read-only" in old
        and new == old.replace("read-only", "danger-full-access")
    )


def check_launches(records: list[dict], launch: Launch, settings: Mapping) -> list[str]:
    """What in the shim log shows that the trial did not run the configuration: the one Claude
    session is not the launch the adapter built or did not get the Claude side's model and
    effort, or a Codex session could not have run the Codex side's model and effort from the
    trial's config.toml (an own model, effort or profile, or the config ignored), or the shim
    rewrote anything but the read-only sandbox. Any of these makes the trial `misconfigured`.

    A session launch is one the shim added flags to; `--version` and other calls get none."""
    claude = requested(settings)["claude"]
    launches = [r for r in records if r.get("kind") == "launch" and r.get("added")]
    sessions = [r for r in launches if r.get("cli") == "claude"]
    if len(sessions) != 1:
        return [f"expected one Claude session launch in the shim log, found {len(sessions)}"]
    (session,) = sessions
    problems = []
    if session.get("argv") != list(launch.argv[1:]):
        problems.append(f"the Claude launch is not the argv the {NAME} adapter built")
    final = session.get("final_argv")
    pairs = set(itertools.pairwise(final)) if isinstance(final, list) else set()
    for flag, value in (("--model", claude.model), ("--effort", claude.effort)):
        if (flag, value) not in pairs:
            problems.append(f"the Claude session did not get {flag} {value}")
    for i, record in enumerate(r for r in launches if r.get("cli") == "codex"):
        argv = record.get("argv")
        own = _codex_override(argv) if isinstance(argv, list) else "an unreadable argv"
        if own is not None:
            problems.append(
                f"Codex session {i} sets {own}, so the trial's config.toml model and effort did"
                " not apply"
            )
        rewrites = record.get("rewrites") or []
        if not all(
            isinstance(r, dict) and _sandbox_rewrite(r.get("from"), r.get("to")) for r in rewrites
        ):
            problems.append(f"the shim rewrote more than the sandbox of Codex session {i}")
    return problems


def model_mismatches(settings: Mapping, applied: Mapping[str, tuple[str, str]]) -> list[str]:
    """The sessions that ran another model than their side requested, which makes the trial
    `misconfigured` (spec Applied model and effort are recorded: Wrong model). `applied` maps a
    session (its transcript) to its CLI and the model the CLI reports it ran; a Codex session on
    Codex's default model, because the trial's config.toml did not apply, is one of them."""
    want = requested(settings)
    problems = []
    for session, (cli, model) in sorted(applied.items()):
        if cli not in want:
            problems.append(f"session {session}: unknown CLI {cli!r}")
        elif model != want[cli].model:
            problems.append(
                f"session {session}: {cli} ran model {model!r}, the configuration requested"
                f" {want[cli].model!r}"
            )
    return problems


def _events(stdout: str) -> list[dict]:
    """The stream-json events of `stdout`, one JSON object per `\n`-ended line. Lines before the
    first one that starts with `{` are a tolerated preamble; from there on every line must be a
    whole event with no repeated key."""
    lines = stdout.split("\n")  # not splitlines: U+2028 and the like may sit inside a string
    if lines[-1] == "":
        lines.pop()
    start = next((i for i, line in enumerate(lines) if line.startswith("{")), len(lines))
    events = []
    for line in lines[start:]:
        try:
            event = json.loads(line, object_pairs_hook=no_repeated_keys)
        except (ValueError, RecursionError):
            raise ReviewCouncilOutputError(_MALFORMED) from None
        if not (isinstance(event, dict) and isinstance(event.get("type"), str)):
            raise ReviewCouncilOutputError(_MALFORMED)
        events.append(event)
    return events


def _texts(event: dict) -> list[str]:
    """The text blocks of a main-session `assistant` event; subagents' events have a parent."""
    if event["type"] != "assistant" or event.get("parent_tool_use_id") is not None:
        return []
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list) or not all(isinstance(b, dict) for b in content):
        raise ReviewCouncilOutputError(_MALFORMED)
    texts = [b.get("text") for b in content if b.get("type") == "text"]
    if not all(isinstance(t, str) for t in texts):
        raise ReviewCouncilOutputError(_MALFORMED)
    return texts


def report(stdout: str) -> str:
    """The council's Markdown report from the session's `--output-format stream-json` stdout: the
    last main-session text, the final `result` included, that holds the pinned report heading
    (skills/run/SKILL.md, Step 6), from that heading on. The skill may go on after the report
    (re-reading its config, a posting offer, the capture gate), so the final message alone is
    not the report. It goes to `rbench.engines.normalization.normalize`. Raises
    `ReviewCouncilOutputError` when the stream is malformed, the session did not succeed, or it
    gave no report; errors never quote engine output, which may quote a hidden task."""
    events = _events(stdout)
    results = [e for e in events if e["type"] == "result"]
    if len(results) != 1:
        raise ReviewCouncilOutputError(
            f"claude: expected one result event on stdout, found {len(results)}"
        )
    (result,) = results
    if events[-1] is not result:
        raise ReviewCouncilOutputError("claude: the result event is not the last event on stdout")
    subtype = result.get("subtype")
    if result.get("is_error") is not False or subtype != "success":
        known = isinstance(subtype, str) and subtype in CLAUDE_SUBTYPES
        ended = f"subtype {subtype}" if known else "an unknown subtype"
        raise ReviewCouncilOutputError(
            f"claude: the session did not succeed; it ended with {ended}"
        )
    final = result.get("result")
    if not isinstance(final, str):
        raise ReviewCouncilOutputError(_MALFORMED)
    texts = [t for e in events for t in _texts(e)] + [final]
    for text in reversed(texts):
        if heading := REPORT_HEADING.search(text):
            return text[heading.start() :]
    raise ReviewCouncilOutputError(
        "claude: the session gave no council report (no '## Review Council Report' heading)"
    )
