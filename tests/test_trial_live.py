"""The trial sandbox on a real Docker engine (design D3): docker only, no credentials, no model call.

Run: RBENCH_LIVE=1 uv run pytest -m live tests/test_trial_live.py -s
It builds both images under tags of its own and removes, by name, every project, listener and tag
it created, also when a step fails.
"""

import json
import subprocess
import uuid

import pytest

from rbench.trial import (
    TrialError,
    TrialProject,
    build_agent_image,
    build_proxy_image,
    image_id,
)

pytestmark = pytest.mark.live

CLIS = {"claude": "2.1.316", "codex": "0.171.0"}  # the versions the #21 smoke run fixed
LISTENER_PORT = 18765
RUN = uuid.uuid4().hex[:8]
TAG = f"live-{RUN}"  # test-owned, so an operator's rbench-agent or rbench-proxy tag survives


def docker(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *argv], check=False, capture_output=True, text=True)


def gateway(network: str) -> str | None:
    out = docker("network", "inspect", network, "--format", "{{json .IPAM.Config}}").stdout
    return next((c["Gateway"] for c in json.loads(out) if c.get("Gateway")), None)


# Each cleanup is registered before its resource exists. pytest runs every finalizer even when an
# earlier one fails, so a failed `down` does not keep the image tags.
@pytest.fixture(scope="module")
def images(request):
    request.addfinalizer(lambda: docker("image", "rm", f"rbench-proxy:{TAG}"))
    proxy = build_proxy_image(tag=TAG)
    request.addfinalizer(lambda: docker("image", "rm", f"rbench-agent:{TAG}"))
    return build_agent_image(CLIS, tag=TAG), proxy


def new_project(request, tmp_path_factory, images, suffix: str, name: str | None = None):
    agent, proxy = images
    name = name or f"rbench-live-{RUN}-{suffix}"
    project = TrialProject.create(tmp_path_factory.mktemp(suffix), name, agent.ref, proxy)
    request.addfinalizer(project.down)
    return project


@pytest.fixture(scope="module")
def trial(request, tmp_path_factory, images):
    project = new_project(request, tmp_path_factory, images, "sandbox")
    listener = f"{project.name}-host-listener"
    request.addfinalizer(lambda: docker("rm", "-f", listener))
    project.up()
    # A service in the Docker host's network namespace, listening on every address.
    server = f"require('http').createServer((q, r) => r.end('listener')).listen({LISTENER_PORT})"
    started = docker(
        "run", "-d", "--rm", "--name", listener, "--network", "host",
        image_id(images[0].ref), "node", "-e", server,
    )  # fmt: skip
    assert started.returncode == 0, started.stderr
    return project, images[0]


def leftovers(name: str) -> tuple[str, str]:
    label = f"label=com.docker.compose.project={name}"
    containers = docker("ps", "--all", "--quiet", "--filter", label).stdout.strip()
    return containers, docker("network", "ls", "--quiet", "--filter", label).stdout.strip()


def run(project: TrialProject, *argv: str) -> subprocess.CompletedProcess[str]:
    result = project.exec_agent(list(argv))
    print(
        f"$ {' '.join(argv)}\nexit {result.returncode} {result.stdout.strip()} {result.stderr.strip()}"
    )
    return result


def proxy_log(project: TrialProject) -> list[str]:
    dest = project.directory / "proxy.log"
    project.copy_proxy_log(dest)
    lines = dest.read_text().splitlines()
    print("proxy log:\n" + "\n".join(lines))
    return lines


