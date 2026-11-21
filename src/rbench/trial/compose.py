"""One `docker compose` project per trial (design D3, spec trial-isolation).

The agent container sits only on `internal`, an internal bridge in isolated gateway mode: it has
no route out, no host address on the bridge and no external DNS. The proxy sits on `internal` and
on `egress`, an ordinary bridge, and is the agent's only way out. The agent's only mount is the
benchmark's Codex login home, when the trial runs Codex; the scrubbed repository is copied in
(`rbench.trial.repository`). The Claude token is declared in the compose file by variable name
only, and its value reaches `docker compose up` through its environment (spec trial-isolation,
Credentials).

The project name is the project's identity, and `up` removes whatever runs under it first. The
caller owns the name: unique to the run and the trial on the Docker daemon, stable across the
trial's retries, and reused only while the caller holds the run's lock (design D3).
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from rbench.atomic import atomic_write
from rbench.trial.credentials import CLAUDE_TOKEN_ENV, check_login_home
from rbench.trial.images import TrialError, docker, image_id

COMPOSE_FILE = "compose.json"
PROXY_URL = "http://proxy:3128"
PROXY_LOG = "/var/log/squid/access.log"
# Where the Codex login home is mounted; the trial's CODEX_HOME/auth.json links into it (D5).
CODEX_LOGIN = "/rbench/codex-login"
# Docker Compose's rule for project names.
_PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


def compose_spec(
    agent_image: str,
    proxy_image: str,
    claude_token: bool = False,
    codex_login: Path | None = None,
) -> dict:
    """The compose project of a trial; images are `<name>@sha256:<hex>` and run by digest.

    With `claude_token`, the agent's environment names the Claude token variable without a value,
    so Compose takes it from its own environment. `codex_login` is the absolute path of the Codex
    login home, mounted read-write at `CODEX_LOGIN`.
    """
    proxy_env = {
        name: PROXY_URL for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    }
    # An empty NO_PROXY: every name, the proxy's own aside, goes through the proxy.
    env: dict[str, str | None] = proxy_env | {"NO_PROXY": "", "no_proxy": ""}
    if claude_token:
        env[CLAUDE_TOKEN_ENV] = None
    spec = {
        "services": {
            "proxy": {
                "image": image_id(proxy_image),
                "pull_policy": "never",
                "networks": ["internal", "egress"],
            },
            "agent": {
                "image": image_id(agent_image),
                "pull_policy": "never",
                "networks": ["internal"],
                "environment": env,
                "command": ["sleep", "infinity"],
                "init": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "depends_on": {"proxy": {"condition": "service_healthy"}},
            },
        },
        "networks": {
            "internal": {
                "driver": "bridge",
                "internal": True,
                "driver_opts": {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"},
            },
            "egress": {"driver": "bridge"},
        },
    }
    if codex_login is not None:
        if not codex_login.is_absolute():
            raise ValueError(f"Codex login home {codex_login}: give an absolute path")
        # The long syntax never creates a missing source; a file mount would break Codex's
        # save by rename (EBUSY), so the whole directory is mounted (design D5).
        mount = {"type": "bind", "source": str(codex_login), "target": CODEX_LOGIN}
        spec["services"]["agent"]["volumes"] = [mount]
    return spec


@dataclass(frozen=True)
class TrialProject:
    """A trial's compose project: its name, the directory that holds its compose file, and the
    credentials its agent gets: the Claude token's value (kept in memory only, out of `repr`) and
    the Codex login home."""

    name: str
    directory: Path
    claude_token: str | None = field(default=None, repr=False, compare=False)
    codex_login: Path | None = None

    @classmethod
    def create(
        cls,
        directory: Path,
        name: str,
        agent_image: str,
        proxy_image: str,
        *,
        claude_token: str | None = None,
        codex_login: Path | None = None,
    ) -> "TrialProject":
        """Write the project's compose file into `directory`; start nothing.

        `claude_token` is the value from `claude_token()`; `codex_login` the benchmark's Codex
        login home, checked by `up`. The compose file holds neither value, only the variable's
        name and the login home's path.
        """
        if not _PROJECT_NAME.fullmatch(name):
            raise ValueError(f"compose project name {name!r}: use [a-z0-9][a-z0-9_-]*")
        codex_login = codex_login.absolute() if codex_login is not None else None
        directory.mkdir(parents=True, exist_ok=True)
        spec = compose_spec(agent_image, proxy_image, claude_token is not None, codex_login)
        atomic_write(directory / COMPOSE_FILE, json.dumps(spec, indent=2, sort_keys=True) + "\n")
        return cls(name, directory, claude_token, codex_login)

    def _compose(self, *argv: str, what: str, env: dict[str, str] | None = None) -> str:
        file = str(self.directory / COMPOSE_FILE)
        cmd = ("compose", "--project-name", self.name, "--file", file, *argv)
        return docker(*cmd, what=what, env=env)

    def up(self) -> None:
        """Start a fresh proxy and agent; return once the proxy is healthy.

        Compose identifies a project by its name and would adopt containers left under it, so
        whatever an earlier attempt left is removed first. A failed start removes what it created.
        A Codex login home that holds more than the login is refused before anything starts.
        """
        if self.codex_login is not None:
            check_login_home(self.codex_login)
        # The token only from this trial: never one the operator's shell happens to export.
        env = {k: v for k, v in os.environ.items() if k != CLAUDE_TOKEN_ENV}
        if self.claude_token is not None:
            env[CLAUDE_TOKEN_ENV] = self.claude_token
        self.down()
        try:
            self._compose(
                "up", "--detach", "--wait", what=f"trial project {self.name}: cannot start", env=env
            )
        except TrialError as e:
            try:
                self.down()
            except TrialError as cleanup:
                raise TrialError(f"{e}; its containers or networks may remain: {cleanup}") from e
            raise TrialError(f"{e}; removed what it created") from e

    def exec_agent(
        self, argv: list[str], stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run `argv` in the agent container; its exit status is the caller's to judge."""
        file = str(self.directory / COMPOSE_FILE)
        try:
            return subprocess.run(
                ["docker", "compose", "--project-name", self.name, "--file", file]
                + ["exec", "-T", "agent", *argv],
                check=False,
                capture_output=True,
                text=True,
                errors="replace",
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
            )
        except OSError as e:
            raise TrialError(f"trial project {self.name}: cannot run docker: {e}") from e

    def copy_proxy_log(self, dest: Path) -> None:
        """Copy the proxy's access log, every allowed and refused request, to `dest`."""
        what = f"copying the proxy log of trial project {self.name}"
        self._compose("cp", f"proxy:{PROXY_LOG}", str(dest), what=what)

    def copy_to_agent(self, src: Path, dest: str) -> None:
        """Copy `src` to `dest` in the agent, owned by the agent's user (`cp -a`), never mounted."""
        what = f"copying into the agent of trial project {self.name}"
        self._compose("cp", "--archive", str(src), f"agent:{dest}", what=what)

    def copy_from_agent(self, src: str, dest: Path) -> None:
        """Copy `src` in the agent to `dest`; what the engine wrote there is untrusted."""
        what = f"copying {src} from the agent of trial project {self.name}"
        self._compose("cp", f"agent:{src}", str(dest), what=what)

    def down(self) -> None:
        """Remove the project's containers and networks; the images stay for the next trial."""
        what = f"removing trial project {self.name}"
        self._compose("down", "--volumes", "--remove-orphans", "--timeout", "5", what=what)
