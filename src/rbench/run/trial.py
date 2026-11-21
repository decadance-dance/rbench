"""One attempt of one trial in a fresh sandbox (spec trial-isolation; spec run-ledger, Failures and
retries; design D3, D5, D7).

`run_attempt` holds the Codex login when the trial uses Codex, marks the trial `running`, starts
its compose project under the name `project_name` gives it, puts the task's repository in,
prepares the hermetic homes, writes the launch's files between the adapter's checkout hooks, runs
the adapter's setup commands and then the engine's launch under a time limit, copies out the shim
log, the transcripts and the proxy log, audits the trial while the agent still runs, records its
final state and removes the project.

A Markdown engine's report, once it is valid UTF-8, then goes through the pinned normalization
step (design D9) in a second, fresh compose project (`normalizer_name`) of the engine's image,
with no repository, under its own time limit. Its shim log, transcripts, proxy log and findings
land in `raw/normalizer/`, its record in `raw/normalization.json`, and its tokens, from the step's
`modelUsage`, in `usage.json` under `normalization`. The trial is then audited again, with the
step's proxy log and sessions, while the engine's agent still runs: the canary scan covers the
whole trial directory, the step's findings included.

The final state:
- `misconfigured` when a checkout hook gives a reason, before the engine starts;
- `contaminated` when the audit says so, whatever the engine did;
- `interrupted` when a CLI reported a rate or usage limit (`rbench.trial.limits`), with that
  attempt's usage added to the trial's wasted tokens, whatever the engine did next;
- `failed` with reason `timeout` when the engine ran past its limit;
- `misconfigured` when the shim log shows the launch did not run as built;
- `failed` with the adapter's reason for an engine error, invalid output or a PR not eligible,
  or because the normalization step failed or ran past its limit (never retried: that would
  rerun the engine);
- `contaminated` when the audit after the normalization step says so;
- `interrupted` when the normalization step's own CLI reported a limit, with the engine's and the
  step's usage as wasted tokens, as for the engine;
- `done` with the findings and the usage otherwise.

An infrastructure error (`TrialError`, an archive that fails its check, a launch that did not
complete in the agent) records nothing final: it propagates, and the executor retries the attempt
from the start. Reasons recorded in the ledger never quote the engine's output.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from rbench.atomic import atomic_write
from rbench.engines.normalization import Normalization, NormalizationError, normalize
from rbench.engines.plain import Launch
from rbench.ledger.plan import (
    TRIALS_DIR,
    Image,
    Plan,
    PlannedConfiguration,
    PlannedEngine,
    PlannedTrial,
)
from rbench.ledger.trials import RAW_DIR, add_wasted, dump_json, set_state, store_results
from rbench.run.adapters import Adapter, EngineFailure, EngineTrial, Output
from rbench.tasks.archive import archive_name
from rbench.tasks.hidden import HiddenConfig
from rbench.tasks.loader import Task
from rbench.trial.audit import Audit, audit_trial, record_audit
from rbench.trial.compose import COMPOSE_LABEL, LOGIN_LABEL, TrialProject, login_key
from rbench.trial.hermetic import LOG_FILE, TRANSCRIPTS_DIR, collect, prepare, shim_settings
from rbench.trial.images import TrialError, docker
from rbench.trial.limits import detect_limits
from rbench.trial.repository import (
    REPO_DIR,
    RepositoryState,
    Runner,
    agent_runner,
    prepare_repository,
)
from rbench.trial.sessions import SessionError, read_shim_log
from rbench.trial.usage import Usage, UsageError, count_usage, usage_record

# limit: the spec sets no number; one hour per trial. Make it a matrix setting when an engine
# needs more.
TIMEOUT = 3600.0
# limit: the spec sets no number; the normalizer only copies one report, with no tools.
NORMALIZE_TIMEOUT = 900.0
# After the limit, `timeout` sends TERM to the engine's process group, then KILL this much later.
KILL_AFTER = 30
# How long past the limit and KILL_AFTER the host waits for `docker compose exec` to return.
HOST_GRACE = 60.0
DOWN_TRIES = 3
PROJECT_DIR = "project"  # the trial's compose file, outside raw/ so it outlives an attempt
NORMALIZER_DIR = "normalizer"  # the normalization step's compose file, and its output in raw/
STDOUT_FILE = "stdout"
STDERR_FILE = "stderr"
ANSWER_FILE = "answer"
FINDINGS_FILE = "findings.json"  # the normalizer's findings, in raw/normalizer/ for the audit
PROXY_LOG_FILE = "proxy.log"
# Writes stdin to "$1", creating its directory, as the agent's user.
WRITE = 'mkdir -p "$(dirname "$1")" && cat > "$1"'
REGULAR = '[ -f "$1" ] && [ ! -L "$1" ]'
# Runs the engine in "$1" under `timeout -v -k "$2" "$3"` and ends stderr with its own line,
# `rbench-launch <outcome> <status>`. `timeout`'s own stderr goes to a file, the engine's to the
# real stderr, so the outcome says what happened rather than guessing from the status:
# - `timeout`: `timeout` reported sending a signal, so the timer fired;
# - `start`: the command is not an executable on PATH, or `timeout` reported failing to run it;
# - `exit`: the engine ran and exited with that status, 124 included.
# Only the last line counts, so an engine printing such a line earlier changes nothing; none means
# the launch never completed in the agent, a Docker failure.
MARK = "rbench-launch"
RUN = (
    'cd "$1" || exit; k=$2 l=$3; shift 3; m=' + MARK + ";"
    ' p=$(command -v "$1") && [ -x "$p" ] || { printf "\\n%s start 127\\n" "$m" >&2; exit 127; };'
    " f=$(mktemp) || exit;"
    """ timeout -v -k "$k" "$l" sh -c 'exec 2>&3 3>&-; exec "$@"' sh "$@" 3>&2 2>"$f"; s=$?;"""
    ' if [ ! -s "$f" ]; then o=exit; elif [ "$s" = 124 ] || [ "$s" = 137 ]; then o=timeout;'
    ' else o=start; fi; rm -f "$f"; printf "\\n%s %s %s\\n" "$m" "$o" "$s" >&2; exit "$s"'
)
_LAUNCHED = re.compile(rf"{MARK} (exit|timeout|start) (\d+)")


def project_name(identity: str, trial_id: str) -> str:
    """The trial's compose project name: unique to the run (its persisted `identity`) and the
    trial on the Docker daemon, the same for every attempt and wherever the run directory moves,
    and free of task ids."""
    key = f"{identity}\0{trial_id}".encode()
    return f"rbench-{hashlib.sha256(key).hexdigest()[:16]}"


def normalizer_name(identity: str, trial_id: str) -> str:
    """The compose project name of the trial's normalization step, like `project_name`."""
    return project_name(identity, f"{trial_id}/{NORMALIZER_DIR}")


