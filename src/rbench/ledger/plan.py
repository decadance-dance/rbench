"""Frozen plans and run directories (spec run-ledger, Frozen plan; design D8).

`runs/<run-id>/` holds:

    plan.json            the frozen plan, written once when the run is created
    ledger.lock          the run's lock file; taking it is resume's job
    trials/<trial-id>/   one per trial (see `rbench.ledger.trials`), created `pending`

The plan is one round of a matrix: its configurations, the version of every task set and the
content hash of every planned task, each engine's pin, the CLI versions and the image digest, and
the trials in queue order (attempt, then task, then configuration). It holds no timestamps, so the
same inputs give the same bytes, and the run never re-reads its matrix: an edited matrix only
earns a warning (`matrix_warning`).

Image digests and the CLI versions found in each image come from the image build, so they are an
explicit input here (`Image`) and must match the versions the engine declaration pins.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from rbench.atomic import atomic_write
from rbench.engines.declaration import Engine, load_engine
from rbench.engines.pins import check_ref
from rbench.ledger.trials import RAW_DIR, LedgerError, dump_json, set_state
from rbench.matrix.expansion import Configuration, Matrix, MatrixError, parse_matrix
from rbench.tasks.loader import Task
from rbench.tasks.manifest import (
    CORE_LIST_FILE,
    CoreList,
    Manifest,
    load_core_list,
    load_task_set,
    select_core,
)

# Bumped whenever plan.json changes shape; a run of another format is refused, not guessed at.
PLAN_FORMAT = 1
PLAN_FILE = "plan.json"
LOCK_FILE = "ledger.lock"
TRIALS_DIR = "trials"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
# The hidden set's directory is operator configuration (issue #35); until then runs plan these.
PUBLIC_SETS = ("reviewbench", "ours")


@dataclass(frozen=True)
class Image:
    ref: str  # <name>@sha256:<64 hex>
    clis: dict[str, str]  # CLI -> the version installed in the image


def trial_id(task_id: str, configuration: Configuration, attempt: int) -> str:
    """Readable and short enough for a directory name; unique within a plan (checked there)."""
    return f"{task_id}--{configuration.hash[:12]}--{attempt}"


def _who(task: Task) -> str:
    return "a hidden task" if task.set == "hidden" else f"task {task.id!r}"


def freeze_plan(
    matrix: Matrix,
    round_name: str,
    engines: Mapping[str, Engine],
    task_sets: Sequence[tuple[Manifest, Sequence[Task]]],
    core: CoreList | None,
    images: Mapping[str, Image],
    matrix_path: str,
    matrix_sha256: str,
) -> dict:
    """The frozen plan of round `round_name` of `matrix`, as plan.json stores it.

    `task_sets` are `load_task_set` results, so each task already matches its manifest's hash.
    `core` is the core list, needed when the matrix plans core tasks. `images` maps each engine
    to its image. `matrix_path` and `matrix_sha256` name the matrix file as it was read.
    """
    round_ = next((r for r in matrix.rounds if r.name == round_name), None)
    if round_ is None:
        names = ", ".join(r.name for r in matrix.rounds)
        raise LedgerError(f"matrix {matrix_path}: no round {round_name!r}; rounds: {names}")
    providers = sorted(frozenset().union(*(c.providers for c in round_.configurations)))
    budget = {p: round_.budget_for(p) for p in providers}

    engine_records = {}
    for name in sorted({c.engine for c in round_.configurations}):
        where = f"engine {name!r}"
        if (engine := engines.get(name)) is None:
            raise LedgerError(f"{where}: the matrix uses it, but it is not declared")
        if engine.pin_kind == "git" and engine.pin_ref.startswith("refs/tags/"):
            # Configuration ids are only as exact as the version (Configuration.settings).
            raise LedgerError(f"{where}: resolve tag {engine.pin_ref} to its commit first")
        if (image := images.get(name)) is None:
            raise LedgerError(f"{where}: no image given")
        ref = check_ref(where, "image", image.ref, "image")
        if image.clis != engine.clis:
            raise LedgerError(
                f"{where}: image {ref} has CLIs {image.clis}, but the declaration pins"
                f" {engine.clis}"
            )
        engine_records[name] = {
            "pin": {"kind": engine.pin_kind, "ref": engine.pin_ref},
            "version": engine.version,
            "clis": engine.clis,
            "image": ref,
        }
    for c in round_.configurations:
        if c.version != engines[c.engine].version:
            raise LedgerError(
                f"configuration {c.id}: expanded against another version of {c.engine!r}"
            )

    versions: dict[str, int] = {}
    planned: dict[str, dict] = {}  # task id -> its plan entry
    loaded: list[Task] = []
    for manifest, tasks in task_sets:
        if manifest.set in versions:
            raise LedgerError(f"task set {manifest.set!r} is given twice")
        versions[manifest.set] = manifest.version
        for task in tasks:
            if task.set != manifest.set or task.id not in manifest.tasks:
                raise LedgerError(f"{_who(task)} is not in the {manifest.set!r} manifest")
            if task.id in planned:
                raise LedgerError(f"{_who(task)}: its id is loaded twice")
            planned[task.id] = {
                "id": task.id,
                "set": task.set,
                "sha256": manifest.tasks[task.id].sha256,
            }
            loaded.append(task)
    core_ids: list[str] = []
    if matrix.tasks == "core":
        if core is None:
            raise LedgerError(f"the {matrix.stage} stage plans core tasks; give the core list")
        core_ids = [t.id for t in select_core(core, loaded)]
    selected = matrix.select_tasks(core_ids, sorted(planned))

    trials = [
        {
            "id": trial_id(task, c, attempt),
            "attempt": attempt,
            "task": task,
            "configuration": c.id,
        }
        for attempt in range(1, matrix.attempts + 1)
        for task in selected
        for c in round_.configurations
    ]
    if len({t["id"] for t in trials}) != len(trials):
        raise LedgerError("two trials share an id; two configurations share a hash prefix")

    return {
        "format": PLAN_FORMAT,
        "matrix": {"path": matrix_path, "sha256": matrix_sha256},
        "stage": matrix.stage,
        "round": {"name": round_.name, "budget": budget},
        "attempts": matrix.attempts,
        "configurations": [
            {"id": c.id, "providers": sorted(c.providers), "settings": c.settings}
            for c in round_.configurations
        ],
        "engines": engine_records,
        "task_sets": versions,
        "tasks": [planned[t] for t in selected],
        "trials": trials,
    }


def create_run(runs_dir: Path, run_id: str, plan: dict) -> Path:
    """Create `runs_dir/run_id` with the plan, the lock file and every trial pending.

    The directory is built under a temporary name and renamed into place, so a run directory
    either holds the whole plan and every trial, or does not exist. An existing run is refused.
    """
    runs_dir = Path(runs_dir)
    if not RUN_ID.fullmatch(run_id):
        raise LedgerError(f"bad run id {run_id!r}; use letters, digits, '.', '_' and '-'")
    run_dir = runs_dir / run_id
    if run_dir.exists():
        raise LedgerError(f"run {run_dir} already exists; resume it or choose another run id")
    runs_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=runs_dir, prefix=f".{run_id}.", suffix=".tmp"))
    try:
        atomic_write(tmp / PLAN_FILE, dump_json(plan))
        atomic_write(tmp / LOCK_FILE, b"")
        (tmp / TRIALS_DIR).mkdir(mode=0o700)
        for trial in plan["trials"]:
            trial_dir = tmp / TRIALS_DIR / trial["id"]
            trial_dir.mkdir(mode=0o700)
            (trial_dir / RAW_DIR).mkdir(mode=0o700)
            set_state(trial_dir, "pending")
        # Fails if another runner created the same run meanwhile: the target is not empty.
        os.rename(tmp, run_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return run_dir


def load_plan(run_dir: Path) -> dict:
    """Read a run's frozen plan; refuse a plan written in another format."""
    path = Path(run_dir) / PLAN_FILE
    try:
        plan = json.loads(path.read_bytes())
    except (OSError, ValueError) as e:
        raise LedgerError(f"{path}: cannot read the plan: {e}") from e
    found = plan.get("format") if isinstance(plan, dict) else None
    if found != PLAN_FORMAT:
        raise LedgerError(
            f"{path}: plan format {found!r}, but this rbench reads format {PLAN_FORMAT};"
            " continue the run with the rbench version that started it, or start a new run"
        )
    return plan


