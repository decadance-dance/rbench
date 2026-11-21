"""A trial's hermetic configuration (design D5, spec trial-isolation, Hermetic configuration).

The agent image carries the policy for every CLI process, a shim's child or not: Claude Code's
managed settings (web tools denied, MCP from claude.ai, auto-memory, background tasks, telemetry,
error reporting, the autoupdater and non-essential traffic off, a SessionStart hook) and Codex's
`requirements.toml` (web search disabled, file credentials). The trial's homes, `CLAUDE_CONFIG_DIR`
and `CODEX_HOME`, start empty in every container, so no operator file reaches them.

Once the trial's project is up and before the engine starts, `prepare` starts the collector under
its own user, writes the shim settings, read-only to every trial process, and writes the engine's
own settings into the homes. The Codex login (#40) is linked into `CODEX_HOME` as `auth.json` by
the step that mounts it. After the engine ends, `collect` copies out the shim log, every
transcript the container's writable layer holds and the trial's launch record.
"""

import copy
import json
import re
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

from rbench.atomic import atomic_write
from rbench.engines.declaration import CLIS, Setting
from rbench.ledger.plan import Image
from rbench.trial.compose import COMPOSE_FILE, TrialProject
from rbench.trial.images import TrialError, docker
from rbench.trial.sessions import launch_record, read_shim_log

SHIM_SETTINGS = "/etc/rbench/shim.json"
COLLECTOR = "/opt/rbench/lib/collector.py"
COLLECTOR_USER = "rbench-collector"
SOCKET = "/run/rbench/collector.sock"
SHIM_LOG = "/var/log/rbench/shim.jsonl"
CLAUDE_HOME = "/home/node/.claude"
CODEX_HOME = "/home/node/.codex"
# The policy flags each shim repeats on a session launch (design D5). Codex takes `--ignore-rules`
# only right after `exec`.
POLICY = {
    "claude": {"flags": ["--strict-mcp-config", "--disallowedTools=WebSearch,WebFetch"]},
    "codex": {
        "flags": ["--strict-config", "-c", 'web_search="disabled"'],
        "exec_flags": ["--ignore-rules"],
    },
}
COLLECTOR_WAIT_SECONDS = 10.0
# What `collect` copies: Claude transcripts (subagents under their session) and Codex rollouts,
# wherever a process wrote them.
TRANSCRIPT = re.compile(
    r"(/projects/[^/]+/[^/]+\.jsonl|/projects/[^/]+/[^/]+/subagents/[^/]+\.jsonl"
    r"|/rollout-[^/]*\.jsonl)$"
)
# Writes stdin to "$1" with mode "$2", in the container and as the exec's user.
WRITE = 'umask 077 && cat > "$1" && chmod "$2" "$1"'
LOG_FILE = "shim.jsonl"
TRANSCRIPTS_DIR = "transcripts"
RECORD_FILE = "launches.json"
_TOML_KEY = re.compile(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*")


def shim_settings(by_flag: Mapping[str, Setting]) -> dict:
    """The shim settings file: the policy flags, and for each CLI in `by_flag` the model and
    effort of the role that CLI runs by flag. Public launch settings only: no credential, no task.
    """
    if unknown := set(by_flag) - set(CLIS):
        raise ValueError(f"shim settings: unknown CLI {', '.join(sorted(unknown))}")
    settings: dict = {"socket": SOCKET}
    for cli in CLIS:
        settings[cli] = copy.deepcopy(POLICY[cli])
        if cli in by_flag:
            settings[cli] |= {"model": by_flag[cli].model, "effort": by_flag[cli].effort}
    return settings


def codex_config(entries: Mapping[str, str]) -> str:
    """A `config.toml` of string values, such as the model and effort of a role delivered by
    config (`Delivery.config`); keys may be dotted."""
    lines = []
    for key, value in sorted(entries.items()):
        if not _TOML_KEY.fullmatch(key) or not isinstance(value, str):
            raise ValueError(f"codex config: bad entry {key!r} = {value!r}")
        lines.append(f"{key} = {json.dumps(value)}\n")
    return "".join(lines)


def _exec(
    project: TrialProject,
    argv: list[str],
    *,
    what: str,
    user: str | None = None,
    stdin: str | None = None,
    detach: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "compose", "--project-name", project.name]
    cmd += ["--file", str(project.directory / COMPOSE_FILE), "exec", "-T"]
    cmd += ["--detach"] * detach + ["--user", user] * (user is not None) + ["agent", *argv]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
        )
    except OSError as e:
        raise TrialError(f"trial project {project.name}: {what}: cannot run docker: {e}") from e
    if check and result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or ["no output"])[-1]
        raise TrialError(
            f"trial project {project.name}: {what}: exited {result.returncode}: {detail}"
        )
    return result


