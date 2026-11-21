"""A trial's credentials (spec trial-isolation, Credentials, Benchmark-only Codex login; design D5)
with a fake `security` and a fake docker. No real Keychain item or login home is read."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from rbench.engines.declaration import Setting
from rbench.ledger.plan import Image
from rbench.trial import (
    CLAUDE_TOKEN_ENV,
    CODEX_LOGIN,
    TrialError,
    TrialProject,
    check_login_home,
    claude_token,
    collect,
    compose_spec,
    credentials,
    hermetic,
    prepare,
    shim_settings,
)

AGENT = "rbench-agent@sha256:" + "a" * 64
PROXY = "rbench-proxy@sha256:" + "b" * 64
# Stand-ins only: never a real credential.
TOKEN = "sk-ant-oat01-stand-in-token-0123456789"
OPERATOR_TOKEN = "sk-ant-oat01-the-operators-own-shell-token"
AUTH = {"tokens": {"access_token": "stand-in-access", "refresh_token": "stand-in-refresh"}}
SECRETS = (TOKEN, OPERATOR_TOKEN, "stand-in-access", "stand-in-refresh")


def login_home(tmp_path: Path, *extra: str, name: str = "login-home") -> Path:
    """A login home as `codex login` leaves it: `auth.json` with mode 0600, plus `extra`."""
    home = tmp_path / name
    home.mkdir()
    (home / "auth.json").write_text(json.dumps(AUTH))
    (home / "auth.json").chmod(0o600)
    for entry in extra:
        if entry.endswith("/"):
            (home / entry).mkdir()
        else:
            (home / entry).write_text("")
            (home / entry).chmod(0o600)
    return home


def chain(error: BaseException) -> str:
    """An exception's message and every cause's and context's."""
    texts = []
    while error is not None:
        texts.append(str(error))
        error = error.__cause__ or error.__context__
    return " | ".join(texts)


def test_token_comes_from_the_keychain_item(fake_bin):
    fake_bin.scenario("security", stdout=TOKEN + "\n")

    assert claude_token() == TOKEN
    (call,) = fake_bin.calls()
    assert call["argv"] == ["find-generic-password", "-s", "rbench-claude-oauth", "-w"]


@pytest.mark.parametrize(
    ("status", "hint"),
    [(44, "no such item; add it with `security add-generic-password"), (51, "may be locked")],
)
def test_keychain_failure_names_the_item_and_status_never_the_output(fake_bin, status, hint):
    # A stand-in token on the last line of both streams: neither may reach the error.
    fake_bin.scenario("security", stdout=TOKEN, stderr=f"security: failed\n{TOKEN}\n", exit=status)

    with pytest.raises(TrialError) as e:
        claude_token()
    message = str(e.value)
    assert f"'rbench-claude-oauth': security exited {status}: " in message
    assert hint in message
    assert TOKEN not in chain(e.value)


@pytest.mark.parametrize("value", ["", "\n", f"{TOKEN}\nsecond line\n", f"a {TOKEN}", "tökén"])
def test_a_keychain_value_that_is_not_one_token_is_refused_unshown(fake_bin, value):
    fake_bin.scenario("security", stdout=value)

    with pytest.raises(TrialError, match="one line of printable ASCII") as e:
        claude_token()
    assert TOKEN not in str(e.value)


