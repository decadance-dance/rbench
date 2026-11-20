import subprocess

import pytest

from rbench.cli import ExitCode, main

PLANNED = ["tasks", "plan", "run", "resume", "report", "publish"]


def test_help_lists_planned_commands_and_exit_codes():
    result = subprocess.run(["rbench", "--help"], capture_output=True, text=True, check=True)

    for command in PLANNED:
        assert f"\n    {command} " in result.stdout
    assert "exit codes:" in result.stdout
    assert "\n  75  stopped at" in result.stdout


def test_tasks_help_lists_import_and_fetch(capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["tasks", "--help"])

    assert exit_.value.code == 0
    out = capsys.readouterr().out
    assert "\n    import " in out and "\n    fetch " in out


@pytest.mark.parametrize("argv", [[], ["tasks"], ["run", "--bogus"]])
def test_bad_command_line_exits_2(argv):
    with pytest.raises(SystemExit) as exit_:
        main(argv)

    assert exit_.value.code == ExitCode.USAGE == 2


@pytest.mark.parametrize("argv", [["run", "--wait"], ["tasks", "fetch"], ["publish"]])
def test_stubs_fail_as_not_implemented(argv, capsys):
    assert main(argv) == ExitCode.ERROR
    assert "not implemented yet" in capsys.readouterr().err
