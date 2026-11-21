import os
import signal
import subprocess
import time

import pytest
from conftest import FAKE_BIN, require_fakes


def run(*argv: str, stdin: str = "", timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, check=False, input=stdin, capture_output=True, text=True, timeout=timeout
    )


@pytest.mark.parametrize("tool", ["claude", "codex", "docker", "gh", "security"])
def test_records_argv_and_stdin(fake_bin, tool):
    result = run(tool, "-p", "two words", stdin="the prompt\n")

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert fake_bin.calls() == [
        {"tool": tool, "argv": ["-p", "two words"], "stdin": "the prompt\n", "cwd": os.getcwd()}
    ]


def test_canned_output_per_tool(fake_bin):
    fake_bin.scenario("codex", stdout='{"type": "done"}\n', exit=3)

    codex = run("codex", "exec")
    claude = run("claude")

    assert (codex.returncode, codex.stdout) == (3, '{"type": "done"}\n')
    assert (claude.returncode, claude.stdout) == (0, "")


def test_rate_limit_message(fake_bin):
    message = "Claude usage limit reached. Your limit will reset at 5pm.\n"
    fake_bin.scenario("claude", stderr=message, exit=1)

    result = run("claude", "-p")

    assert (result.returncode, result.stderr) == (1, message)


def test_delay(fake_bin):
    fake_bin.scenario("docker", delay=2)

    with pytest.raises(subprocess.TimeoutExpired):
        run("docker", "run", timeout=0.5)
    start = time.monotonic()
    fake_bin.scenario("docker", delay=0.3)
    run("docker", "run")
    assert time.monotonic() - start >= 0.3


def test_crash(fake_bin):
    fake_bin.scenario("gh", stdout="partial", crash=True)

    result = run("gh", "pr", "view")

    assert (result.returncode, result.stdout) == (-signal.SIGKILL, "partial")
    assert fake_bin.calls()[-1]["argv"] == ["pr", "view"]


@pytest.mark.parametrize("broken", ["missing", "not executable", "plain copy"])
def test_fixture_fails_closed_on_a_broken_shim(tmp_path, broken):
    real = tmp_path / "real"
    real.mkdir()
    (real / "gh").write_text("#!/bin/sh\nexit 91\n")
    (real / "gh").chmod(0o755)
    shims = tmp_path / "shims"
    shims.mkdir()
    for tool in ["fake", "claude", "codex", "docker"]:
        (shims / tool).symlink_to(FAKE_BIN / tool)
    if broken == "not executable":
        (shims / "gh").write_text("fake")  # what git writes for a symlink with symlinks off
    elif broken == "plain copy":
        (shims / "gh").write_text("#!/bin/sh\n")
        (shims / "gh").chmod(0o755)

    with pytest.raises(pytest.fail.Exception, match="fake gh does not resolve"):
        require_fakes(shims, f"{shims}{os.pathsep}{real}")


def test_fixture_accepts_the_shims():
    require_fakes(FAKE_BIN, str(FAKE_BIN))
