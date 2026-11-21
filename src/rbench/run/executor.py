"""The trial executor (spec run-ledger: Balanced queue order, Failures and retries, Concurrency per
provider, Limit detection and stop; design D3, D8).

The queue is `recover_trials`' answer: the trials still to run, in plan order (attempt, then task,
then configuration). A trial starts once every provider it uses has a free slot: Claude has
`claude_concurrency` (default 2), Codex one per login (`rbench.run.trial.login_lock` holds it
across runners), and the executor starts the earliest queued trial that fits. Configurations with
one set of providers form a group, and a trial of the group starts only once no trial of an
earlier attempt in that group is still queued or running: whenever the run stops, even with a
trial stuck in its retries, every configuration of a group has completed the same number of
attempts, give or take one. Each group is balanced on its own.

An infrastructure error is retried up to `RETRIES` times. After that, and after any other error
the attempt cannot record, the trial goes back to `interrupted`, no new trial starts, running ones
finish, and `RunStopped` carries the error. An error after the trial's final state, such as a
project its attempt could not remove, keeps that state but stops the run the same way.

A trial a limit interrupted stops the run too: from the moment the attempt detects it (the
`limit` latch, set before the trial's project is removed), no new trial starts; running ones
finish, and `LimitReached` names the limits and any error that also stopped the run, which the
operator fixes before resuming after the reset.
"""

import os
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from rbench.ledger.plan import (
    PLAN_FILE,
    PUBLIC_SETS,
    TRIALS_DIR,
    LedgerError,
    Plan,
    PlannedTrial,
    run_identity,
)
from rbench.ledger.trials import read_status, read_wasted, set_state
from rbench.run.adapters import ADAPTERS, Adapter
from rbench.run.trial import (
    NORMALIZER_DIR,
    PROJECT_DIR,
    Latch,
    RunContext,
    label,
    normalizer_name,
    project_name,
    run_attempt,
)
from rbench.tasks.archive import ArchiveError
from rbench.tasks.hidden import HiddenConfig, hidden_config
from rbench.tasks.loader import Task
from rbench.tasks.manifest import load_hidden_task_set, load_task_set
from rbench.trial.compose import COMPOSE_LABEL, TrialProject
from rbench.trial.credentials import check_login_home, claude_token
from rbench.trial.images import TrialError, build_proxy_image, docker

CLAUDE_CONCURRENCY = 2
RETRIES = 3
INFRASTRUCTURE = (TrialError, ArchiveError)
CODEX_LOGIN_ENV = "RBENCH_CODEX_LOGIN"


class RunStopped(Exception):
    """The runner stopped on an error; running trials were let finish."""


class LimitReached(RunStopped):
    """A CLI reported a rate or usage limit; running trials were let finish. `errors` are the
    errors that also stopped the run."""

    def __init__(self, limits: Sequence[str], errors: Sequence[str] = ()) -> None:
        super().__init__("; ".join(limits))
        self.errors = list(errors)


class _Unclean(Exception):
    """The trial reached its final `state`, then its attempt failed, so the run stops."""

    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state


def slots(plan: Plan, trial: PlannedTrial) -> frozenset[str]:
    """The providers `trial` holds a slot of: its models', and Claude for a Markdown engine's
    normalization step."""
    configuration = next(c for c in plan.configurations if c.id == trial.configuration)
    engine = plan.engines[configuration.settings["engine"]]
    return frozenset(configuration.providers) | (
        {"claude"} if engine.output == "markdown" else set()
    )


