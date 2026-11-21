"""The hermetic configuration on a real Docker engine (design D5): docker only, no credentials, no
provider call. Both CLIs talk to a local stand-in for the model APIs (tests/fixtures/provider_stub.py)
inside the agent container, which records every request, so the test sees what a CLI would send.

Run: RBENCH_LIVE=1 uv run pytest -m live tests/test_hermetic_live.py -s
It builds both images under tags of its own and removes, by name, every project, container and
tag it created, also when a step fails.
"""

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from rbench.engines.declaration import Setting
from rbench.trial import (
    TrialProject,
    build_agent_image,
    build_proxy_image,
    collect,
    image_id,
    prepare,
    read_shim_log,
    shim_settings,
    unbound_sessions,
)

pytestmark = pytest.mark.live

CLIS = {"claude": "2.1.316", "codex": "0.171.0"}
RUN = uuid.uuid4().hex[:8]
TAG = f"live-{RUN}"
PORT = 18999
STUB = Path(__file__).parent / "fixtures" / "provider_stub.py"
# No real credential: the CLIs need a key to send a request, and the stub takes any.
CLAUDE_ENV = [
    "NO_PROXY=127.0.0.1",
    f"ANTHROPIC_BASE_URL=http://127.0.0.1:{PORT}",
    "ANTHROPIC_API_KEY=not-a-credential",
]
CODEX_ENV = ["NO_PROXY=127.0.0.1", "STUB_KEY=not-a-credential"]
# The trial's Codex config: the model is set only here (spec: Engine model settings reach the
# CLI), and the provider is the stub.
CODEX_CONFIG = f"""\
model = "gpt-config-only"
model_provider = "stub"

[model_providers.stub]
name = "stub"
base_url = "http://127.0.0.1:{PORT}/v1"
env_key = "STUB_KEY"
wire_api = "responses"
"""


def docker(*argv: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *argv], check=False, capture_output=True, text=True, input=stdin
    )


@pytest.fixture(scope="module")
def images(request):
    request.addfinalizer(lambda: docker("image", "rm", f"rbench-proxy:{TAG}"))
    proxy = build_proxy_image(tag=TAG)
    request.addfinalizer(lambda: docker("image", "rm", f"rbench-agent:{TAG}"))
    return build_agent_image(CLIS, tag=TAG), proxy


def run(project: TrialProject, *argv: str, stdin: str | None = None):
    result = project.exec_agent(list(argv), stdin=stdin)
    out = (result.stdout.strip() + " " + result.stderr.strip()).strip()
    print(f"$ {' '.join(argv)}\nexit {result.returncode} {out[-300:]}")
    return result


def requests(project: TrialProject) -> list[dict]:
    log = run(project, "cat", "/tmp/stub.jsonl").stdout
    return [json.loads(line) for line in log.splitlines()]


def start_stub(project: TrialProject) -> None:
    assert run(project, "sh", "-c", "cat > /tmp/stub.py", stdin=STUB.read_text()).returncode == 0
    file = str(project.directory / "compose.json")
    started = docker("compose", "-p", project.name, "-f", file, "exec", "-T", "--detach", "agent",
                     "python3", "/tmp/stub.py", str(PORT), "/tmp/stub.jsonl")  # fmt: skip
    assert started.returncode == 0, started.stderr
    probe = f"""\
import socket, time
for _ in range(50):
    try:
        socket.create_connection(("127.0.0.1", {PORT}))
        break
    except OSError:
        time.sleep(0.1)
"""
    assert run(project, "python3", "-c", probe).returncode == 0


@pytest.fixture(scope="module")
def trial(request, tmp_path_factory, images):
    agent, proxy = images
    directory = tmp_path_factory.mktemp("hermetic")
    project = TrialProject.create(directory, f"rbench-live-{RUN}-hermetic", agent.ref, proxy)
    request.addfinalizer(project.down)
    project.up()
    prepare(project, shim_settings({"claude": Setting("claude-sonnet-5-5", "low")}), CODEX_CONFIG)
    start_stub(project)
    return project, agent


