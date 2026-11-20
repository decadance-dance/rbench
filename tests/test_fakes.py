import signal
import subprocess
import time

import pytest


def run(*argv: str, stdin: str = "", timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, check=False, input=stdin, capture_output=True, text=True, timeout=timeout
    )


@pytest.mark.parametrize("tool", ["claude", "codex", "docker", "gh"])
def test_records_argv_and_stdin(fake_bin, tool):
    result = run(tool, "-p", "two words", stdin="the prompt\n")

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert fake_bin.calls() == [
        {"tool": tool, "argv": ["-p", "two words"], "stdin": "the prompt\n"}
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