def label(plan: Plan, trial: PlannedTrial) -> str:
    """How messages name `trial`: by its position in a run with hidden tasks, whose ids are
    hidden (spec review-tasks)."""
    if "hidden" in plan.task_sets:
        return f"trial {plan.trials.index(trial) + 1}"
    return f"trial {trial.id}"


@contextmanager
def login_lock(home: Path) -> Iterator[None]:
    """Hold the Codex login home `home` exclusively, against every runner on this host: two trials
    on one login would race its token refresh (design D5). Waits for the holder to finish."""
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # releases the lock


class Latch:
    """Set once a trial detects a limit. The executor holds `lock` while it decides to start a
    trial and starts it, and `set` takes it too, so every trial starts either before the latch is
    set or not at all."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._set = False

    def set(self) -> None:
        with self.lock:
            self._set = True

    def is_set(self) -> bool:
        return self._set


@dataclass
class RunContext:
    """What every trial of a run shares. `tasks` maps the plan's task ids to loaded tasks, and
    `all_tasks` is every loaded task, whose canaries the audit looks for."""

    run_dir: Path
    identity: str
    plan: Plan
    engines_dir: Path
    tasks: Mapping[str, Task]
    all_tasks: tuple[Task, ...]
    archives: Path
    proxy_image: str
    adapters: Mapping[str, Adapter]
    claude_token: Callable[[], str]
    codex_login: Path | None = None
    hidden: HiddenConfig | None = None
    timeout: float = TIMEOUT
    # Set as soon as a trial detects a limit, before its project is removed: the executor starts
    # no trial after it.
    limit: Latch = field(default_factory=Latch)
    configurations: dict[str, PlannedConfiguration] = field(init=False)

    def __post_init__(self) -> None:
        self.configurations = {c.id: c for c in self.plan.configurations}


def run_attempt(ctx: RunContext, trial: PlannedTrial) -> str:
    """Run one attempt of `trial` and record its final state, which it returns."""
    if "codex" not in ctx.configurations[trial.configuration].providers:
        return _attempt(ctx, trial)
    if ctx.codex_login is None:
        raise TrialError("the trial uses Codex, but the run has no Codex login")
    with login_lock(ctx.codex_login):
        _check_login_free(ctx.codex_login, project_name(ctx.identity, trial.id))
        return _attempt(ctx, trial)


def _check_login_free(login: Path, own: str) -> None:
    """Refuse to start while another project's running agent mounts the Codex login: its runner
    may have died and released the lock, or failed to remove it, but its session still holds the
    login. That project is its run's to remove, so it is named, not touched."""
    out = docker(
        "ps",
        "--filter",
        f"label={LOGIN_LABEL}={login_key(login)}",
        "--format",
        f'{{{{.Label "{COMPOSE_LABEL}"}}}}',
        what="listing agents on the Codex login",
    )
    if others := sorted(set(out.split()) - {own}):
        raise ValueError(
            f"Codex login home {login} is still mounted by the running agent of compose project"
            f" {', '.join(others)}; remove that project (resume its run, or `docker compose"
            " --project-name <name> down`) before starting another trial on the login"
        )


