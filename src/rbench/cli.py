"""The `rbench` command line.

The command list and exit codes are provisional until each command's issue fixes its behavior.
"""

import argparse
import sys
from enum import IntEnum
from importlib.metadata import version
from pathlib import Path

from rbench.ledger import TRIALS_DIR, load_plan, matrix_warning, read_status, start_run
from rbench.ledger.resume import recover_trials, run_lock
from rbench.plan import command as plan_command
from rbench.tasks import ours, release, reviewbench


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
    ("release", "publish a public set version's source archives as a GitHub release"),
]
# (set, help, arguments, run) of `rbench tasks import <set>`.
IMPORTERS = [
    (
        "reviewbench",
        "import the pinned ReviewBench subset",
        reviewbench.add_arguments,
        reviewbench.run,
    ),
    ("ours", "mine merged PRs of public repositories", ours.add_arguments, ours.run),
]


def _import_arguments(parser: argparse.ArgumentParser) -> None:
    sets = parser.add_subparsers(dest="set", metavar="<set>", required=True)
    for name, help_, add_arguments, _ in IMPORTERS:
        add_arguments(sets.add_parser(name, help=help_, description=help_))


def _import(args: argparse.Namespace) -> int:
    return next(run for name, _, _, run in IMPORTERS if name == args.set)(args)


TASK_ARGUMENTS = {
    "import": _import_arguments,
    "fetch": release.add_fetch_arguments,
    "release": release.add_release_arguments,
}
TASK_RUN = {"import": _import, "fetch": release.run_fetch, "release": release.run_release}


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
        if name in TASK_ARGUMENTS:
            TASK_ARGUMENTS[name](sub)

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
                "--reuse",
                type=Path,
                metavar="RUN_DIRECTORY",
                help="copy matching done trials into a new smoke or screening run",
            )
            sub.add_argument(
                "--wait",
                action="store_true",
                help="on a limit, pause only that provider and resume it after the reset",
            )
        if name == "resume":
            sub.add_argument("run", type=Path, help="the run directory to resume")
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
            reuse=args.reuse,
        )
    except (OSError, ValueError) as e:
        print(f"rbench run: {e}", file=sys.stderr)
        return ExitCode.ERROR
    try:
        plan = load_plan(run_dir)
    except (OSError, ValueError):
        print("rbench run: cannot read the newly created plan.json", file=sys.stderr)
        return ExitCode.ERROR
    pending = len(plan.trials)
    if args.reuse is not None:
        try:
            pending = sum(
                read_status(run_dir / TRIALS_DIR / trial.id).state == "pending"
                for trial in plan.trials
            )
        except (OSError, ValueError):
            print("rbench run: cannot read the newly created trial statuses", file=sys.stderr)
            return ExitCode.ERROR
    label = "run with hidden tasks" if "hidden" in plan.task_sets else str(run_dir)
    print(f"created {label}: {pending} trials pending")
    print("rbench run: the trial executor is not implemented yet", file=sys.stderr)
    return ExitCode.ERROR


def _resume(run_dir: Path) -> int:
    try:
        with run_lock(run_dir) as previous:
            if previous:
                print(f"warning: recovered stale run lock from {previous}", file=sys.stderr)
            plan = load_plan(run_dir)
            if warning := matrix_warning(plan):
                print(warning, file=sys.stderr)
            queue = recover_trials(run_dir, plan)
    except (OSError, ValueError) as e:
        print(f"rbench resume: {e}", file=sys.stderr)
        return ExitCode.ERROR
    print(f"{run_dir}: {len(queue)} trials ready to rerun")
    print("rbench resume: the trial executor is not implemented yet", file=sys.stderr)
    return ExitCode.ERROR


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "plan":
        return plan_command.run(args)
    if args.command == "run":
        return _run(args)
    if args.command == "resume":
        return _resume(args.run)
    if args.command == "tasks" and args.task_command in TASK_RUN:
        return TASK_RUN[args.task_command](args)
    name = " ".join(filter(None, [args.command, getattr(args, "task_command", None)]))
    print(f"rbench {name}: not implemented yet", file=sys.stderr)
    return ExitCode.ERROR
