"""Frozen plans and run directories (spec run-ledger, Frozen plan; design D8).

`runs/<run-id>/` holds:

    plan.json            the frozen plan, written once when the run is created
    ledger.lock          the run's lock file; taking it is resume's job
    trials/<trial-id>/   one per trial, pending or reused as done (see `rbench.ledger.trials`)

The plan is one round of a matrix: its configurations, the version of every task set and the
content hash of every planned task, each engine's pin, CLI versions, image digest and how each
role receives its model and effort, and the trials in queue order (attempt, then task, then
configuration). It holds no timestamps, so the same inputs give the same bytes, and the run never
re-reads its matrix: an edited matrix only earns a warning (`matrix_warning`).

Hidden tasks use the same frozen fields as public tasks: set version, task id and content hash.
The plan never stores their directory, canary or ground truth.

The matrix path is stored relative to the rbench checkout, never as an absolute or home path,
because run directories may be published; a matrix outside the checkout is refused.

Image digests and the CLI versions found in each image come from the image build, so they are an
explicit input here (`Image`) and must match the versions the engine declaration pins.
"""

import dataclasses
import hashlib
import itertools
import json
import os
import re
import shutil
import tempfile
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rbench.atomic import atomic_write
from rbench.engines.declaration import CLIS, Engine, Setting, load_engine, parse_engine
from rbench.engines.pins import MODELS, FloatingRefError, check_ref
from rbench.ledger.trials import (
    RAW_DIR,
    RUN_ID,
    LedgerError,
    dump_json,
    no_repeated_keys,
    read_results,
    read_status,
    set_state,
    store_results,
)
from rbench.matrix.expansion import (
    STAGES,
    Configuration,
    Matrix,
    MatrixError,
    RoleSetting,
    can_reuse,
    parse_matrix,
)
from rbench.tasks.hidden import check_hidden_results_dir, hidden_config
from rbench.tasks.loader import TASK_ID, TASK_SETS, Task, TaskError
from rbench.tasks.manifest import (
    CORE_LIST_FILE,
    CoreList,
    Manifest,
    load_core_list,
    load_hidden_task_set,
    load_task_set,
    select_core,
)

# Bumped whenever plan.json changes shape; a run of another format is refused, not guessed at.
PLAN_FORMAT = 1
PLAN_FILE = "plan.json"
LOCK_FILE = "ledger.lock"
TRIALS_DIR = "trials"
PUBLIC_SETS = ("reviewbench", "ours")
PIN_KINDS = ("git", "version", "sha256", "cli")
# limit: the checkout of an editable install (`uv run`); an installed wheel has no checkout, so
# every matrix is outside it and `rbench run` refuses.
CHECKOUT_ROOT = Path(__file__).resolve().parents[3]
_HEX64 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Image:
    ref: str  # <name>@sha256:<64 hex>
    clis: dict[str, str]  # CLI -> the version installed in the image


@dataclass(frozen=True)
class MatrixFile:
    path: str  # POSIX, relative to the rbench checkout
    sha256: str


@dataclass(frozen=True)
class PlannedRound:
    name: str
    budget: dict[str, int]  # provider -> tokens, for every provider the round uses


@dataclass(frozen=True)
class Pin:
    kind: str
    ref: str


@dataclass(frozen=True)
class PlannedRole:
    cli: str | None  # the declared CLI; None: the model's CLI
    delivery: str  # flag | config
    default: Setting  # the declared default, so the frozen declaration can be checked whole


@dataclass(frozen=True)
class PlannedEngine:
    pin: Pin
    version: str
    clis: dict[str, str]
    image: str
    roles: dict[str, PlannedRole]


@dataclass(frozen=True)
class PlannedConfiguration:
    id: str
    providers: tuple[str, ...]
    settings: dict


@dataclass(frozen=True)
class PlannedTask:
    id: str
    set: str
    sha256: str


@dataclass(frozen=True)
class PlannedTrial:
    id: str
    attempt: int
    task: str
    configuration: str


@dataclass(frozen=True)
class Plan:
    format: int
    matrix: MatrixFile
    stage: str
    round: PlannedRound
    attempts: int
    configurations: tuple[PlannedConfiguration, ...]
    engines: dict[str, PlannedEngine]
    task_sets: dict[str, int]
    tasks: tuple[PlannedTask, ...]
    trials: tuple[PlannedTrial, ...]