def _attempt(ctx: RunContext, trial: PlannedTrial) -> str:
    trial_dir = ctx.run_dir / TRIALS_DIR / trial.id
    raw = trial_dir / RAW_DIR
    configuration = ctx.configurations[trial.configuration]
    name = configuration.settings["engine"]
    engine = ctx.plan.engines[name]
    adapter = ctx.adapters[name]
    task = ctx.tasks[trial.task]
    set_state(trial_dir, "running")
    # An attempt starts from nothing: an earlier attempt's output must not reach this one's audit.
    shutil.rmtree(raw, ignore_errors=True)
    raw.mkdir(mode=0o700)
    for stale in ("audit.json", "repository.json"):
        (trial_dir / stale).unlink(missing_ok=True)

    clis = set(configuration.providers)
    project = TrialProject.create(
        trial_dir / PROJECT_DIR,
        project_name(ctx.identity, trial.id),
        engine.image,
        ctx.proxy_image,
        claude_token=ctx.claude_token() if "claude" in clis else None,
        codex_login=ctx.codex_login if "codex" in clis else None,
    )
    try:
        project.up()
        repository = prepare_repository(project, task, _archive(ctx, task), trial_dir)
        engine_trial = EngineTrial(
            configuration.settings, engine, ctx.engines_dir, task.pr, label(ctx.plan, trial)
        )
        homes = adapter.homes(engine_trial)
        prepare(project, shim_settings(homes.by_flag), homes.codex_config, homes.claude_settings)
        launch = adapter.launch(engine_trial)
        if problems := _checkout(adapter, project, launch):
            set_state(trial_dir, "misconfigured", "; ".join(problems))
            return "misconfigured"
        _setup(project, adapter.setup(engine_trial))
        status, timed_out = _run_engine(ctx, project, launch, raw)
        answer = _copy_answer(project, launch, raw)
        records = _collect(project, Image(engine.image, engine.clis), raw)
        project.copy_proxy_log(raw / PROXY_LOG_FILE)
        audit = audit_trial(
            trial_dir,
            ctx.all_tasks,
            proxy_log=raw / PROXY_LOG_FILE,
            shim_log=raw / LOG_FILE,
            transcripts=raw / TRANSCRIPTS_DIR,
            repository=repository,
            run=agent_runner(project, "the trial's agent"),
        )
        record_audit(trial_dir, audit)
        if audit.contaminated:
            return "contaminated"
        codex_stdout = _codex_stdout(launch, records, raw)
        if limits := detect_limits(records, raw / TRANSCRIPTS_DIR, codex_stdout.values()):
            ctx.limit.set()
            # Wasted first: a runner killed in between leaves the trial running, which resume
            # reruns, rather than an interrupted trial whose tokens are lost.
            add_wasted(trial_dir, _wasted(records, raw, codex_stdout))
            set_state(trial_dir, "interrupted", "; ".join(map(str, limits)))
            return "interrupted"
        if timed_out:
            set_state(trial_dir, "failed", "timeout")
            return "failed"
        if problems := adapter.check(engine_trial, launch, records):
            set_state(trial_dir, "misconfigured", "; ".join(problems))
            return "misconfigured"
        output = Output(status, _read(raw / STDOUT_FILE), answer, records, raw / TRANSCRIPTS_DIR)
        try:
            normalized = Usage({}, [])
            if engine.output == "markdown":
                report = adapter.report(engine_trial, launch, output)
                try:
                    report.encode("utf-8")
                except UnicodeEncodeError:  # a lone surrogate a JSON escape gave
                    raise EngineFailure("invalid output: the report is not valid UTF-8") from None
                step = _Step(
                    ctx, trial, engine, trial_dir, audit, repository, records, codex_stdout
                )
                normalization = step.run(report, agent_runner(project, "the trial's agent"))
                if isinstance(normalization, str):
                    return normalization  # its final state is recorded
                findings, normalized = normalization.findings, Usage(normalization.usage, [])
            else:
                findings = adapter.result(engine_trial, launch, output)
            usage = count_usage(records, raw / TRANSCRIPTS_DIR, codex_stdout)
            record = usage_record(usage, normalized)
            _check_unicode(findings, record)
        except EngineFailure as e:
            set_state(trial_dir, "failed", e.reason)
            return "failed"
        except UsageError:
            # Its message quotes transcript paths, ids and models, all engine output.
            set_state(trial_dir, "failed", "invalid output: the transcripts' usage is malformed")
            return "failed"
        store_results(trial_dir, findings, record)
        return "done"
    finally:
        _down(project)


