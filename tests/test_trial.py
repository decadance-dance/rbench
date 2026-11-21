import json
import re
from pathlib import Path

import pytest

from rbench.engines.pins import FloatingRefError
from rbench.ledger.plan import Image, check_image
from rbench.trial import (
    PROXY_LOG,
    PROXY_URL,
    TrialError,
    TrialProject,
    build_agent_image,
    build_proxy_image,
    compose_spec,
    image_id,
)

ROOT = Path(__file__).resolve().parents[1]
AGENT_ID = "sha256:" + "a" * 64
PROXY_ID = "sha256:" + "b" * 64
AGENT = f"rbench-agent@{AGENT_ID}"
PROXY = f"rbench-proxy@{PROXY_ID}"
CLIS = {"claude": "2.1.316", "codex": "0.171.0"}
DOWN = ["down", "--volumes", "--remove-orphans", "--timeout", "5"]


def docker_answers(fake_bin, **extra):
    replies = [
        {"match": "build ", "stdout": AGENT_ID + "\n"},
        {"match": "claude --version", "stdout": "2.1.316 (Claude Code)\n"},
        {"match": "codex --version", "stdout": "codex-cli 0.171.0\n"},
    ]
    fake_bin.scenario("docker", replies=replies, **extra)


def test_agent_image_pins_versions_and_reads_them_back(fake_bin):
    docker_answers(fake_bin)

    image = build_agent_image(CLIS, images_dir=Path("/images"))

    assert image == Image(AGENT, CLIS)
    check_image("built", image)
    build, *runs = [call["argv"] for call in fake_bin.calls()]
    assert build == [
        "build",
        "--quiet",
        "--tag",
        "rbench-agent:claude-2.1.316-codex-0.171.0",
        "--build-arg",
        "CLAUDE_VERSION=2.1.316",
        "--build-arg",
        "CODEX_VERSION=0.171.0",
        "/images/agent",
    ]
    assert runs == [
        ["run", "--rm", "--network", "none", AGENT_ID, "/usr/local/bin/claude", "--version"],
        ["run", "--rm", "--network", "none", AGENT_ID, "/usr/local/bin/codex", "--version"],
    ]


def test_agent_image_installs_only_declared_clis(fake_bin):
    docker_answers(fake_bin)

    assert build_agent_image({"claude": "2.1.316"}).clis == {"claude": "2.1.316"}

    build = fake_bin.calls()[0]["argv"]
    assert "CODEX_VERSION=" in build and "CLAUDE_VERSION=2.1.316" in build
    assert [c["argv"][-2] for c in fake_bin.calls()[1:]] == ["/usr/local/bin/claude"]


def test_agent_image_rejects_another_version_inside(fake_bin):
    fake_bin.scenario(
        "docker",
        replies=[
            {"match": "build ", "stdout": AGENT_ID + "\n"},
            {"match": "claude --version", "stdout": "2.1.317 (Claude Code)\n"},
        ],
    )

    with pytest.raises(TrialError, match=r"expected claude 2\.1\.316.*2\.1\.317"):
        build_agent_image({"claude": "2.1.316"})


@pytest.mark.parametrize(
    "banner", ["2.1.316 (Claude Code)\n2.1.317 (Claude Code)\n", "v2.1.316 (Claude Code)\n", ""]
)
def test_agent_image_needs_exactly_the_version_banner(fake_bin, banner):
    fake_bin.scenario(
        "docker",
        replies=[
            {"match": "build ", "stdout": AGENT_ID + "\n"},
            {"match": "claude --version", "stdout": banner},
        ],
    )

    with pytest.raises(TrialError, match="expected claude 2.1.316"):
        build_agent_image({"claude": "2.1.316"})


@pytest.mark.parametrize(
    "clis, error",
    [
        ({}, TrialError),
        ({"gemini": "1.0.0"}, TrialError),
        ({"claude": "latest"}, FloatingRefError),
        ({"claude": "2.1.316; rm -rf /"}, FloatingRefError),
    ],
)
def test_agent_image_checks_versions_before_docker(fake_bin, clis, error):
    with pytest.raises(error):
        build_agent_image(clis)
    assert fake_bin.calls() == []