def execute(
    run_dir: Path,
    plan: Plan,
    queue: Sequence[PlannedTrial],
    attempt: Callable[[PlannedTrial], str],
    *,
    claude_concurrency: int = CLAUDE_CONCURRENCY,
    retries: int = RETRIES,
    log: Callable[[str], None] = print,
    limit: Latch | None = None,
) -> dict[str, int]:
    """Run `queue` with `attempt` and return the count of each final state; raises `RunStopped`
    once running trials end when a trial could not be run, or `LimitReached` when a trial was
    interrupted by a limit. `attempt` sets `limit` when it detects one."""
    if claude_concurrency < 1:
        raise ValueError("the Claude concurrency must be at least 1")
    free = {"claude": claude_concurrency, "codex": 1}
    pending = list(queue)
    running: dict[Future, PlannedTrial] = {}
    counts: dict[str, int] = {}
    errors: list[str] = []
    limits: list[str] = []
    guard = threading.Lock()
    if limit is None:
        limit = Latch()

    def work(trial: PlannedTrial) -> str:
        name = label(plan, trial)
        trial_dir = run_dir / TRIALS_DIR / trial.id
        error, infrastructure = "", False
        for n in range(retries + 1):
            spent = len(read_wasted(trial_dir))
            try:
                return attempt(trial)
            except (*INFRASTRUCTURE, OSError, ValueError) as e:  # any other stops the run
                infrastructure = isinstance(e, INFRASTRUCTURE)
                error = (
                    f"infrastructure error: {e}" if infrastructure else f"{type(e).__name__}: {e}"
                )
            # An error after the final state, such as removing the project, never reruns the
            # trial, but what it left is unconfirmed: the run stops.
            # A limit's interrupted state is final too: its wasted tokens show this attempt set it.
            state = read_status(trial_dir).state
            if state not in ("pending", "running", "interrupted") or (
                state == "interrupted" and len(read_wasted(trial_dir)) > spent
            ):
                raise _Unclean(state, f"{name}: {state}, then {error}")
            if not infrastructure or n == retries:
                break
            with guard:
                log(f"{name}: {error}; retry {n + 1} of {retries}")
        if infrastructure:
            error += f" (after {retries} retries)"
        set_state(trial_dir, "interrupted", error)
        raise RunStopped(f"{name}: {error}")

    with ThreadPoolExecutor(max_workers=claude_concurrency + 1) as pool:
        while pending or running:
            if not errors and not limits and not limit.is_set():
                # The earliest attempt each provider group has queued or running.
                earliest: dict[frozenset[str], int] = {}
                for trial in [*pending, *running.values()]:
                    group = slots(plan, trial)
                    earliest[group] = min(earliest.get(group, trial.attempt), trial.attempt)
                for trial in list(pending):
                    need = slots(plan, trial)
                    if trial.attempt != earliest[need] or not all(free[p] > 0 for p in need):
                        continue
                    with limit.lock:  # a limit detected during the scan wins over this start
                        if limit.is_set():
                            break
                        for p in need:
                            free[p] -= 1
                        pending.remove(trial)
                        running[pool.submit(work, trial)] = trial
            if not running:
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                trial = running.pop(future)
                for p in slots(plan, trial):
                    free[p] += 1
                try:
                    state = future.result()
                except RunStopped as e:
                    errors.append(str(e))
                    continue
                except _Unclean as e:
                    counts[e.state] = counts.get(e.state, 0) + 1
                    if e.state == "interrupted":  # a limit, then its teardown failed
                        limits.append(
                            f"{label(plan, trial)}: interrupted: {_reason(run_dir, trial)}"
                        )
                    errors.append(str(e))
                    with guard:
                        log(str(e))
                    continue
                counts[state] = counts.get(state, 0) + 1
                message = f"{label(plan, trial)}: {state}"
                if state == "interrupted":
                    message += f": {_reason(run_dir, trial)}"
                    limits.append(message)
                with guard:
                    log(message)
    if limits:
        raise LimitReached(limits, errors)
    if errors:
        raise RunStopped("; ".join(errors))
    return counts


def _reason(run_dir: Path, trial: PlannedTrial) -> str | None:
    return read_status(run_dir / TRIALS_DIR / trial.id).reason