def _checkout(adapter: Adapter, project: TrialProject, launch: Launch) -> list[str]:
    """Write the launch's files between the adapter's checkout hooks, each called only when the
    adapter defines it (design D13); the reasons that make the trial `misconfigured`, after which
    nothing more is written."""
    run = agent_runner(project, "the trial's agent")
    if (check := getattr(adapter, "check_checkout", None)) and (problems := check(run)):
        return problems
    for path, content in sorted(launch.files.items()):
        done = project.exec_agent(["sh", "-c", WRITE, "sh", path], stdin=content.decode())
        if done.returncode != 0:
            raise TrialError(f"trial project {project.name}: cannot write {path} in the agent")
    prepare_checkout = getattr(adapter, "prepare_checkout", None)
    return prepare_checkout(run) if prepare_checkout else []


def _setup(project: TrialProject, commands: tuple[tuple[str, ...], ...]) -> None:
    """Run the adapter's setup commands in the agent. They install what the image holds, offline,
    so a failure is the image's: an infrastructure error, which never quotes their output."""
    for n, argv in enumerate(commands, 1):
        done = project.exec_agent(list(argv))
        if done.returncode != 0:
            raise TrialError(
                f"trial project {project.name}: setup command {n} ({argv[0]}) exited"
                f" {done.returncode} in the agent"
            )


