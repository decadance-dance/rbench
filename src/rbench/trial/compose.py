"""One `docker compose` project per trial (design D3, spec trial-isolation).

The agent container sits only on `internal`, an internal bridge in isolated gateway mode: it has
no route out, no host address on the bridge and no external DNS. The proxy sits on `internal` and
on `egress`, an ordinary bridge, and is the agent's only way out. Nothing is mounted into the
agent here: the scrubbed repository is copied in (#39) and the Codex login home mounted (#40) by
the steps that own them.

The project name is the project's identity, and `up` removes whatever runs under it first. The
caller owns the name: unique to the run and the trial on the Docker daemon, stable across the
trial's retries, and reused only while the caller holds the run's lock (design D3).
"""

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rbench.atomic import atomic_write
from rbench.trial.images import TrialError, docker, image_id

COMPOSE_FILE = "compose.json"
PROXY_URL = "http://proxy:3128"
PROXY_LOG = "/var/log/squid/access.log"
# Docker Compose's rule for project names.
_PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


def compose_spec(agent_image: str, proxy_image: str) -> dict:
    """The compose project of a trial; images are `<name>@sha256:<hex>` and run by digest."""
    proxy_env = {
        name: PROXY_URL for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    }
    return {
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
                # An empty NO_PROXY: every name, the proxy's own aside, goes through the proxy.
                "environment": proxy_env | {"NO_PROXY": "", "no_proxy": ""},
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


@dataclass(frozen=True)
class TrialProject:
    """A trial's compose project: its name and the directory that holds its compose file."""

    name: str
    directory: Path

    @classmethod
    def create(
        cls, directory: Path, name: str, agent_image: str, proxy_image: str
    ) -> "TrialProject":
        """Write the project's compose file into `directory`; start nothing."""
        if not _PROJECT_NAME.fullmatch(name):
            raise ValueError(f"compose project name {name!r}: use [a-z0-9][a-z0-9_-]*")
        directory.mkdir(parents=True, exist_ok=True)
        spec = compose_spec(agent_image, proxy_image)
        atomic_write(directory / COMPOSE_FILE, json.dumps(spec, indent=2, sort_keys=True) + "\n")
        return cls(name, directory)

    def _compose(self, *argv: str, what: str) -> str:
        file = str(self.directory / COMPOSE_FILE)
        return docker("compose", "--project-name", self.name, "--file", file, *argv, what=what)

    def up(self) -> None:
        """Start a fresh proxy and agent; return once the proxy is healthy.

        Compose identifies a project by its name and would adopt containers left under it, so
        whatever an earlier attempt left is removed first. A failed start removes what it created.
        """
        self.down()
        try:
            self._compose(
                "up", "--detach", "--wait", what=f"trial project {self.name}: cannot start"
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
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
            )
        except OSError as e:
            raise TrialError(f"trial project {self.name}: cannot run docker: {e}") from e

    def copy_proxy_log(self, dest: Path) -> None:
        """Copy the proxy's access log, every allowed and refused request, to `dest`."""
        what = f"copying the proxy log of trial project {self.name}"
        self._compose("cp", f"proxy:{PROXY_LOG}", str(dest), what=what)

    def down(self) -> None:
        """Remove the project's containers and networks; the images stay for the next trial."""
        what = f"removing trial project {self.name}"
        self._compose("down", "--volumes", "--remove-orphans", "--timeout", "5", what=what)
