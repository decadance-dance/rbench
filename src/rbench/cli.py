"""The `rbench` command line.

The command list and exit codes are provisional until each command's issue fixes its behavior.
"""

import argparse
import sys
from enum import IntEnum
from importlib.metadata import version
from pathlib import Path

from rbench.ledger import load_plan, start_run
from rbench.plan import command as plan_command


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
        task_commands.add_parser(name, help=help_, description=help_, **common)

    for name, help_ in COMMANDS:
        sub = commands.add_parser(name, help=help_, description=help_, **common)
        if name == "plan":
            plan_command.add_arguments(sub)
        if name == "run":
            sub.add_argument("matrix", type=Path, help="the matrix file")
            sub.add_argument("--round", required=True, help="the matrix round to run")
            sub.add_argument(
                "--images",
                type=Path,
                required=True,
                help="JSON file: per engine, its image by digest and the CLI versions in it",
            )
            sub.add_argument(
                "--engines",
                type=Path,
                default=Path("engines"),
                help="engine declarations (default: engines)",
            )
            sub.add_argument(
                "--tasks", type=Path, default=Path("tasks"), help="task sets (default: tasks)"
            )
            sub.add_argument(
                "--runs", type=Path, default=Path("runs"), help="run directories (default: runs)"
            )
            sub.add_argument(
                "--run-id", help="the run directory's name (default: UTC time, stage, round)"
            )
            sub.add_argument(
                "--wait",
                action="store_true",
                help="on a limit, pause only that provider and resume it after the reset",
            )
    return parser


def _run(args: argparse.Namespace) -> int:
    try:
        run_dir = start_run(
            args.matrix,
            args.round,
            images_path=args.images,
            engines_dir=args.engines,
            tasks_root=args.tasks,
            runs_dir=args.runs,
            run_id=args.run_id,
        )
        trials = len(load_plan(run_dir).trials)
    except (OSError, ValueError) as e:
        print(f"rbench run: {e}", file=sys.stderr)
        return ExitCode.ERROR
    print(f"created {run_dir}: {trials} trials pending")
    print("rbench run: the trial executor is not implemented yet", file=sys.stderr)
    return ExitCode.ERROR


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "plan":
        return plan_command.run(args)
    if args.command == "run":
        return _run(args)
    name = " ".join(filter(None, [args.command, getattr(args, "task_command", None)]))
    print(f"rbench {name}: not implemented yet", file=sys.stderr)
    return ExitCode.ERROR