def _write(project: TrialProject, path: str, text: str, mode: str, user: str | None) -> None:
    argv = ["sh", "-c", WRITE, "sh", path, mode]
    _exec(project, argv, what=f"writing {path}", user=user, stdin=text)


def prepare(
    project: TrialProject,
    settings: dict,
    codex_config: str | None = None,
    claude_settings: dict | None = None,
) -> None:
    """Start the collector, write the shim settings and the engine's settings into the homes.

    `settings` is `shim_settings(...)`; `codex_config` becomes `$CODEX_HOME/config.toml` and
    `claude_settings` `$CLAUDE_CONFIG_DIR/settings.json`.
    """
    _exec(project, [COLLECTOR], what="starting the collector", user=COLLECTOR_USER, detach=True)
    deadline = time.monotonic() + COLLECTOR_WAIT_SECONDS
    while _exec(project, ["test", "-S", SOCKET], what="", check=False).returncode != 0:
        if time.monotonic() > deadline:
            raise TrialError(f"trial project {project.name}: the collector did not open {SOCKET}")
        time.sleep(0.1)
    text = json.dumps(settings, indent=2, sort_keys=True) + "\n"
    _write(project, SHIM_SETTINGS, text, "0444", user="0")
    if codex_config is not None:
        _write(project, f"{CODEX_HOME}/config.toml", codex_config, "0600", user=None)
    if claude_settings is not None:
        text = json.dumps(claude_settings, indent=2, sort_keys=True) + "\n"
        _write(project, f"{CLAUDE_HOME}/settings.json", text, "0600", user=None)


def _compose(project: TrialProject, *argv: str, what: str) -> str:
    file = str(project.directory / COMPOSE_FILE)
    return docker("compose", "--project-name", project.name, "--file", file, *argv, what=what)


def collect(project: TrialProject, image: Image, dest: Path) -> list[str]:
    """Copy the shim log and every transcript in the agent's writable layer into `dest`, and write
    the trial's launch record there; return the transcripts' paths in the container.

    The transcripts are found by `docker diff`, not by the homes the shim log names, so a session
    started without the shims is found too (design D5).
    """
    where = f"trial project {project.name}"
    dest.mkdir(parents=True, exist_ok=True)
    _compose(project, "cp", f"agent:{SHIM_LOG}", str(dest / LOG_FILE), what=f"{where}: shim log")
    container = _compose(project, "ps", "--quiet", "agent", what=f"{where}: agent id").strip()
    if not container:
        raise TrialError(f"{where}: no agent container to collect from")
    changed = docker("diff", container, what=f"{where}: listing written files")
    written = (line[2:] for line in changed.splitlines() if line[:2] in ("A ", "C "))
    found = sorted(path for path in written if TRANSCRIPT.search(path))
    for path in found:
        target = dest / TRANSCRIPTS_DIR / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        _compose(project, "cp", f"agent:{path}", str(target), what=f"{where}: copying {path}")
    record = launch_record(image, read_shim_log(dest / LOG_FILE))
    atomic_write(dest / RECORD_FILE, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return found
