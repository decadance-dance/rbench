"""A trial's hermetic configuration (design D5, spec trial-isolation, Hermetic configuration).

The agent image carries the policy for every CLI process, a shim's child or not: Claude Code's
managed settings (web tools denied, MCP from claude.ai, auto-memory, background tasks, telemetry,
error reporting, the autoupdater and non-essential traffic off, a SessionStart hook) and Codex's
`requirements.toml` (web search disabled, file credentials). The trial's homes, `CLAUDE_CONFIG_DIR`
and `CODEX_HOME`, start empty in every container, so no operator file reaches them.

Once the trial's project is up and before the engine starts, `prepare` starts the collector under
its own user, writes the shim settings, read-only to every trial process, and writes the engine's
own settings into the homes. When the trial mounts the Codex login home, `CODEX_HOME/auth.json`
becomes a symlink into it: Codex follows it, so a refresh, its lock and its temporary save file
land in the login home and nowhere else (design D5). After the engine ends, `collect` copies out
the shim log, every transcript the container's writable layer holds and the trial's launch record.
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
from rbench.trial.compose import CODEX_LOGIN, COMPOSE_FILE, TrialProject
from rbench.trial.credentials import LOGIN_FILE
from rbench.trial.images import TrialError, docker, host_env
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
# What Codex needs of its login home, tried as the trial's user (design D5): read `auth.json` and
# open it for writing (Codex 0.171.0 does before it replaces it), create its temporary save file
# next to it, and open the lock for writing. `<>` and `>>` open without truncating, so no content
# changes; the probe file is a transient `.auth-*` file and is removed. `true`, not `:`: a failed
# redirection on a special builtin exits the shell without the reason.
LOGIN_PROBE = f"""\
cd {CODEX_LOGIN} || exit 1
{{ true < auth.json; }} 2>/dev/null || {{ echo "cannot read auth.json" >&2; exit 1; }}
{{ true 1<> auth.json; }} 2>/dev/null || {{ echo "cannot write auth.json" >&2; exit 1; }}
probe=$(mktemp -p . .auth-probe.XXXXXX 2>/dev/null) || {{ echo "cannot create files" >&2; exit 1; }}
rm -f "$probe"
if [ -e auth.json.lock ]; then
  {{ true >> auth.json.lock; }} 2>/dev/null || {{ echo "cannot write auth.json.lock" >&2; exit 1; }}
fi
"""
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
            errors="replace",
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            env=host_env(),
        )
    except OSError as e:
        where = f"trial project {project.name}: {what}"
        raise TrialError(f"{where}: cannot run docker: {e.strerror}") from None
    if check and result.returncode != 0:
        detail = project.redact((result.stderr.strip().splitlines() or ["no output"])[-1])
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
    `claude_settings` `$CLAUDE_CONFIG_DIR/settings.json`. With the project's Codex login home,
    `$CODEX_HOME/auth.json` links to its `auth.json`, once the trial's user is shown to be able
    to read the login and write the home and its lock.
    """
    _exec(project, [COLLECTOR], what="starting the collector", user=COLLECTOR_USER, detach=True)
    deadline = time.monotonic() + COLLECTOR_WAIT_SECONDS
    while _exec(project, ["test", "-S", SOCKET], what="", check=False).returncode != 0:
        if time.monotonic() > deadline:
            raise TrialError(f"trial project {project.name}: the collector did not open {SOCKET}")
        time.sleep(0.1)
    text = json.dumps(settings, indent=2, sort_keys=True) + "\n"
    _write(project, SHIM_SETTINGS, text, "0444", user="0")
    if project.codex_login is not None:
        what = f"checking that the trial's user can use the Codex login home {CODEX_LOGIN}"
        _exec(project, ["sh", "-c", LOGIN_PROBE], what=what)
        link = ["ln", "-s", f"{CODEX_LOGIN}/{LOGIN_FILE}", f"{CODEX_HOME}/{LOGIN_FILE}"]
        _exec(project, link, what="linking the Codex login")
    if codex_config is not None:
        _write(project, f"{CODEX_HOME}/config.toml", codex_config, "0600", user=None)
    if claude_settings is not None:
        text = json.dumps(claude_settings, indent=2, sort_keys=True) + "\n"
        _write(project, f"{CLAUDE_HOME}/settings.json", text, "0600", user=None)


def collect(project: TrialProject, image: Image, dest: Path) -> list[str]:
    """Copy the shim log and every transcript in the agent's writable layer into `dest`, and write
    the trial's launch record there; return the transcripts' paths in the container.

    The transcripts are found by `docker diff`, not by the homes the shim log names, so a session
    started without the shims is found too (design D5).
    """
    where = f"trial project {project.name}"
    dest.mkdir(parents=True, exist_ok=True)
    compose = project._compose

    def take(source: str, target: Path, what: str) -> None:
        # The engine names its transcripts and may have planted links: neither the path nor
        # docker's message about it is quoted.
        try:
            compose("cp", f"agent:{source}", str(target), what=what)
        except TrialError:
            raise TrialError(f"{where}: copying {what} out of the agent failed") from None

    take(SHIM_LOG, dest / LOG_FILE, "the shim log")
    container = compose("ps", "--quiet", "agent", what=f"{where}: agent id").strip()
    if not container:
        raise TrialError(f"{where}: no agent container to collect from")
    secrets = [project.claude_token] if project.claude_token else []
    changed = docker("diff", container, what=f"{where}: listing written files", secrets=secrets)
    written = (line[2:] for line in changed.splitlines() if line[:2] in ("A ", "C "))
    found = sorted(path for path in written if TRANSCRIPT.search(path))
    if project.claude_token and any(project.claude_token in path for path in found):
        # The path itself would reach an argv, the run directory and the result.
        raise TrialError(f"{where}: a transcript's path holds the Claude token; nothing copied")
    for n, path in enumerate(found, 1):
        target = dest / TRANSCRIPTS_DIR / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        take(path, target, f"transcript {n} of {len(found)}")
    record = launch_record(image, read_shim_log(dest / LOG_FILE))
    atomic_write(dest / RECORD_FILE, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return found
