"""The `rbench` command line.

The command list and exit codes are provisional until each command's issue fixes its behavior.
"""

import argparse
import shlex
import sys
from contextlib import ExitStack
from enum import IntEnum
from importlib.metadata import version
from pathlib import Path

from rbench.ledger import TRIALS_DIR, Plan, load_plan, matrix_warning, read_status, start_run
from rbench.ledger.resume import recover_trials, run_lock
from rbench.plan import command as plan_command
from rbench.run import executor
from rbench.tasks import ours, release, reviewbench
from rbench.tasks.archive import ArchiveError
from rbench.tasks.release import default_archive_dir
from rbench.trial.images import TrialError


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
            sub.add_argument(
                "--engines",
                type=Path,
                default=Path("engines"),
                help="engine declarations (default: engines)",
            )
            sub.add_argument(
                "--tasks", type=Path, default=Path("tasks"), help="task sets (default: tasks)"
            )
        if name in ("run", "resume"):
            sub.add_argument(
                "--archives",
                type=Path,
                default=None,
                help="task source archives (default: the `rbench tasks fetch` cache)",
            )
            sub.add_argument(
                "--claude-concurrency",
                type=int,
                default=executor.CLAUDE_CONCURRENCY,
                metavar="N",
                help=f"Claude trials at once (default: {executor.CLAUDE_CONCURRENCY}); Codex"
                f" runs one at a time on the login {executor.CODEX_LOGIN_ENV} names",
            )
    return parser


def _resume_command(run_dir: Path, plan: Plan, args: argparse.Namespace) -> str:
    """The command that resumes the run with the options this runner was given, where they differ
    from `rbench resume`'s defaults; a run with hidden tasks keeps its directory unnamed."""
    words = []
    for flag, value, default in (
        ("--engines", args.engines, Path("engines")),
        ("--tasks", args.tasks, Path("tasks")),
        ("--archives", args.archives, None),
        ("--claude-concurrency", args.claude_concurrency, executor.CLAUDE_CONCURRENCY),
    ):
        if value != default:
            words += [flag, str(value)]
    where = "<its run directory>" if "hidden" in plan.task_sets else shlex.quote(str(run_dir))
    return " ".join(["rbench resume", where, *map(shlex.quote, words)])


def _execute(command: str, run_dir: Path, plan: Plan, args: argparse.Namespace) -> int:
    """Run the trials left in `run_dir`; the caller holds the run lock."""
    resume = _resume_command(run_dir, plan, args)
    try:
        queue = recover_trials(run_dir, plan)
        print(f"{len(queue)} trials to run")
        ctx = executor.prepare_run(
            run_dir,
            plan,
            engines_dir=args.engines,
            tasks_root=args.tasks,
            archives=args.archives or default_archive_dir(),
        )
        counts = executor.run_trials(ctx, queue, claude_concurrency=args.claude_concurrency)
    except executor.LimitReached as e:
        print(f"{command}: stopped at a limit: {e}", file=sys.stderr)
        print(f"resume after the limit resets: {resume}", file=sys.stderr)
        return ExitCode.STOPPED
    except executor.RunStopped as e:
        print(f"{command}: stopped: {e}", file=sys.stderr)
        print(f"fix the cause, then resume: {resume}", file=sys.stderr)
        return ExitCode.ERROR
    except (OSError, ValueError, TrialError, ArchiveError) as e:
        print(f"{command}: {e}", file=sys.stderr)
        return ExitCode.ERROR
    print(", ".join(f"{n} {state}" for state, n in sorted(counts.items())) or "nothing to run")
    return ExitCode.OK


def _run(args: argparse.Namespace) -> int:
    with ExitStack() as lock:
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
                lock=lock,
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
        return _execute("rbench run", run_dir, plan, args)


def _resume(args: argparse.Namespace) -> int:
    run_dir = args.run
    try:
        with run_lock(run_dir) as previous:
            if previous:
                print(f"warning: recovered stale run lock from {previous}", file=sys.stderr)
            plan = load_plan(run_dir)
            if warning := matrix_warning(plan):
                print(warning, file=sys.stderr)
            return _execute("rbench resume", run_dir, plan, args)
    except (OSError, ValueError) as e:
        print(f"rbench resume: {e}", file=sys.stderr)
        return ExitCode.ERROR


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "plan":
        return plan_command.run(args)
    if args.command == "run":
        return _run(args)
    if args.command == "resume":
        return _resume(args)
    if args.command == "tasks" and args.task_command in TASK_RUN:
        return TASK_RUN[args.task_command](args)
    name = " ".join(filter(None, [args.command, getattr(args, "task_command", None)]))
    print(f"rbench {name}: not implemented yet", file=sys.stderr)
    return ExitCode.ERROR