def test_trial_sandbox(trial):
    project, agent = trial
    print(f"agent image {agent.ref} {agent.clis}")
    code = ["-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20"]

    # The proxy is reachable and tunnels an allowlisted host (no credentials: TLS and a 404).
    allowed = run(project, "curl", *code, "https://api.anthropic.com/")
    assert allowed.returncode == 0 and allowed.stdout != "000"
    # github.com is refused by the proxy.
    github = run(project, "curl", *code, "https://github.com/")
    assert github.returncode == 56 and "403" in github.stderr
    # A numeric destination is refused, even the address of an allowlisted host.
    ip = docker("compose", "-p", project.name, "-f", str(project.directory / "compose.json"),
                "exec", "-T", "proxy", "getent", "ahostsv4", "api.anthropic.com").stdout.split()[0]  # fmt: skip
    numeric = run(project, "curl", *code, f"https://{ip}/")
    assert numeric.returncode == 56 and "403" in numeric.stderr
    # Plain HTTP is refused too.
    plain = run(project, "curl", *code, "http://github.com/")
    assert plain.stdout == "403"

    log = proxy_log(project)
    agent_ip = log[0].split()[1]
    assert agent_ip != "127.0.0.1" and all(line.split()[1] == agent_ip for line in log)
    assert any("TCP_TUNNEL/200 CONNECT api.anthropic.com:443" in line for line in log)
    assert any("TCP_DENIED/403 CONNECT github.com:443" in line for line in log)
    assert any(f"TCP_DENIED/403 CONNECT {ip}:443" in line for line in log)
    assert any("TCP_DENIED/403 GET http://github.com/" in line for line in log)

    # No route but the proxy: external DNS fails, and so does a direct connection to an address.
    for host in ("github.com", "host.docker.internal"):
        assert run(project, "getent", "hosts", host).returncode != 0
    direct = run(
        project, "curl", "-sS", "--noproxy", "*", "--connect-timeout", "5", f"https://{ip}/"
    )
    assert direct.returncode in (7, 28)

    # No host service on a bridge address. The proxy, on the ordinary egress bridge, reaches the
    # listener, so the agent's failure is the isolation and not a dead listener.
    egress = gateway(f"{project.name}_egress")
    assert egress is not None and gateway(f"{project.name}_internal") is None
    probe = f"exec 3<>/dev/tcp/{egress}/{LISTENER_PORT} && printf 'GET / HTTP/1.0\\r\\n\\r\\n' >&3 && cat <&3"
    control = docker("compose", "-p", project.name, "-f", str(project.directory / "compose.json"),
                     "exec", "-T", "proxy", "bash", "-c", probe)  # fmt: skip
    print(f"proxy -> {egress}:{LISTENER_PORT}: {control.stdout.splitlines()[-1:]}")
    assert control.stdout.endswith("listener")
    for address in filter(None, [egress, gateway("bridge"), "host.docker.internal"]):
        url = f"http://{address}:{LISTENER_PORT}/"
        reached = run(project, "curl", "-sS", "--noproxy", "*", "--connect-timeout", "5", url)
        assert reached.returncode != 0 and reached.stdout == ""

    # Nothing but the image in the agent: no mount, no operator home, no task data.
    agent_id = docker("compose", "-p", project.name, "-f", str(project.directory / "compose.json"),
                      "ps", "-q", "agent").stdout.strip()  # fmt: skip
    mounts = docker("inspect", "--format", "{{json .Mounts}}", agent_id).stdout.strip()
    print(f"agent mounts: {mounts}")
    assert mounts == "[]"
    assert run(project, "ls", "/home").stdout.split() == ["node"]
    assert run(project, "id", "-un").stdout.strip() == "node"


def test_a_new_attempt_never_gets_a_leftover_container(request, tmp_path_factory, images):
    earlier = new_project(request, tmp_path_factory, images, "attempt1")
    earlier.up()
    assert earlier.exec_agent(["touch", "/tmp/earlier-attempt"]).returncode == 0

    # Same name, another directory: what a retry after a crash looks like.
    retry = new_project(request, tmp_path_factory, images, "attempt2", name=earlier.name)
    retry.up()

    assert retry.exec_agent(["test", "-e", "/tmp/earlier-attempt"]).returncode == 1


def test_failed_start_leaves_nothing_behind(request, tmp_path_factory, images):
    project = new_project(request, tmp_path_factory, images, "unhealthy")
    file = project.directory / "compose.json"
    spec = json.loads(file.read_text())
    spec["services"]["proxy"]["healthcheck"] = {
        "test": ["CMD", "false"],
        "interval": "1s",
        "retries": 1,
        "start_period": "0s",
    }
    file.write_text(json.dumps(spec))

    with pytest.raises(TrialError, match=f"trial project {project.name}: cannot start") as e:
        project.up()

    print(e.value)
    assert leftovers(project.name) == ("", "")