def test_no_security_command_says_macos_only(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    with pytest.raises(TrialError, match="cannot run security \\(macOS only\\)"):
        claude_token()


def test_login_home_with_the_login_and_codex_files_only_passes(tmp_path):
    home = login_home(tmp_path, "auth.json.lock", ".auth-1f2e.tmp")

    assert check_login_home(home) == home.resolve()


def test_login_home_inside_the_checkout_is_refused_also_through_a_symlink(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(credentials, "CHECKOUT_ROOT", checkout)
    inside = login_home(checkout, name="creds")
    outside = tmp_path / "outside-link"
    outside.symlink_to(inside)

    for given in (inside, outside):
        with pytest.raises(TrialError, match="inside the checkout .*; keep the login outside it"):
            check_login_home(given)


def test_login_home_with_what_codex_login_left_is_refused_naming_it(tmp_path):
    # Spike #21's login home held these next to auth.json.
    home = login_home(tmp_path, "auth.lock", "log/", "tmp/")

    with pytest.raises(
        TrialError, match=r"holds auth.lock, log/, tmp/; it may hold only auth.json"
    ):
        check_login_home(home)


def test_login_home_whose_auth_json_leads_elsewhere_is_refused(tmp_path):
    home = tmp_path / "login-home"
    home.mkdir()
    (tmp_path / "elsewhere.json").write_text("{}")
    (home / "auth.json").symlink_to(tmp_path / "elsewhere.json")
    (home / ".auth-dir").mkdir()

    with pytest.raises(TrialError, match="holds .auth-dir/, auth.json;"):
        check_login_home(home)


def test_login_home_without_a_login_or_not_a_directory_is_refused(tmp_path):
    with pytest.raises(TrialError, match="not a directory"):
        check_login_home(tmp_path / "missing")
    (tmp_path / "empty").mkdir()
    with pytest.raises(TrialError, match="no auth.json; run `codex login`"):
        check_login_home(tmp_path / "empty")


def test_compose_names_the_token_and_mounts_the_login_home(tmp_path):
    agent = compose_spec(AGENT, PROXY, True, tmp_path)["services"]["agent"]

    assert agent["environment"][CLAUDE_TOKEN_ENV] is None  # a name, the value from compose's env
    assert agent["volumes"] == [
        {
            "type": "bind",
            "source": str(tmp_path),
            "target": CODEX_LOGIN,
            "bind": {"create_host_path": False},
        }
    ]
    dollar = compose_spec(AGENT, PROXY, codex_login=Path("/x/login-${HOME}$"))
    assert dollar["services"]["agent"]["volumes"][0]["source"] == "/x/login-$${HOME}$$"
    plain = compose_spec(AGENT, PROXY)["services"]["agent"]
    assert CLAUDE_TOKEN_ENV not in plain["environment"] and "volumes" not in plain
    with pytest.raises(ValueError, match="absolute path"):
        compose_spec(AGENT, PROXY, codex_login=Path("relative"))


def credentialed(tmp_path: Path, **credentials) -> TrialProject:
    return TrialProject.create(tmp_path / "run", "rbench-t1", AGENT, PROXY, **credentials)


def test_only_up_gets_the_token_and_only_this_trials(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setenv(CLAUDE_TOKEN_ENV, OPERATOR_TOKEN)
    monkeypatch.setenv("RBENCH_FAKE_ENV", CLAUDE_TOKEN_ENV)
    project = credentialed(tmp_path, claude_token=TOKEN, codex_login=login_home(tmp_path))
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "shim.jsonl").write_text("")

    project.up()
    prepare(project, shim_settings({}), "", {})
    project.exec_agent(["true"])
    project.copy_proxy_log(tmp_path / "proxy.log")
    fake_bin.scenario("docker", replies=[{"match": "ps --quiet agent", "stdout": "abc123\n"}])
    collect(project, Image(AGENT, {"codex": "0.171.0"}), raw)
    project.down()

    assert TOKEN not in repr(project)
    calls = fake_bin.calls()
    commands = {" ".join(c["argv"][5:7]) for c in calls}
    assert {"down --volumes", "up --detach", "exec -T", "cp proxy:/var/log/squid/access.log"} <= (
        commands
    )
    assert any(c["argv"][0] == "diff" for c in calls)
    for call in calls:
        expected = TOKEN if call["argv"][5:6] == ["up"] else None
        assert call["env"] == {CLAUDE_TOKEN_ENV: expected}, call["argv"]
        assert TOKEN not in json.dumps(call["argv"]) + call["stdin"]


def test_a_trial_without_the_token_never_passes_the_operators_own(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setenv(CLAUDE_TOKEN_ENV, OPERATOR_TOKEN)
    monkeypatch.setenv("RBENCH_FAKE_ENV", CLAUDE_TOKEN_ENV)

    credentialed(tmp_path).up()

    ups = [c for c in fake_bin.calls() if "up" in c["argv"]]
    assert [c["env"] for c in ups] == [{CLAUDE_TOKEN_ENV: None}]


def test_up_checks_the_login_home_right_before_the_start(fake_bin, tmp_path):
    home = login_home(tmp_path)
    project = credentialed(tmp_path, codex_login=home)
    (home / "log").mkdir()  # after the trial was created

    with pytest.raises(TrialError, match="holds log/"):
        project.up()
    assert [c["argv"][5] for c in fake_bin.calls()] == ["down"]


def test_up_refuses_a_login_home_that_resolves_elsewhere_since_create(fake_bin, tmp_path):
    first = login_home(tmp_path, name="first")
    link = tmp_path / "link"
    link.symlink_to(first)
    project = credentialed(tmp_path, codex_login=link)
    assert project.codex_login == first.resolve()  # the compose file mounts the resolved path
    first.rename(tmp_path / "first-old")
    first.symlink_to(login_home(tmp_path, name="second"))

    with pytest.raises(TrialError, match="now resolves elsewhere"):
        project.up()
    assert [c["argv"][5] for c in fake_bin.calls()] == ["down"]


def test_prepare_probes_the_login_home_as_the_trials_user_then_links_it(fake_bin, tmp_path):
    project = credentialed(tmp_path, codex_login=login_home(tmp_path))

    prepare(project, shim_settings({}))

    argvs = [c["argv"][c["argv"].index("-T") + 1 :] for c in fake_bin.calls()]
    probe = ["agent", "sh", "-c", hermetic.LOGIN_PROBE]
    link = ["agent", "ln", "-s", "/rbench/codex-login/auth.json", "/home/node/.codex/auth.json"]
    assert argvs.index(probe) + 1 == argvs.index(link)


def test_a_login_home_the_trials_user_cannot_write_stops_prepare(fake_bin, tmp_path):
    project = credentialed(tmp_path, codex_login=login_home(tmp_path))
    fake_bin.scenario(
        "docker",
        replies=[{"match": "auth.json.lock", "stderr": "cannot create files\n", "exit": 1}],
    )

    with pytest.raises(TrialError, match="can use the Codex login home .*: cannot create files"):
        prepare(project, shim_settings({}))
    assert not any("ln" in c["argv"] for c in fake_bin.calls())


def test_docker_diagnostics_never_carry_the_token(fake_bin, tmp_path):
    """The token on the last stderr line of a failed start, a failed exec in prepare and an exec
    the caller judges: redacted everywhere, causes included."""
    project = credentialed(tmp_path, claude_token=TOKEN)
    leak = {"stderr": f"error: env {CLAUDE_TOKEN_ENV}={TOKEN}\n", "exit": 1}

    fake_bin.scenario("docker", replies=[{"match": "up --detach", **leak}])
    with pytest.raises(TrialError) as start:
        project.up()
    fake_bin.scenario("docker", replies=[{"match": "shim.json", **leak}])
    with pytest.raises(TrialError) as setup:
        prepare(project, shim_settings({}))
    fake_bin.scenario("docker", stdout=f"{TOKEN}\n", **leak)
    result = project.exec_agent(["env"])

    for error in (start.value, setup.value):
        assert "<redacted>" in str(error)
        assert TOKEN not in chain(error)
    assert TOKEN not in result.stdout + result.stderr
    assert "<redacted>" in result.stdout


def test_a_trials_raw_output_holds_no_credential_value(fake_bin, tmp_path, monkeypatch):
    """Every file the trial leaves in its run directory, every docker argv and stdin, and the error
    of a failed start: none holds the token or the login's tokens."""
    monkeypatch.setenv(CLAUDE_TOKEN_ENV, OPERATOR_TOKEN)
    fake_bin.scenario("security", stdout=TOKEN + "\n")
    home = login_home(tmp_path, "auth.json.lock")
    project = credentialed(tmp_path, claude_token=claude_token(), codex_login=home)
    project.up()
    prepare(project, shim_settings({"claude": Setting("claude-opus-5-5", "high")}), "", {})
    raw = project.directory / "raw"
    raw.mkdir()
    (raw / "shim.jsonl").write_text("")  # as `docker compose cp` of an empty shim log leaves it
    fake_bin.scenario("docker", replies=[{"match": "ps --quiet agent", "stdout": "abc123\n"}])
    collect(project, Image(AGENT, {"claude": "2.1.316", "codex": "0.171.0"}), raw)
    fail = {"match": "up --detach", "stderr": f"boom: {TOKEN}\n", "exit": 1}
    fake_bin.scenario("docker", replies=[fail])
    with pytest.raises(TrialError) as failed:
        project.up()

    files = sorted(p for p in project.directory.rglob("*") if p.is_file())
    assert [p.relative_to(project.directory).as_posix() for p in files] == [
        "compose.json",
        "raw/launches.json",
        "raw/shim.jsonl",
    ]
    texts = [p.read_text() for p in files] + [chain(failed.value), repr(project)]
    texts += [json.dumps(c["argv"]) + c["stdin"] for c in fake_bin.calls()]
    for secret in SECRETS:
        assert not [t for t in texts if secret in t], secret
    assert os.environ[CLAUDE_TOKEN_ENV] == OPERATOR_TOKEN  # the run never touched its own env


@pytest.mark.live
def test_compose_mounts_exactly_the_checked_directory(tmp_path, monkeypatch):
    """Docker only, no daemon state: Compose resolves the bind source to the directory rbench
    checked, also when its name holds `$` and the variable it reads like is set."""
    assert shutil.which("docker"), "docker CLI needed"
    checked = login_home(tmp_path, name="login-${RBENCH_REVIEW_SUFFIX}")
    login_home(tmp_path, "log/", name="login-different")
    monkeypatch.setenv("RBENCH_REVIEW_SUFFIX", "different")
    project = credentialed(tmp_path, codex_login=checked)
    assert check_login_home(checked) == project.codex_login

    file = str(project.directory / "compose.json")
    config = subprocess.run(
        ["docker", "compose", "--project-name", project.name, "--file", file]
        + ["config", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )

    (mount,) = json.loads(config.stdout)["services"]["agent"]["volumes"]
    print("resolved mount:", mount)
    # `config` prints a compose file again, so a literal `$` comes back doubled.
    assert mount["source"].replace("$$", "$") == str(project.codex_login)
    assert mount["target"] == CODEX_LOGIN