def test_policy_holds_with_and_without_the_shims_and_every_session_is_bound(trial):
    project, agent = trial
    print(f"agent image {agent.ref} {agent.clis}")

    # The homes hold only what the trial wrote; the logs and settings are out of the engine's reach.
    homes = run(project, "find", "/home/node/.claude", "/home/node/.codex", "-mindepth", "1")
    assert homes.stdout.split() == ["/home/node/.codex/config.toml"]
    assert run(project, "sh", "-c", "echo {} >> /var/log/rbench/shim.jsonl").returncode != 0
    assert run(project, "sh", "-c", "echo {} > /etc/rbench/shim.json").returncode != 0
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in run(project, "env").stdout

    prompt = "Reply with OK."
    shim_claude = run(project, "env", *CLAUDE_ENV, "claude", "-p", prompt)
    path_claude = run(project, "env", *CLAUDE_ENV, "/usr/local/bin/claude", "-p", prompt)
    claude_requests = requests(project)
    shim_codex = run(project, "env", *CODEX_ENV, "codex", "exec", "--skip-git-repo-check", prompt)
    path_codex = run(project, "env", *CODEX_ENV, "/usr/local/bin/codex", "--search",
                     "-c", 'web_search="live"', "exec", "--skip-git-repo-check", prompt)  # fmt: skip
    version = run(project, "codex", "--version")
    for result in (shim_claude, path_claude, shim_codex, path_codex, version):
        assert result.returncode == 0
    assert "disallowed by requirements" in path_codex.stderr
    assert version.stdout.strip() == "codex-cli 0.171.0"

    sent = requests(project)
    claude_bodies = [r["body"] for r in claude_requests if r["path"].startswith("/v1/messages")]
    codex_bodies = [r["body"] for r in sent if r["path"] == "/v1/responses"]
    print(
        "Claude requests:", [(b["model"], [t["name"] for t in b["tools"]]) for b in claude_bodies]
    )
    print(
        "Codex requests:", [(b["model"], [t.get("name") for t in b["tools"]]) for b in codex_bodies]
    )
    # Web tools are gone from every request, the shim's session and the real binary's alike.
    assert len(claude_bodies) == 2 and len(codex_bodies) == 2
    for body in claude_bodies:
        assert "WebSearch" not in json.dumps(body) and "WebFetch" not in json.dumps(body)
    for body in codex_bodies:
        assert all(tool["type"] != "web_search" for tool in body["tools"])
        assert "web_search" not in json.dumps(body["tools"])
        assert body["model"] == "gpt-config-only"  # set only in the trial's config.toml
    assert claude_bodies[0]["model"] == "claude-sonnet-5-5"  # by flag, from the shim settings

    dest = project.directory / "raw"
    found = collect(project, agent, dest)
    print("transcripts:", found)
    record = json.loads((dest / "launches.json").read_text())
    print("launch record:", json.dumps(record, indent=1)[:3000])
    assert record["image"] == agent.ref and record["clis"] == CLIS
    argvs = [launch["argv"] for launch in record["launches"]]
    assert argvs == [["-p", prompt], ["exec", "--skip-git-repo-check", prompt], ["--version"]]
    assert record["launches"][1]["final_argv"][:5] == [
        "--strict-config", "-c", 'web_search="disabled"', "exec", "--ignore-rules",
    ]  # fmt: skip
    assert all(launch["exit"] == 0 for launch in record["launches"])
    claude_sessions = [s for s in record["sessions"] if s["cli"] == "claude"]
    codex_sessions = [s for s in record["sessions"] if s["cli"] == "codex"]
    assert [s["launch"] for s in claude_sessions] == [1, None]
    assert claude_sessions[0]["session_id"] == record["launches"][0]["session_id"]
    assert [s["launch"] for s in codex_sessions] == [2]
    assert codex_sessions[0]["meta"]["cli_version"] == "0.171.0"

    # Only the sessions that bypassed the shims fail to bind.
    problems = unbound_sessions(read_shim_log(dest / "shim.jsonl"), dest / "transcripts")
    print("unbound:", problems)
    bypass_claude = claude_sessions[1]["session_id"]
    assert len(problems) == 3
    assert all(bypass_claude in p for p in problems[:2])
    assert "Codex thread" in problems[2] and "no origin launch" in problems[2]
    assert codex_sessions[0]["meta"]["id"] not in problems[2]


def test_without_the_managed_files_the_same_calls_get_web_tools(images, request):
    """The control: the assertions above fail when the image's managed configuration is hidden."""
    agent, _ = images
    name = f"rbench-live-{RUN}-control"
    request.addfinalizer(lambda: docker("rm", "-f", name))
    started = docker("run", "-d", "--name", name, "--network", "none", "--init",
                     "--tmpfs", "/etc/claude-code", "--tmpfs", "/etc/codex",
                     image_id(agent.ref), "sleep", "infinity")  # fmt: skip
    assert started.returncode == 0, started.stderr

    def sh(script: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return docker("exec", "-i", name, "sh", "-c", script, stdin=stdin)

    assert sh("cat > /tmp/stub.py", STUB.read_text()).returncode == 0
    assert sh("cat > /home/node/.codex/config.toml", CODEX_CONFIG).returncode == 0
    assert (
        docker(
            "exec", "-d", name, "python3", "/tmp/stub.py", str(PORT), "/tmp/stub.jsonl"
        ).returncode
        == 0
    )
    env = " ".join(CLAUDE_ENV + CODEX_ENV)
    script = (
        f"sleep 1; export {env}; /usr/local/bin/claude -p 'Reply with OK.' </dev/null >/dev/null; "
        "/usr/local/bin/codex --search exec --skip-git-repo-check 'Reply with OK.' </dev/null >/dev/null; "
        "cat /tmp/stub.jsonl"
    )
    sent = [json.loads(line) for line in sh(script).stdout.splitlines()]
    claude = [r["body"] for r in sent if r["path"].startswith("/v1/messages")]
    codex = [r["body"] for r in sent if r["path"] == "/v1/responses"]
    print("control: Claude names WebSearch:", ["WebSearch" in json.dumps(b) for b in claude])
    print("control: Codex tool types:", [[t["type"] for t in b["tools"]] for b in codex])
    assert claude and all(
        "WebSearch" in json.dumps(b) and "WebFetch" in json.dumps(b) for b in claude
    )
    assert codex and all(any(t["type"] == "web_search" for t in b["tools"]) for b in codex)