def load_tasks(
    plan: Plan, tasks_root: Path, hidden: HiddenConfig | None = None
) -> tuple[dict[str, Task], tuple[Task, ...]]:
    """The plan's tasks by id, and every task loaded as `start_run` loads them, whose canaries the
    audit looks for; refused when a planned set's version or a task's content hash is not the one
    the plan froze. `hidden` is the hidden directory, given when the plan has hidden tasks."""
    sets = [load_task_set(tasks_root / s, s) for s in PUBLIC_SETS if (tasks_root / s).is_dir()]
    if "hidden" in plan.task_sets:
        sets.append(load_hidden_task_set(hidden or hidden_config()))
    loaded, hashes = {}, {}
    for manifest, tasks in sets:
        if manifest.set in plan.task_sets and manifest.version != plan.task_sets[manifest.set]:
            raise LedgerError(
                f"task set {manifest.set!r} is at version {manifest.version}, but the run froze"
                f" version {plan.task_sets[manifest.set]}; finish the run with that version"
            )
        for task in tasks:
            loaded[task.id] = task
            hashes[task.id] = manifest.tasks[task.id].sha256
    for i, planned in enumerate(plan.tasks, 1):
        if hashes.get(planned.id) != planned.sha256:
            who = f"task {i} of the plan" if planned.set == "hidden" else f"task {planned.id!r}"
            raise LedgerError(f"{who} is missing or changed since the run started")
    return {t.id: loaded[t.id] for t in plan.tasks}, tuple(loaded.values())


def remove_leftovers(run_dir: Path, plan: Plan, proxy_image: str) -> list[str]:
    """Remove the compose projects this run's trials left on the Docker daemon, such as after a
    crash; call it holding the run lock. Returns the names removed."""
    identity = run_identity(run_dir)
    # Each trial's own project, and its normalization step's: name -> (trial, its directory).
    names = {
        name(identity, t.id): (t, directory)
        for t in plan.trials
        for name, directory in ((project_name, PROJECT_DIR), (normalizer_name, NORMALIZER_DIR))
    }
    found = set()
    for query in (["ps", "--all"], ["network", "ls"]):
        out = docker(
            *query,
            "--filter",
            f"label={COMPOSE_LABEL}",
            "--format",
            f'{{{{.Label "{COMPOSE_LABEL}"}}}}',
            what="listing compose projects",
        )
        found |= set(out.split()) & names.keys()
    configurations = {c.id: c for c in plan.configurations}
    for name in sorted(found):
        trial, directory = names[name]
        engine = plan.engines[configurations[trial.configuration].settings["engine"]]
        path = run_dir / TRIALS_DIR / trial.id / directory
        TrialProject.create(path, name, engine.image, proxy_image).down()
    return sorted(found)


def prepare_run(
    run_dir: Path,
    plan: Plan,
    *,
    engines_dir: Path,
    tasks_root: Path,
    archives: Path,
    adapters: Mapping[str, Adapter] = ADAPTERS,
) -> RunContext:
    """Everything the run's trials share, checked before any trial starts: an adapter for every
    engine, the frozen tasks, the credentials the run's providers need and the proxy image. Under
    the run lock, it also removes the run's leftover compose projects."""
    if not (run_dir / PLAN_FILE).is_file():
        raise LedgerError(f"{run_dir}: not a run directory")
    if missing := sorted(set(plan.engines) - set(adapters)):
        raise LedgerError(f"no adapter for engine {', '.join(missing)} yet; it cannot run")
    hidden = hidden_config() if "hidden" in plan.task_sets else None
    tasks, all_tasks = load_tasks(plan, tasks_root, hidden)
    providers = set().union(*(slots(plan, t) for t in plan.trials))
    codex_login = None
    if "codex" in providers:
        if not (home := os.environ.get(CODEX_LOGIN_ENV)):
            raise TrialError(
                f"the run uses Codex: set {CODEX_LOGIN_ENV} to the benchmark's login home"
            )
        codex_login = check_login_home(Path(home))
    token = claude_token() if "claude" in providers else None
    proxy_image = build_proxy_image()
    if removed := remove_leftovers(run_dir, plan, proxy_image):
        print(f"removed {len(removed)} compose projects an earlier runner left", file=sys.stderr)

    def token_of() -> str:
        if token is None:
            raise TrialError("no Claude token was read for this run")
        return token

    return RunContext(
        run_dir,
        run_identity(run_dir),
        plan,
        engines_dir,
        tasks,
        all_tasks,
        archives,
        proxy_image,
        adapters,
        token_of,
        codex_login,
        hidden,
    )


def run_trials(ctx: RunContext, queue: Sequence[PlannedTrial], **options) -> dict[str, int]:
    """`execute` the queue with `run_attempt` in `ctx`."""
    attempt = lambda t: run_attempt(ctx, t)
    return execute(ctx.run_dir, ctx.plan, queue, attempt, limit=ctx.limit, **options)
