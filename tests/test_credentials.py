"""A trial's credentials (spec trial-isolation, Credentials, Benchmark-only Codex login; design D5)
with a fake `security` and a fake docker. No real Keychain item or login home is read."""

import json
import os
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


def login_home(tmp_path: Path, *extra: str) -> Path:
    home = tmp_path / "login-home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps(AUTH))
    for name in extra:
        if name.endswith("/"):
            (home / name).mkdir()
        else:
            (home / name).write_text("")
    return home


def test_token_comes_from_the_keychain_item(fake_bin):
    fake_bin.scenario("security", stdout=TOKEN + "\n")

    assert claude_token() == TOKEN
    (call,) = fake_bin.calls()
    assert call["argv"] == ["find-generic-password", "-s", "rbench-claude-oauth", "-w"]


def test_missing_keychain_item_is_named_and_no_value_is_shown(fake_bin):
    stderr = "security: SecKeychainSearchCopyNext: The specified item could not be found.\n"
    fake_bin.scenario("security", stdout=TOKEN, stderr=stderr, exit=44)

    with pytest.raises(
        TrialError, match="'rbench-claude-oauth': security exited 44: .*not be"
    ) as e:
        claude_token()
    assert TOKEN not in str(e.value)


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

    assert check_login_home(home) == home.absolute()


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
    assert agent["volumes"] == [{"type": "bind", "source": str(tmp_path), "target": CODEX_LOGIN}]
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

    project.up()

    assert TOKEN not in repr(project)
    ups = [c for c in fake_bin.calls() if "up" in c["argv"]]
    assert [c["env"] for c in ups] == [{CLAUDE_TOKEN_ENV: TOKEN}]
    for call in fake_bin.calls():
        assert TOKEN not in json.dumps(call["argv"]) + call["stdin"]


def test_a_trial_without_the_token_never_passes_the_operators_own(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setenv(CLAUDE_TOKEN_ENV, OPERATOR_TOKEN)
    monkeypatch.setenv("RBENCH_FAKE_ENV", CLAUDE_TOKEN_ENV)

    credentialed(tmp_path).up()

    ups = [c for c in fake_bin.calls() if "up" in c["argv"]]
    assert [c["env"] for c in ups] == [{CLAUDE_TOKEN_ENV: None}]


def test_up_refuses_an_unclean_login_home_before_docker(fake_bin, tmp_path):
    project = credentialed(tmp_path, codex_login=login_home(tmp_path, "log/"))

    with pytest.raises(TrialError, match="holds log/"):
        project.up()
    assert fake_bin.calls() == []


def test_prepare_links_auth_json_into_the_login_home(fake_bin, tmp_path):
    project = credentialed(tmp_path, codex_login=login_home(tmp_path))

    prepare(project, shim_settings({}))

    link = ["ln", "-s", "/rbench/codex-login/auth.json", "/home/node/.codex/auth.json"]
    assert [c["argv"][-4:] for c in fake_bin.calls() if "ln" in c["argv"]] == [link]


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
    fake_bin.scenario("docker", replies=[{"match": "up --detach", "stderr": "boom\n", "exit": 1}])
    with pytest.raises(TrialError) as failed:
        project.up()

    files = sorted(p for p in project.directory.rglob("*") if p.is_file())
    assert [p.relative_to(project.directory).as_posix() for p in files] == [
        "compose.json",
        "raw/launches.json",
        "raw/shim.jsonl",
    ]
    texts = [p.read_text() for p in files] + [str(failed.value), repr(project)]
    texts += [json.dumps(c["argv"]) + c["stdin"] for c in fake_bin.calls()]
    for secret in SECRETS:
        assert not [t for t in texts if secret in t], secret
    assert os.environ[CLAUDE_TOKEN_ENV] == OPERATOR_TOKEN  # the run never touched its own env
