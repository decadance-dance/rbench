"""The `rbench plan` command: read the inputs, print the plan, refuse a round over its budget."""

import argparse
import sys
from pathlib import Path

from rbench.engines.declaration import Engine, load_engine
from rbench.matrix.expansion import Matrix, load_matrix
from rbench.plan.estimate import load_smoke, plan_round, render
from rbench.tasks import CORE_LIST_FILE, MANIFEST_FILE, load_core_list, load_manifest

PUBLIC_SETS = ("reviewbench", "ours")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("matrix", type=Path, help="the matrix file")
    parser.add_argument("--round", help="plan only this round (default: every round)")
    parser.add_argument(
        "--engines",
        type=Path,
        required=True,
        metavar="DIR",
        help="directory of engine declarations (*.toml)",
    )
    parser.add_argument(
        "--tasks-root",
        type=Path,
        default=Path("tasks"),
        metavar="DIR",
        help=f"holds {CORE_LIST_FILE} and one directory per public set (default: tasks)",
    )
    parser.add_argument(
        "--hidden",
        type=Path,
        metavar="DIR",
        help="the hidden task set, counted when the stage plans all tasks",
    )
    parser.add_argument(
        "--smoke", type=Path, metavar="FILE", help="smoke medians (default: the DEFAULT estimate)"
    )


def load_engines(directory: Path) -> dict[str, Engine]:
    if not directory.is_dir():
        raise ValueError(f"engine directory {directory} does not exist")
    engines: dict[str, Engine] = {}
    for path in sorted(directory.glob("*.toml")):
        engine = load_engine(path)
        if engine.name in engines:
            raise ValueError(f"{path}: engine {engine.name!r} is declared twice")
        engines[engine.name] = engine
    return engines


def select_tasks(matrix: Matrix, root: Path, hidden: Path | None) -> tuple[int, str]:
    """How many tasks the stage plans, and where they come from; only ids are read."""
    if matrix.tasks == "core":
        core = load_core_list(root / CORE_LIST_FILE)
        return len(matrix.select_tasks(core.task_ids, ())), f"core list v{core.version}"
    sets = [(s, root / s) for s in PUBLIC_SETS] + ([("hidden", hidden)] if hidden else [])
    ids = [i for s, d in sets for i in load_manifest(d / MANIFEST_FILE, s).tasks]
    return len(matrix.select_tasks((), ids)), f"sets {', '.join(s for s, _ in sets)}"


def run(args: argparse.Namespace) -> int:
    """Print the plan; 0 when it fits, 1 on bad input or a round over its budget."""
    try:
        matrix = load_matrix(args.matrix, load_engines(args.engines))
        rounds = [r for r in matrix.rounds if args.round in (None, r.name)]
        if not rounds:
            names = ", ".join(r.name for r in matrix.rounds) or "none"
            what = f"no round {args.round!r}" if args.round else "no rounds"
            raise ValueError(f"{args.matrix}: {what}; rounds: {names}")
        tasks, origin = select_tasks(matrix, args.tasks_root, args.hidden)
        smoke = load_smoke(args.smoke) if args.smoke else None
    except (ValueError, OSError) as e:
        print(f"rbench plan: {e}", file=sys.stderr)
        return 1
    plans = [plan_round(r, tasks, matrix.attempts, smoke) for r in rounds]
    sys.stdout.write(render(str(args.matrix), matrix, tasks, origin, plans, smoke))
    refused = [(p.round.name, provider, p) for p in plans for provider in p.over_budget()]
    for name, provider, plan in refused:
        print(
            f"rbench plan: refusing round {name!r}: the {provider} estimate"
            f" {plan.total(provider)} exceeds its budget {plan.round.budget[provider]}",
            file=sys.stderr,
        )
    return 1 if refused else 0