@dataclass
class _Step:
    """The normalization step of one trial (design D9), after the engine's audit."""

    ctx: RunContext
    trial: PlannedTrial
    engine: PlannedEngine
    trial_dir: Path
    engine_audit: Audit
    repository: RepositoryState
    records: list[dict]  # the engine's shim log, for the wasted tokens
    codex_stdout: dict[int, Path]

    def run(self, report: str, run: Runner) -> Normalization | str:
        """Copy the findings of the engine's Markdown `report` in a fresh normalizer project,
        then audit the trial again with the step's evidence (`run`: the engine's agent, for the
        repository's object count). Returns the normalization, or the final state it recorded:
        `failed` when the step failed or ran past its limit, `contaminated` by the audit,
        `interrupted` when the step's CLI reported a limit."""
        if not report.strip():
            return Normalization([], [], {})  # no model runs (design D9)
        ctx, trial, engine = self.ctx, self.trial, self.engine
        out = self.trial_dir / RAW_DIR / NORMALIZER_DIR
        project = TrialProject.create(
            self.trial_dir / NORMALIZER_DIR,
            normalizer_name(ctx.identity, trial.id),
            engine.image,
            ctx.proxy_image,
            claude_token=ctx.claude_token(),
        )
        try:
            try:
                project.up()
                who = label(ctx.plan, trial).removeprefix("trial ")
                normalization = normalize(project, report, who, timeout=NORMALIZE_TIMEOUT)
            except (NormalizationError, TrialError) as e:
                try:
                    step_records = self._evidence(project, out)
                except (TrialError, OSError):
                    # The step failed; its evidence is kept as far as it was copied.
                    step_records = _shim_records(out / LOG_FILE)
                if self._interrupted(step_records, out):
                    return "interrupted"
                # Never retried: a retry would rerun the engine.
                set_state(self.trial_dir, "failed", f"normalization failed: {e}")
                return "failed"
            step_records = self._evidence(project, out)
            normalization.write_record(self.trial_dir / RAW_DIR)
            atomic_write(out / FINDINGS_FILE, dump_json(normalization.findings))
            audit = audit_trial(
                self.trial_dir,
                ctx.all_tasks,
                proxy_log=out / PROXY_LOG_FILE,
                shim_log=out / LOG_FILE,
                transcripts=out / TRANSCRIPTS_DIR,
                repository=self.repository,
                run=run,
            )
            first = self.engine_audit
            record_audit(
                self.trial_dir,
                Audit(
                    first.reasons + audit.reasons,
                    first.refused + audit.refused,
                    first.refused_omitted + audit.refused_omitted,
                ),
            )
            if audit.contaminated:
                return "contaminated"
            if self._interrupted(step_records, out):
                return "interrupted"
            return normalization
        finally:
            _down(project)

    def _evidence(self, project: TrialProject, out: Path) -> list[dict]:
        """Copy the step's evidence into `out`; its shim log's records."""
        records = _collect(project, Image(self.engine.image, self.engine.clis), out)
        project.copy_proxy_log(out / PROXY_LOG_FILE)
        return records

    def _interrupted(self, step_records: list[dict], out: Path) -> bool:
        """Record the trial `interrupted` when the step's own sessions, as its shim log binds
        them, report a limit (`detect_limits`): the latch first, so nothing new starts, then
        the engine's and the step's usage as wasted tokens."""
        if not (limits := detect_limits(step_records, out / TRANSCRIPTS_DIR)):
            return False
        self.ctx.limit.set()
        raw = self.trial_dir / RAW_DIR
        add_wasted(self.trial_dir, _wasted(self.records, raw, self.codex_stdout, step_records))
        reasons = "; ".join(f"normalization: {limit}" for limit in limits)
        set_state(self.trial_dir, "interrupted", reasons)
        return True


def _archive(ctx: RunContext, task: Task) -> Path:
    """The task's source archive: a hidden task's in the hidden directory, next to the task, and
    a public task's in the archive directory (design D11)."""
    name = archive_name(task.archive_sha256)
    if task.set != "hidden":
        return ctx.archives / name
    if ctx.hidden is None:
        raise ValueError("the run has hidden tasks but no hidden directory")
    return ctx.hidden.inside(name)


def _wasted(
    records: list[dict],
    raw: Path,
    codex_stdout: dict[int, Path],
    step: list[dict] | None = None,
) -> dict:
    """The usage of an attempt a limit interrupted: the engine's, and with `step` (the
    normalization step's shim log) the step's, counted from its transcripts since it gave no
    result; each flagged unknown when it cannot be counted."""
    unknown = Usage({}, ["the transcripts' usage is malformed, so it is unknown"])

    def count(records: list[dict], transcripts: Path, stdout: dict[int, Path]) -> Usage:
        try:
            return count_usage(records, transcripts, stdout)
        except UsageError:  # its message quotes transcript paths, ids and models
            return unknown

    engine = count(records, raw / TRANSCRIPTS_DIR, codex_stdout)
    normalizer = Usage({}, [])
    if step is not None:
        normalizer = count(step, raw / NORMALIZER_DIR / TRANSCRIPTS_DIR, {})
    record = usage_record(engine, normalizer)
    try:
        _check_unicode([], record)
        return record
    except EngineFailure:
        return usage_record(unknown, normalizer if step is None else unknown)


