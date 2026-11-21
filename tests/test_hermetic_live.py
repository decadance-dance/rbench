"""The hermetic configuration on a real Docker engine (design D5): docker only, no credentials, no
provider call. Both CLIs talk to a local stand-in for the model APIs (tests/fixtures/provider_stub.py)
inside the agent container, which records every request, so the test sees what a CLI would send.

Run: RBENCH_LIVE=1 uv run pytest -m live tests/test_hermetic_live.py -s
It builds both images under tags of its own and removes, by name, every project, container and
tag it created, also when a step fails.
"""

import json
import re
import subprocess
import uuid
from pathlib import Path

import pytest

from rbench.engines.declaration import Setting
from rbench.trial import (
    CLAUDE_TOKEN_ENV,
    CODEX_LOGIN,
    TrialError,
    TrialProject,
    build_agent_image,
    build_proxy_image,
    check_login_home,
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


def test_codex_prompt_stays_literal_and_a_resume_supplies_no_origin(trial):
    """The round 1 review's repros: a prompt that reads like the sandbox option, and a shim
    resume of a session the real binary started."""
    project, agent = trial
    literal = run(project, "env", *CODEX_ENV, "codex", "exec", "--skip-git-repo-check", "--",
                  "--sandbox=read-only")  # fmt: skip
    assert literal.returncode == 0
    body = [r["body"] for r in requests(project) if r["path"] == "/v1/responses"][-1]
    said = [
        part.get("text")
        for item in body["input"]
        if item.get("role") == "user"
        for part in item.get("content", [])
    ]
    print("user messages:", said)
    assert "--sandbox=read-only" in said and "--sandbox=danger-full-access" not in said

    prompt = "Reply with OK."
    by_path = run(project, "env", *CODEX_ENV, "/usr/local/bin/codex", "exec",
                  "--skip-git-repo-check", prompt)  # fmt: skip
    thread = re.search(r"session id: (\S+)", by_path.stderr).group(1)
    resumed = run(project, "env", *CODEX_ENV, "codex", "exec", "--skip-git-repo-check",
                  "resume", thread, prompt)  # fmt: skip
    assert by_path.returncode == resumed.returncode == 0

    dest = project.directory / "raw-resume"
    collect(project, agent, dest)
    problems = unbound_sessions(read_shim_log(dest / "shim.jsonl"), dest / "transcripts")
    print("unbound:", problems)
    (problem,) = [p for p in problems if thread in p]
    assert "no origin launch, only resumes" in problem


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


# Credentials (spec trial-isolation, Credentials, Benchmark-only Codex login; issue #40). Stand-ins
# only: no Keychain read, no real login home, and the CLIs talk to the stub.
TOKEN = f"sk-ant-oat01-stand-in-{RUN}"
AUTH = {"tokens": {"access_token": f"access-{RUN}", "refresh_token": f"refresh-{RUN}"}}
REFRESHED = {"tokens": {"access_token": f"new-access-{RUN}", "refresh_token": f"new-refresh-{RUN}"}}
# What Codex 0.171.0 does on a refresh (#20): lock `auth.json.lock` next to the file `auth.json`
# resolves to, write a temporary file there and rename it onto `auth.json`.
FAKE_REFRESH = """\
import fcntl, os, sys, tempfile
target = os.path.realpath(os.path.expanduser("~/.codex/auth.json"))
home = os.path.dirname(target)
with open(os.path.join(home, "auth.json.lock"), "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    fd, tmp = tempfile.mkstemp(prefix=".auth-", dir=home)
    with os.fdopen(fd, "w") as f:
        f.write(sys.stdin.read())
    os.replace(tmp, target)
print(target)
"""


@pytest.fixture(scope="module")
def credentialed(request, tmp_path_factory, images):
    agent, proxy = images
    login = tmp_path_factory.mktemp("login-home")
    (login / "auth.json").write_text(json.dumps(AUTH))
    (login / "auth.json.lock").write_text("")
    for name in ("auth.json", "auth.json.lock"):  # as `codex login` leaves them
        (login / name).chmod(0o600)
    directory = tmp_path_factory.mktemp("credentialed")
    name = f"rbench-live-{RUN}-credentials"
    project = TrialProject.create(
        directory, name, agent.ref, proxy, claude_token=TOKEN, codex_login=login
    )
    request.addfinalizer(project.down)
    project.up()
    prepare(project, shim_settings({}), CODEX_CONFIG)
    start_stub(project)
    return project, agent, login


def test_credentials_reach_the_trial_and_not_its_output(credentialed):
    project, agent, login = credentialed

    # The token is in the container's environment; the value goes in by stdin, not argv.
    same = run(project, "sh", "-c", f'test "${CLAUDE_TOKEN_ENV}" = "$(cat)"', stdin=TOKEN)
    assert same.returncode == 0
    link = run(project, "readlink", "/home/node/.codex/auth.json").stdout.strip()
    assert link == f"{CODEX_LOGIN}/auth.json"

    env = ["NO_PROXY=127.0.0.1", f"ANTHROPIC_BASE_URL=http://127.0.0.1:{PORT}"]
    claude = run(project, "env", *env, "claude", "-p", "Reply with OK.")
    codex = run(project, "env", *CODEX_ENV, "codex", "exec", "--skip-git-repo-check", "OK?")
    assert claude.returncode == codex.returncode == 0
    paths = [r["path"] for r in requests(project)]
    print("stub requests:", paths)
    assert any(p.startswith("/v1/messages") for p in paths) and "/v1/responses" in paths

    raw = project.directory / "raw"
    found = collect(project, agent, raw)
    project.copy_proxy_log(project.directory / "proxy.log")
    assert found  # both CLIs wrote transcripts
    files = [p for p in project.directory.rglob("*") if p.is_file()]
    print("scanned:", sorted(str(p.relative_to(project.directory)) for p in files))
    history = docker("history", "--no-trunc", image_id(agent.ref)).stdout
    config = docker("image", "inspect", image_id(agent.ref)).stdout
    texts = [p.read_text(errors="replace") for p in files] + [history, config]
    for secret in (TOKEN, *AUTH["tokens"].values()):
        assert not [t for t in texts if secret in t]
    check_login_home(login)
    assert json.loads((login / "auth.json").read_text()) == AUTH


def test_a_refresh_writes_back_only_to_the_login_home(credentialed, tmp_path):
    project, _, login = credentialed
    operator = tmp_path / "operator-codex"  # the operator's own Codex login: never mounted
    operator.mkdir()
    (operator / "auth.json").write_text(json.dumps(AUTH))

    refreshed = run(project, "python3", "-c", FAKE_REFRESH, stdin=json.dumps(REFRESHED))

    assert refreshed.returncode == 0
    assert refreshed.stdout.strip() == f"{CODEX_LOGIN}/auth.json"
    assert json.loads((login / "auth.json").read_text()) == REFRESHED
    assert sorted(p.name for p in login.iterdir()) == ["auth.json", "auth.json.lock"]
    assert (login / "auth.json").stat().st_mode & 0o777 == 0o600
    check_login_home(login)
    assert json.loads((operator / "auth.json").read_text()) == AUTH
    # The trial's home still links to the login: no copy of the login lands in the container.
    link = run(project, "readlink", "/home/node/.codex/auth.json").stdout.strip()
    assert link == f"{CODEX_LOGIN}/auth.json"
    copies = run(project, "find", "/home/node", "-name", "*auth*", "-type", "f").stdout.split()
    assert copies == []


@pytest.mark.parametrize(
    ("home_mode", "auth_mode", "reason"),
    [(0o500, 0o600, "cannot create files"), (0o700, 0o444, "cannot write auth.json")],
)
def test_a_login_home_the_trials_user_cannot_write_is_refused(
    request, tmp_path, images, home_mode, auth_mode, reason
):
    """The host-side check passes, but Codex could not save a refresh there: a read-only home
    (no temporary file) or a read-only `auth.json` (Codex opens it for writing first)."""
    agent, proxy = images
    login = tmp_path / "login-home"
    login.mkdir()
    (login / "auth.json").write_text(json.dumps(AUTH))
    (login / "auth.json").chmod(auth_mode)
    login.chmod(home_mode)
    request.addfinalizer(lambda: login.chmod(0o700))
    check_login_home(login)
    name = f"rbench-live-{RUN}-unwritable-login-{oct(home_mode)[2:]}-{oct(auth_mode)[2:]}"
    project = TrialProject.create(tmp_path / "trial", name, agent.ref, proxy, codex_login=login)
    request.addfinalizer(project.down)
    project.up()

    with pytest.raises(TrialError, match=f"can use the Codex login home .*: {reason}"):
        prepare(project, shim_settings({}), CODEX_CONFIG)
    link = run(project, "test", "-e", "/home/node/.codex/auth.json")
    assert link.returncode != 0
    assert json.loads((login / "auth.json").read_text()) == AUTH  # the probe changed nothing