def trial_id(task_id: str, configuration: Configuration, attempt: int) -> str:
    """Readable and short enough for a directory name; unique within a plan (checked there)."""
    return f"{task_id}--{configuration.hash[:12]}--{attempt}"


def _who(task: Task) -> str:
    return "a hidden task" if task.set == "hidden" else f"task {task.id!r}"


def _exact(where: str, kind: str, value: object, field: str) -> str:
    try:
        return check_ref(where, kind, value, field)
    except FloatingRefError as e:
        raise LedgerError(str(e)) from None


def check_image(where: str, image: Image) -> None:
    """The image is referenced by digest, and its CLIs are known CLIs at exact versions."""
    _exact(where, "image", image.ref, "image")
    if not (isinstance(image.clis, dict) and image.clis):
        raise LedgerError(f"{where}: clis must name at least one CLI")
    for cli, version in image.clis.items():
        if cli not in CLIS:
            raise LedgerError(f"{where}: clis: unknown CLI {cli!r}; expected one of {CLIS}")
        _exact(where, "version", version, f"clis.{cli}")


def freeze_plan(
    matrix: Matrix,
    round_name: str,
    engines: Mapping[str, Engine],
    task_sets: Sequence[tuple[Manifest, Sequence[Task]]],
    core: CoreList | None,
    images: Mapping[str, Image],
    matrix_path: str,
    matrix_sha256: str,
) -> Plan:
    """The frozen plan of round `round_name` of `matrix`.

    `task_sets` are `load_task_set` results, so each task already matches its manifest's hash.
    `core` is the core list, needed when the matrix plans core tasks. `images` maps each engine
    to its image; every entry is checked, used or not. `matrix_path` (relative to the checkout)
    and `matrix_sha256` name the matrix file as it was read.
    """
    for name, image in sorted(images.items()):
        check_image(f"image of engine {name!r}", image)
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
        if image.clis != engine.clis:
            raise LedgerError(
                f"{where}: image {image.ref} has CLIs {image.clis}, but the declaration pins"
                f" {engine.clis}"
            )
        engine_records[name] = {
            "pin": {"kind": engine.pin_kind, "ref": engine.pin_ref},
            "version": engine.version,
            "clis": engine.clis,
            "image": image.ref,
            # How each role gets its model and effort: a changed declaration changes the launch.
            "roles": {
                r: {
                    "cli": role.cli,
                    "delivery": role.delivery,
                    "default": {"model": role.default.model, "effort": role.default.effort},
                }
                for r, role in engine.roles.items()
            },
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
    data = {
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
    # The same checks resume applies, so a plan that is written can always be read back.
    return parse_plan(data, "frozen plan")


def _object(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        raise LedgerError(f"{where}: expected an object")
    return value


def _fields(value: object, where: str, keys: set[str]) -> dict:
    value = _object(value, where)
    if value.keys() != keys:
        raise LedgerError(f"{where}: expected exactly the fields {', '.join(sorted(keys))}")
    return value


def _list(value: object, where: str) -> list:
    if not (isinstance(value, list) and value):
        raise LedgerError(f"{where}: expected a non-empty list")
    return value


def _string(value: object, where: str, pattern: re.Pattern | None = None) -> str:
    if not (isinstance(value, str) and value and (pattern is None or pattern.fullmatch(value))):
        raise LedgerError(f"{where}: expected a valid string")
    return value


def _count(value: object, where: str) -> int:
    if type(value) is not int or value < 1:
        raise LedgerError(f"{where}: expected a positive integer")
    return value


def _one_of(value: object, where: str, allowed: Sequence[str]) -> str:
    if not (isinstance(value, str) and value in allowed):
        raise LedgerError(f"{where}: expected one of {', '.join(allowed)}")
    return value


def parse_plan(data: object, where: str) -> Plan:
    """Validate a plan as plan.json stores it; errors name `where` and the field or trial."""
    found = data.get("format") if isinstance(data, dict) else None
    if type(found) is not int or found != PLAN_FORMAT:
        raise LedgerError(
            f"{where}: format: plan format {found!r}, but this rbench reads format {PLAN_FORMAT};"
            " continue the run with the rbench version that started it, or start a new run"
        )
    d = _fields(
        data,
        where,
        {"format", "matrix", "stage", "round", "attempts", "configurations", "engines"}
        | {"task_sets", "tasks", "trials"},
    )

    m = _fields(d["matrix"], f"{where}: matrix", {"path", "sha256"})
    path = _string(m["path"], f"{where}: matrix.path")
    posix = PurePosixPath(path)
    bad = posix.is_absolute() or ".." in posix.parts or posix.as_posix() != path
    if bad or "\\" in path or "\0" in path:
        raise LedgerError(f"{where}: matrix.path: expected a plain path relative to the checkout")
    matrix = MatrixFile(path, _string(m["sha256"], f"{where}: matrix.sha256", _HEX64))
    stage = _one_of(d["stage"], f"{where}: stage", tuple(STAGES))
    _, max_tasks, stage_attempts = STAGES[stage]
    attempts = _count(d["attempts"], f"{where}: attempts")
    if attempts != stage_attempts:
        raise LedgerError(f"{where}: attempts: the {stage} stage runs {stage_attempts}")

    engines: dict[str, PlannedEngine] = {}
    declared: dict[str, Engine] = {}
    for name, raw in sorted(_object(d["engines"], f"{where}: engines").items()):
        at = f"{where}: engines.{name}"
        e = _fields(raw, at, {"pin", "version", "clis", "image", "roles"})
        clis = _object(e["clis"], f"{at}.clis")
        image = Image(e["image"], clis)
        check_image(at, image)
        roles = _object(e["roles"], f"{at}.roles")
        for role, r in roles.items():
            _fields(r, f"{at}.roles.{role}", {"cli", "delivery", "default"})
        # The declaration's own rules (pin, CLI pins, role CLI and delivery, defaults) apply to
        # the frozen copy, so a hand-edited plan cannot hold a declaration the loader rejects.
        declaration = {
            "name": name,
            "pin": e["pin"],
            "cli": clis,
            "roles": {
                role: {k: v for k, v in r.items() if not (k == "cli" and v is None)}
                for role, r in roles.items()
            },
        }
        try:
            engine = parse_engine(declaration, at)
        except (TypeError, ValueError) as error:
            raise LedgerError(f"{at}: not a valid engine declaration: {error}") from None
        if engine.pin_kind == "git" and engine.pin_ref.startswith("refs/tags/"):
            raise LedgerError(f"{at}.pin.ref: a tag; the plan needs its commit")
        if e["version"] != engine.version:
            raise LedgerError(f"{at}.version: not the version its pin gives")
        declared[name] = engine
        engines[name] = PlannedEngine(
            Pin(engine.pin_kind, engine.pin_ref),
            engine.version,
            dict(sorted(engine.clis.items())),
            image.ref,
            {
                role: PlannedRole(r.cli, r.delivery, r.default)
                for role, r in sorted(engine.roles.items())
            },
        )

    configurations: dict[str, tuple[PlannedConfiguration, Configuration]] = {}
    for i, raw in enumerate(_list(d["configurations"], f"{where}: configurations")):
        at = f"{where}: configurations[{i}]"
        c = _fields(raw, at, {"id", "providers", "settings"})
        keys = {"engine", "version", "roles"}
        if isinstance(c["settings"], dict) and "inputs" in c["settings"]:
            keys.add("inputs")  # only an engine with input files, such as `plain`, has it
        s = _fields(c["settings"], f"{at}.settings", keys)
        engine = engines.get(s["engine"]) if isinstance(s["engine"], str) else None
        if engine is None:
            raise LedgerError(f"{at}.settings.engine: not an engine of the plan")
        if s["version"] != engine.version:
            raise LedgerError(f"{at}.settings.version: not the plan's version of its engine")
        role_settings = _object(s["roles"], f"{at}.settings.roles")
        if role_settings.keys() != engine.roles.keys():
            raise LedgerError(f"{at}.settings.roles: not the roles of its engine")
        roles = []
        for role, r in sorted(role_settings.items()):
            r = _fields(r, f"{at}.settings.roles.{role}", {"models", "effort"})
            models = _list(r["models"], f"{at}.settings.roles.{role}.models")
            for j, model in enumerate(models):
                _one_of(model, f"{at}.settings.roles.{role}.models[{j}]", tuple(MODELS))
            effort = _string(r["effort"], f"{at}.settings.roles.{role}.effort")
            for model in models:
                try:
                    declared[s["engine"]].delivery(
                        role, Setting(model, effort), f"{at}.settings.roles.{role}"
                    )
                except ValueError as error:
                    raise LedgerError(str(error)) from None
            roles.append((role, RoleSetting(tuple(models), effort)))
        inputs = _object(s.get("inputs", {}), f"{at}.settings.inputs")
        for name, digest in sorted(inputs.items()):
            _string(digest, f"{at}.settings.inputs.{name}", _HEX64)
        config = Configuration(
            s["engine"], s["version"], tuple(roles), tuple(sorted(inputs.items()))
        )
        if config.settings != s or config.id != c["id"]:
            raise LedgerError(f"{at}.id: does not match its settings")
        if c["providers"] != sorted(config.providers):
            raise LedgerError(f"{at}.providers: not the CLIs of its models")
        if config.id in configurations:
            raise LedgerError(f"{at}.id: repeats an earlier configuration")
        configurations[config.id] = (
            PlannedConfiguration(config.id, tuple(c["providers"]), s),
            config,
        )
    used = {c.engine for _, c in configurations.values()}
    if used != engines.keys():
        raise LedgerError(f"{where}: engines: expected exactly the engines of the configurations")

    r = _fields(d["round"], f"{where}: round", {"name", "budget"})
    budget = _object(r["budget"], f"{where}: round.budget")
    providers = {p for _, c in configurations.values() for p in c.providers}
    if budget.keys() != providers:
        raise LedgerError(
            f"{where}: round.budget: expected a budget for exactly {sorted(providers)}"
        )
    round_ = PlannedRound(
        _string(r["name"], f"{where}: round.name"),
        {p: _count(v, f"{where}: round.budget.{p}") for p, v in sorted(budget.items())},
    )

    task_sets = {}
    for name, version in sorted(_object(d["task_sets"], f"{where}: task_sets").items()):
        _one_of(name, f"{where}: task_sets", TASK_SETS)
        task_sets[name] = _count(version, f"{where}: task_sets.{name}")
    tasks: list[PlannedTask] = []
    for i, raw in enumerate(_list(d["tasks"], f"{where}: tasks")):
        at = f"{where}: tasks[{i}]"
        t = _fields(raw, at, {"id", "set", "sha256"})
        task = PlannedTask(
            _string(t["id"], f"{at}.id", TASK_ID),
            _one_of(t["set"], f"{at}.set", tuple(task_sets)),
            _string(t["sha256"], f"{at}.sha256", _HEX64),
        )
        if any(other.id == task.id for other in tasks):
            raise LedgerError(f"{at}.id: repeats an earlier task")
        tasks.append(task)
    if max_tasks is not None and len(tasks) > max_tasks:
        raise LedgerError(f"{where}: tasks: the {stage} stage plans at most {max_tasks}")

    raw_trials = _list(d["trials"], f"{where}: trials")
    count = attempts * len(tasks) * len(configurations)
    if len(raw_trials) != count:
        raise LedgerError(
            f"{where}: trials: {len(raw_trials)} trials, but attempts × tasks × configurations"
            f" gives {count}"
        )
    expected: list[PlannedTrial] = []
    queue = itertools.product(range(1, attempts + 1), tasks, configurations.values())
    for i, (raw, (a, task, (_, c))) in enumerate(zip(raw_trials, queue, strict=True)):
        want = PlannedTrial(trial_id(task.id, c, a), a, task.id, c.id)
        expected.append(want)
        at = f"{where}: trials[{i}]"
        t = _fields(raw, at, {"id", "attempt", "task", "configuration"})
        _count(t["attempt"], f"{at}.attempt")
        for key, value in dataclasses.asdict(want).items():
            if t[key] != value:
                raise LedgerError(
                    f"{at}.{key}: does not match the plan's tasks, configurations and queue order"
                )
    if len({t.id for t in expected}) != len(expected):
        raise LedgerError(f"{where}: trials: two configurations share a hash prefix")

    return Plan(
        PLAN_FORMAT,
        matrix,
        stage,
        round_,
        attempts,
        tuple(p for p, _ in configurations.values()),
        engines,
        task_sets,
        tuple(tasks),
        tuple(expected),
    )


def _check_hidden_run_dir(path: Path) -> None:
    try:
        check_hidden_results_dir(path)
    except (TaskError, OSError, RuntimeError):
        raise LedgerError(
            "results of hidden tasks go only to a path git ignores;"
            " use --runs with an ignored directory such as runs/"
        ) from None


def _check_reuse_stage(stage: str, reuse: Path | None) -> None:
    if reuse is not None and not can_reuse(stage):
        raise LedgerError(f"the {stage} stage requires fresh trials and cannot use --reuse")


def _reuse_trials(staging: Path, plan: Plan, source_dir: Path) -> None:
    try:
        source_dir = source_dir.resolve(strict=True)
        source = load_plan(source_dir)
    except (OSError, ValueError, RuntimeError):
        # An unreadable plan may belong to a hidden run, so its path and contents stay private.
        raise LedgerError("cannot read or validate the earlier run's frozen plan.json") from None
    if not RUN_ID.fullmatch(source_dir.name):
        raise LedgerError("the earlier run's directory name must be a valid run id")

    hidden = "hidden" in source.task_sets or "hidden" in plan.task_sets
    source_trials = {trial.id: trial for trial in source.trials}
    source_tasks = {task.id: task for task in source.tasks}
    unchanged_tasks = {task.id for task in plan.tasks if source_tasks.get(task.id) == task}
    unchanged_engines = {
        name
        for name, new in plan.engines.items()
        if (old := source.engines.get(name)) is not None
        and old.clis == new.clis
        and old.image.rsplit("@", 1)[1] == new.image.rsplit("@", 1)[1]
    }
    unchanged_configs = {
        config.id
        for config in plan.configurations
        if config.settings["engine"] in unchanged_engines
    }

    for trial in plan.trials:
        # The full configuration id and attempt must match, even though the path uses a prefix.
        # The configuration hash includes the engine version, which parse_plan checks against its pin.
        if not (
            source_trials.get(trial.id) == trial
            and trial.task in unchanged_tasks
            and trial.configuration in unchanged_configs
        ):
            continue
        previous = source_dir / TRIALS_DIR / trial.id
        try:
            status = read_status(previous)
        except LedgerError:
            continue
        if status.state != "done":
            continue
        try:
            findings, usage = read_results(previous)
            store_results(
                staging / TRIALS_DIR / trial.id, findings, usage, reused_from=source_dir.name
            )
        except (OSError, ValueError):
            if hidden:
                index = source.trials.index(trial) + 1
                raise LedgerError(
                    f"cannot reuse source trial {index} in a run with hidden tasks"
                ) from None
            raise


def create_run(runs_dir: Path, run_id: str, plan: Plan, *, reuse: Path | None = None) -> Path:
    """Create `runs_dir/run_id` with the plan, the lock file and all trial checkpoints.

    The directory is built under a temporary name and renamed into place, so a run directory
    either holds the whole plan and every trial, or does not exist. An existing run is refused.
    Plans with hidden tasks require Git to ignore both the staging and final directories.
    """
    runs_dir = Path(runs_dir)
    hidden = "hidden" in plan.task_sets
    _check_reuse_stage(plan.stage, reuse)
    if not RUN_ID.fullmatch(run_id):
        label = "bad run id" if hidden else f"bad run id {run_id!r}"
        raise LedgerError(f"{label}; use letters, digits, '.', '_' and '-'")
    run_dir = runs_dir / run_id
    tmp = None
    try:
        if hidden:
            _check_hidden_run_dir(run_dir)
        if run_dir.exists():
            label = "run" if hidden else f"run {run_dir}"
            raise LedgerError(f"{label} already exists; resume it or choose another run id")
        runs_dir.mkdir(parents=True, exist_ok=True)
        prefix = ".rbench." if hidden else f".{run_id}."
        tmp = Path(tempfile.mkdtemp(dir=runs_dir, prefix=prefix, suffix=".tmp"))
        if hidden:
            # An ignore rule for the final directory may leave its staging sibling visible.
            _check_hidden_run_dir(tmp)
        atomic_write(tmp / PLAN_FILE, dump_json(dataclasses.asdict(plan)))
        atomic_write(tmp / LOCK_FILE, b"")
        (tmp / TRIALS_DIR).mkdir(mode=0o700)
        for trial in plan.trials:
            trial_dir = tmp / TRIALS_DIR / trial.id
            trial_dir.mkdir(mode=0o700)
            (trial_dir / RAW_DIR).mkdir(mode=0o700)
            set_state(trial_dir, "pending")
        if reuse is not None:
            _reuse_trials(tmp, plan, reuse)
        # Fails if another runner created the same run meanwhile: the target is not empty.
        os.rename(tmp, run_dir)
    except BaseException as e:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        if hidden and isinstance(e, OSError):
            raise LedgerError(f"cannot create the run with hidden tasks: {e.strerror}") from None
        raise
    return run_dir


def load_plan(run_dir: Path) -> Plan:
    """Read and validate a run's frozen plan; refuse a plan written in another format."""
    path = Path(run_dir) / PLAN_FILE
    try:
        data = json.loads(path.read_bytes(), object_pairs_hook=no_repeated_keys)
    except (OSError, ValueError, RecursionError) as e:
        raise LedgerError(f"{path}: cannot read the plan: {e}") from e
    return parse_plan(data, str(path))


def checkout_path(matrix_path: Path, root: Path | None = None) -> str:
    """`matrix_path` relative to the rbench checkout, as plan.json stores it.

    A symlink is refused: the plan would record its target, and retargeting the link later
    would go unnoticed.
    """
    root = (root or CHECKOUT_ROOT).resolve()
    if Path(matrix_path).is_symlink():
        raise LedgerError(f"matrix {matrix_path} is a symlink; give the matrix file itself")
    try:
        return Path(matrix_path).resolve().relative_to(root).as_posix()
    except ValueError:
        raise LedgerError(
            f"matrix {matrix_path} is outside the rbench checkout {root}; a run records its"
            " matrix relative to the checkout, so keep the matrix file inside it"
        ) from None


def matrix_warning(plan: Plan, root: Path | None = None) -> str | None:
    """A warning when the plan's matrix file changed or is gone; the run keeps its plan.

    The path is resolved against the rbench checkout, whatever the current directory.
    """
    path = (root or CHECKOUT_ROOT) / plan.matrix.path
    try:
        changed = hashlib.sha256(path.read_bytes()).hexdigest() != plan.matrix.sha256
    except OSError:
        changed = True
    if not changed:
        return None
    return (
        f"warning: matrix file {plan.matrix.path} changed or is gone since the run started; the"
        " run continues with its frozen plan (a changed matrix needs a new run)"
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
    """Read `{"<engine>": {"image": "<name>@sha256:<hex>", "clis": {"<cli>": "<version>"}}}`.

    Every entry is checked, used by the round or not; a repeated key is refused.
    """
    try:
        data = json.loads(Path(path).read_bytes(), object_pairs_hook=no_repeated_keys)
    except (OSError, ValueError, RecursionError) as e:
        raise LedgerError(f"images file {path}: cannot read it: {e}") from e
    if not isinstance(data, dict):
        raise LedgerError(f"images file {path}: expected an object of engines")
    images = {}
    for name, raw in data.items():
        where = f"images file {path}: engine {name!r}"
        raw = _fields(raw, where, {"image", "clis"})
        image = Image(raw["image"], _object(raw["clis"], f"{where}: clis"))
        check_image(where, image)
        images[name] = image
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
    root: Path | None = None,
    reuse: Path | None = None,
) -> Path:
    """Load the inputs from disk, freeze the plan of one round and create its run directory.

    Every input is checked before anything is written. The default run id is the UTC start time,
    the stage and the round. `root` is the rbench checkout (default: this one).
    """
    stored_path = checkout_path(matrix_path, root)
    raw = Path(matrix_path).read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise MatrixError(f"{matrix_path}: not a valid UTF-8 TOML file: {e}") from e
    engines = load_engines(engines_dir)
    matrix = parse_matrix(data, str(matrix_path), engines)
    _check_reuse_stage(matrix.stage, reuse)
    hidden_set = None
    if matrix.tasks == "all":
        # The hidden-task API already reports validation failures and symlink loops as TaskError.
        try:
            hidden_set = load_hidden_task_set(hidden_config())
        except OSError as e:
            raise LedgerError(f"cannot read the hidden task set: {e.strerror}") from None
    tasks_root = Path(tasks_root)
    task_sets = [load_task_set(tasks_root / s, s) for s in PUBLIC_SETS if (tasks_root / s).is_dir()]
    if hidden_set is not None:
        task_sets.append(hidden_set)
    core = load_core_list(tasks_root / CORE_LIST_FILE) if matrix.tasks == "core" else None
    plan = freeze_plan(
        matrix,
        round_name,
        engines,
        task_sets,
        core,
        load_images(images_path),
        stored_path,
        hashlib.sha256(raw).hexdigest(),
    )
    if run_id is None:
        run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{matrix.stage}-{round_name}"
    return create_run(runs_dir, run_id, plan, reuse=reuse)