def test_build_failure_names_the_image(fake_bin):
    fake_bin.scenario("docker", stderr="step 1\nERROR: npm 404\n", exit=1)

    with pytest.raises(TrialError, match=r"building rbench-proxy:latest.*exited 1: ERROR: npm 404"):
        build_proxy_image()


def test_build_without_an_image_id_fails(fake_bin):
    fake_bin.scenario("docker", stdout="Successfully built\n")

    with pytest.raises(TrialError, match="printed no image id"):
        build_proxy_image()


def test_proxy_image_reference(fake_bin):
    fake_bin.scenario("docker", stdout=PROXY_ID + "\n")

    assert build_proxy_image(images_dir=Path("/images")) == PROXY
    assert fake_bin.calls()[0]["argv"] == [
        "build",
        "--quiet",
        "--tag",
        "rbench-proxy:latest",
        "/images/proxy",
    ]


@pytest.mark.parametrize("ref", ["rbench-agent:latest", "rbench-agent@sha256:abc", "sha256:x"])
def test_image_id_needs_a_digest(ref):
    with pytest.raises(TrialError, match="not referenced by digest"):
        image_id(ref)


def test_agent_reaches_out_only_through_the_proxy():
    spec = compose_spec(AGENT, PROXY)
    agent, proxy = spec["services"]["agent"], spec["services"]["proxy"]

    assert agent["networks"] == ["internal"]
    assert proxy["networks"] == ["internal", "egress"]
    assert spec["networks"]["internal"] == {
        "driver": "bridge",
        "internal": True,
        "driver_opts": {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"},
    }
    assert "internal" not in spec["networks"]["egress"]
    env = agent["environment"]
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        assert env[name] == PROXY_URL
    assert env["NO_PROXY"] == env["no_proxy"] == ""


def test_agent_holds_nothing_but_its_image():
    spec = compose_spec(AGENT, PROXY)

    for service in spec["services"].values():
        assert not {"volumes", "env_file", "secrets", "configs", "ports"} & service.keys()
        assert service["pull_policy"] == "never"
        assert "build" not in service
    assert spec["services"]["agent"]["image"] == AGENT_ID
    assert spec["services"]["proxy"]["image"] == PROXY_ID
    assert "volumes" not in spec


def test_project_file_and_commands(fake_bin, tmp_path):
    project = TrialProject.create(tmp_path / "trial", "rbench-t1", AGENT, PROXY)
    file = str(tmp_path / "trial" / "compose.json")

    assert json.loads(Path(file).read_text()) == compose_spec(AGENT, PROXY)
    project.up()
    fake_bin.scenario("docker", stdout="out", exit=7)
    result = project.exec_agent(["curl", "https://github.com"], stdin="x")
    fake_bin.scenario("docker")
    project.copy_proxy_log(tmp_path / "proxy.log")
    project.copy_to_agent(tmp_path / "stage", "/rbench")
    project.copy_from_agent("/rbench/output", tmp_path / "out")
    project.down()

    assert (result.returncode, result.stdout) == (7, "out")
    base = ["compose", "--project-name", "rbench-t1", "--file", file]
    assert [(c["argv"], c["stdin"]) for c in fake_bin.calls()] == [
        (base + DOWN, ""),
        (base + ["up", "--detach", "--wait"], ""),
        (base + ["exec", "-T", "agent", "curl", "https://github.com"], "x"),
        (base + ["cp", f"proxy:{PROXY_LOG}", str(tmp_path / "proxy.log")], ""),
        (base + ["cp", "--archive", str(tmp_path / "stage"), "agent:/rbench"], ""),
        (base + ["cp", "agent:/rbench/output", str(tmp_path / "out")], ""),
        (base + DOWN, ""),
    ]


def test_start_removes_a_leftover_project_of_that_name_first(fake_bin, tmp_path):
    # Compose identifies a project by its name: without the removal, `up` would adopt the
    # containers an earlier attempt left behind, with whatever it wrote into them.
    project = TrialProject.create(tmp_path, "rbench-t1", AGENT, PROXY)

    project.up()

    assert [c["argv"][5:] for c in fake_bin.calls()] == [DOWN, ["up", "--detach", "--wait"]]


UNHEALTHY = {
    "match": "up --detach",
    "stderr": "container rbench-t1-proxy-1 is unhealthy\n",
    "exit": 1,
}


def test_failed_start_removes_what_it_created(fake_bin, tmp_path):
    project = TrialProject.create(tmp_path, "rbench-t1", AGENT, PROXY)
    fake_bin.scenario("docker", replies=[UNHEALTHY])

    with pytest.raises(TrialError, match="trial project rbench-t1: cannot start.*unhealthy") as e:
        project.up()

    assert "removed" in str(e.value)
    assert [c["argv"][5:] for c in fake_bin.calls()] == [DOWN, ["up", "--detach", "--wait"], DOWN]


def test_failed_cleanup_after_a_failed_start_is_reported_too(fake_bin, tmp_path, monkeypatch):
    project = TrialProject.create(tmp_path, "rbench-t1", AGENT, PROXY)
    fake_bin.scenario("docker", replies=[UNHEALTHY])
    downs = []

    def down(self):
        downs.append(self.name)
        if len(downs) == 2:
            raise TrialError("removing trial project rbench-t1: docker compose exited 1: busy")

    monkeypatch.setattr(TrialProject, "down", down)

    with pytest.raises(TrialError, match="unhealthy.*may remain.*busy"):
        project.up()
    assert downs == ["rbench-t1", "rbench-t1"]


def test_leftover_that_cannot_be_removed_stops_the_start(fake_bin, tmp_path):
    project = TrialProject.create(tmp_path, "rbench-t1", AGENT, PROXY)
    fake_bin.scenario("docker", replies=[{"match": "down", "stderr": "busy\n", "exit": 1}])

    with pytest.raises(TrialError, match="removing trial project rbench-t1.*busy"):
        project.up()
    assert [c["argv"][5] for c in fake_bin.calls()] == ["down"]


def test_agent_command_without_docker_names_the_trial(tmp_path, monkeypatch):
    project = TrialProject.create(tmp_path, "rbench-t1", AGENT, PROXY)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    with pytest.raises(TrialError, match="trial project rbench-t1: cannot run docker"):
        project.exec_agent(["true"])


@pytest.mark.parametrize("name", ["", "Rbench", "-x", "a b", "a/b"])
def test_project_name_is_checked(tmp_path, name):
    with pytest.raises(ValueError, match="compose project name"):
        TrialProject.create(tmp_path, name, AGENT, PROXY)


def test_squid_allows_only_listed_names_on_443():
    conf = (ROOT / "images/proxy/squid.conf").read_text()
    rules = [line for line in conf.splitlines() if line and not line.startswith("#")]

    assert 'acl allowed dstdomain -n "/etc/squid/allowlist"' in rules
    access = [line for line in rules if line.startswith("http_access")]
    assert access == ["http_access allow CONNECT SSL_ports allowed", "http_access deny all"]
    assert "acl SSL_ports port 443" in rules
    assert "cache deny all" in rules and not any(r.startswith("cache allow") for r in rules)
    assert any(re.fullmatch(r"access_log stdio:\S+ rbench !self", line) for line in rules)


def test_proxy_image_carries_the_merged_allowlist():
    dockerfile = (ROOT / "images/proxy/Dockerfile").read_text()

    assert "COPY allowlist /etc/squid/allowlist" in dockerfile
    hosts = [
        line
        for line in (ROOT / "images/proxy/allowlist").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert hosts == ["api.anthropic.com", "chatgpt.com", "auth.openai.com"]


@pytest.mark.parametrize("image", ["agent", "proxy"])
def test_base_images_are_pinned_by_digest(image):
    dockerfile = (ROOT / "images" / image / "Dockerfile").read_text()

    froms = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(froms) == 1 and re.fullmatch(r"FROM \S+@sha256:[0-9a-f]{64}", froms[0])


def test_agent_build_context_holds_only_the_image_and_its_policy():
    context = ROOT / "images/agent"
    files = sorted(
        p.relative_to(context).as_posix()
        for p in context.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts  # excluded by .dockerignore
    )
    assert files == [
        ".dockerignore",
        "Dockerfile",
        "gh",
        "lib/collector.py",
        "lib/session_hook.py",
        "lib/shim.py",
        "managed-settings.json",
        "requirements.toml",
    ]
    copies = re.findall(r"^COPY .*? (\S+) \S+$", (context / "Dockerfile").read_text(), re.MULTILINE)
    assert copies == ["gh", "managed-settings.json", "requirements.toml", "lib/"]