def matrix_warning(plan: dict) -> str | None:
    """A warning when the plan's matrix file changed or is gone; the run keeps its plan.

    A relative matrix path is read from the current directory, as the run read it.
    """
    path = Path(plan["matrix"]["path"])
    try:
        changed = hashlib.sha256(path.read_bytes()).hexdigest() != plan["matrix"]["sha256"]
    except OSError:
        changed = True
    if not changed:
        return None
    return (
        f"warning: matrix file {path} changed or is gone since the run started; the run"
        " continues with its frozen plan (a changed matrix needs a new run)"
    )


def load_engines(engines_dir: Path) -> dict[str, Engine]:
    """Every engine declaration (`*.toml`) in `engines_dir`, by name."""
    engines: dict[str, Engine] = {}
    for path in sorted(Path(engines_dir).glob("*.toml")):
        engine = load_engine(path)
        if engine.name in engines:
            raise LedgerError(f"{path}: engine {engine.name!r} is declared twice")
        engines[engine.name] = engine
    if not engines:
        raise LedgerError(f"no engine declarations (*.toml) in {engines_dir}")
    return engines


def load_images(path: Path) -> dict[str, Image]:
    """Read `{"<engine>": {"image": "<name>@sha256:<hex>", "clis": {"<cli>": "<version>"}}}`."""
    try:
        data = json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as e:
        raise LedgerError(f"images file {path}: cannot read it: {e}") from e
    if not isinstance(data, dict):
        raise LedgerError(f"images file {path}: expected an object of engines")
    images = {}
    for name, raw in data.items():
        where = f"images file {path}: engine {name!r}"
        if not (isinstance(raw, dict) and raw.keys() == {"image", "clis"}):
            raise LedgerError(f"{where}: expected exactly the fields image and clis")
        clis = raw["clis"]
        if not (
            isinstance(raw["image"], str)
            and isinstance(clis, dict)
            and all(isinstance(v, str) for v in clis.values())
        ):
            raise LedgerError(f"{where}: image must be a string and clis an object of strings")
        images[name] = Image(raw["image"], dict(clis))
    return images


def start_run(
    matrix_path: Path,
    round_name: str,
    *,
    images_path: Path,
    engines_dir: Path,
    tasks_root: Path,
    runs_dir: Path,
    run_id: str | None = None,
) -> Path:
    """Load the inputs from disk, freeze the plan of one round and create its run directory.

    The default run id is the UTC start time, the stage and the round.
    """
    raw = Path(matrix_path).read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise MatrixError(f"{matrix_path}: not a valid UTF-8 TOML file: {e}") from e
    engines = load_engines(engines_dir)
    matrix = parse_matrix(data, str(matrix_path), engines)
    tasks_root = Path(tasks_root)
    task_sets = [load_task_set(tasks_root / s, s) for s in PUBLIC_SETS if (tasks_root / s).is_dir()]
    core = load_core_list(tasks_root / CORE_LIST_FILE) if matrix.tasks == "core" else None
    plan = freeze_plan(
        matrix,
        round_name,
        engines,
        task_sets,
        core,
        load_images(images_path),
        str(matrix_path),
        hashlib.sha256(raw).hexdigest(),
    )
    if run_id is None:
        run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{matrix.stage}-{round_name}"
    return create_run(runs_dir, run_id, plan)
