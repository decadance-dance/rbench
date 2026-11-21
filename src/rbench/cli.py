"""The `rbench` command line.

The command list and exit codes are provisional until each command's issue fixes its behavior.
"""

import argparse
import sys
from enum import IntEnum
from importlib.metadata import version

from rbench.tasks import reviewbench


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2  # argparse exits with 2 on bad arguments
    STOPPED = 75  # stopped at a round budget, soft threshold or limit (spec run-ledger)


EXIT_CODES_HELP = """exit codes:
  0   success
  1   error
  2   bad command line
  75  stopped at a round budget, a soft threshold or a limit; prints the resume command
"""

# (command, help); `tasks` has its own subcommands.
COMMANDS = [
    ("plan", "print the trial count and token estimate of a matrix round; runs nothing"),
    ("run", "run one round of a matrix"),
    ("resume", "resume a stopped or crashed run"),
    ("report", "write the HTML report and results file of a run"),
    ("publish", "publish a run's report and results without hidden-task details"),
]
TASK_COMMANDS = [
    ("import", "import tasks into a task set"),
    ("fetch", "download task source archives and check them against the manifest"),
]


def build_parser() -> argparse.ArgumentParser:
    # Every parser's help ends with the exit codes, kept as written.
    common = {"epilog": EXIT_CODES_HELP, "formatter_class": argparse.RawDescriptionHelpFormatter}
    parser = argparse.ArgumentParser(
        prog="rbench",
        description="A hermetic, resumable benchmark for AI code-review engines.",
        **common,
    )
    parser.add_argument("--version", action="version", version=f"rbench {version('rbench')}")
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    tasks = commands.add_parser("tasks", help="manage task sets", **common)
    task_commands = tasks.add_subparsers(
        dest="task_command", metavar="<task-command>", required=True
    )
    for name, help_ in TASK_COMMANDS:
        sub = task_commands.add_parser(name, help=help_, description=help_, **common)
        if name == "import":
            reviewbench.add_arguments(sub)

    for name, help_ in COMMANDS:
        sub = commands.add_parser(name, help=help_, description=help_, **common)
        if name == "run":
            sub.add_argument(
                "--wait",
                action="store_true",
                help="on a limit, pause only that provider and resume it after the reset",
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "tasks" and args.task_command == "import":
        return reviewbench.run(args)
    name = " ".join(filter(None, [args.command, getattr(args, "task_command", None)]))
    print(f"rbench {name}: not implemented yet", file=sys.stderr)
    return ExitCode.ERROR