def _check_unicode(findings: list[dict], usage: dict) -> None:
    """Refuse output the ledger cannot store as UTF-8, such as a lone surrogate a JSON escape
    gave, as invalid output rather than a ledger write that stops the run."""
    try:
        json.dumps([findings, usage], ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise EngineFailure("invalid output: a string in it is not valid Unicode") from None


def _down(project: TrialProject) -> None:
    """Remove the project, trying `DOWN_TRIES` times; the last failure propagates."""
    for n in range(1, DOWN_TRIES + 1):
        try:
            project.down()
            return
        except TrialError:
            if n == DOWN_TRIES:
                raise


def _run_engine(
    ctx: RunContext, project: TrialProject, launch: Launch, raw: Path
) -> tuple[int, bool]:
    """Run the launch in the repository under the time limit; return its exit status and whether
    it ran out of time. Its stdout and stderr land in `raw`."""
    argv = ["sh", "-c", RUN, "sh", REPO_DIR, f"{KILL_AFTER}s", f"{ctx.timeout:g}s", *launch.argv]
    wait = ctx.timeout + KILL_AFTER + HOST_GRACE
    done = project.exec_agent(argv, stdin=launch.stdin, timeout=wait)
    (raw / STDOUT_FILE).write_text(done.stdout, encoding="utf-8")
    (raw / STDERR_FILE).write_text(done.stderr, encoding="utf-8")
    last = done.stderr.rstrip("\n").rpartition("\n")[2]
    if not (launched := _LAUNCHED.fullmatch(last)):
        # Docker's own error, or the agent's: nothing of the engine is quoted.
        raise TrialError(
            f"trial project {project.name}: the engine's launch did not complete in the agent"
            f" (docker exec exited {done.returncode})"
        )
    outcome, status = launched[1], int(launched[2])
    if outcome == "start":
        raise TrialError(
            f"trial project {project.name}: the engine's launch could not start in the agent"
            f" (exit {status})"
        )
    return status, outcome == "timeout"


def _collect(project: TrialProject, image: Image, raw: Path) -> list[dict]:
    """Copy the shim log and the transcripts out; the shim log's records, or none when it cannot
    be read, which the audit then reports as contamination."""
    try:
        collect(project, image, raw)
    except SessionError:
        pass  # the transcripts are already copied; the audit fails the unreadable log closed
    return _shim_records(raw / LOG_FILE)


def _shim_records(log: Path) -> list[dict]:
    """The records of a copied shim log; none when it cannot be read."""
    try:
        return read_shim_log(log)
    except (OSError, ValueError):
        return []


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _copy_answer(project: TrialProject, launch: Launch, raw: Path) -> str | None:
    """The final answer the launch names, copied out of the agent; None when the engine wrote no
    regular file there. A link the engine planted is not followed."""
    if launch.output is None:
        return None
    if project.exec_agent(["sh", "-c", REGULAR, "sh", launch.output]).returncode != 0:
        return None
    dest = raw / ANSWER_FILE
    project.copy_from_agent(launch.output, dest)
    if dest.is_symlink() or not dest.is_file():
        return None
    return _read(dest)


def _codex_stdout(launch: Launch, records: list[dict], raw: Path) -> dict[int, Path]:
    """The engine's stdout as the stdout of its shim launch, when the engine is that one
    `codex exec --json` launch; a Codex launch an engine makes itself has stdout the runner never
    sees (design D5), and one without `--json` prints no events `count_usage` can read (D7)."""
    options = launch.argv[: launch.argv.index("--")] if "--" in launch.argv else launch.argv
    if options[:2] != ("codex", "exec") or "--json" not in options:
        return {}
    own = [
        r["launch"]
        for r in records
        if r["kind"] == "launch" and r["cli"] == "codex" and r["argv"] == list(launch.argv[1:])
    ]
    return {own[0]: raw / STDOUT_FILE} if len(own) == 1 else {}
