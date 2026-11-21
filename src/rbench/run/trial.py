"""One attempt of one trial in a fresh sandbox (spec trial-isolation; spec run-ledger, Failures and
retries; design D3, D5, D7).

`run_attempt` holds the Codex login when the trial uses Codex, marks the trial `running`, starts
its compose project under the name `project_name` gives it, puts the task's repository in,
prepares the hermetic homes, runs the engine's launch under a time limit, copies out the shim log,
the transcripts and the proxy log, audits the trial while the agent still runs, records its final
state and removes the project.

The final state:
- `contaminated` when the audit says so, whatever the engine did;
- `failed` with reason `timeout` when the engine ran past its limit;
- `misconfigured` when the shim log shows the launch did not run as built;
- `failed` with the adapter's reason for an engine error, invalid output or a PR not eligible;
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
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from rbench.engines.plain import Launch
from rbench.ledger.plan import TRIALS_DIR, Image, Plan, PlannedConfiguration, PlannedTrial
from rbench.ledger.trials import RAW_DIR, set_state, store_results
from rbench.run.adapters import Adapter, EngineFailure, EngineTrial, Output
from rbench.tasks.archive import archive_name
from rbench.tasks.hidden import HiddenConfig
from rbench.tasks.loader import Task
from rbench.trial.audit import audit_trial, record_audit
from rbench.trial.compose import TrialProject
from rbench.trial.hermetic import LOG_FILE, TRANSCRIPTS_DIR, collect, prepare, shim_settings
from rbench.trial.images import TrialError
from rbench.trial.repository import REPO_DIR, agent_runner, prepare_repository
from rbench.trial.sessions import SessionError, read_shim_log
from rbench.trial.usage import UsageError, count_usage, usage_record

# limit: the spec sets no number; one hour per trial. Make it a matrix setting when an engine
# needs more.
TIMEOUT = 3600.0
# After the limit, `timeout` sends TERM to the engine's process group, then KILL this much later.
KILL_AFTER = 30
# How long past the limit and KILL_AFTER the host waits for `docker compose exec` to return.
HOST_GRACE = 60.0
DOWN_TRIES = 3
PROJECT_DIR = "project"  # the trial's compose file, outside raw/ so it outlives an attempt
STDOUT_FILE = "stdout"
STDERR_FILE = "stderr"
ANSWER_FILE = "answer"
PROXY_LOG_FILE = "proxy.log"
# Writes stdin to "$1", creating its directory, as the agent's user.
WRITE = 'mkdir -p "$(dirname "$1")" && cat > "$1"'
REGULAR = '[ -f "$1" ] && [ ! -L "$1" ]'
# Runs the engine in "$1" under `timeout -k "$2" "$3"` and ends stderr with its own line:
# `rbench-launch <status> <seconds>`, the launch's exit status and its time inside the agent. Only
# the last line counts, so an engine printing such a line earlier changes nothing; none means the
# launch never completed in the agent, a Docker failure.
MARK = "rbench-launch"
RUN = (
    'cd "$1" || exit; k=$2 l=$3; shift 3; t0=$(date +%s); timeout -k "$k" "$l" "$@"; s=$?;'
    f' printf "\\n{MARK} %s %s\\n" "$s" "$(($(date +%s) - t0))" >&2; exit "$s"'
)
_LAUNCHED = re.compile(rf"{MARK} (\d+) (\d+)")
# `timeout`'s status when the limit ended the engine: by TERM, or by KILL after KILL_AFTER.
TIMED_OUT = (124, 128 + 9)


def project_name(identity: str, trial_id: str) -> str:
    """The trial's compose project name: unique to the run (its persisted `identity`) and the
    trial on the Docker daemon, the same for every attempt and wherever the run directory moves,
    and free of task ids."""
    key = f"{identity}\0{trial_id}".encode()
    return f"rbench-{hashlib.sha256(key).hexdigest()[:16]}"


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
        return _attempt(ctx, trial)


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
        engine_trial = EngineTrial(configuration.settings, engine, ctx.engines_dir, task.pr)
        homes = adapter.homes(engine_trial)
        prepare(project, shim_settings(homes.by_flag), homes.codex_config, homes.claude_settings)
        launch = adapter.launch(engine_trial)
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
        if timed_out:
            set_state(trial_dir, "failed", "timeout")
            return "failed"
        if problems := adapter.check(engine_trial, launch, records):
            set_state(trial_dir, "misconfigured", "; ".join(problems))
            return "misconfigured"
        output = Output(status, _read(raw / STDOUT_FILE), answer, records, raw / TRANSCRIPTS_DIR)
        try:
            findings = adapter.result(engine_trial, launch, output)
            usage = count_usage(records, raw / TRANSCRIPTS_DIR, _codex_stdout(launch, records, raw))
            record = usage_record(usage)
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


def _archive(ctx: RunContext, task: Task) -> Path:
    """The task's source archive: a hidden task's in the hidden directory, next to the task, and
    a public task's in the archive directory (design D11)."""
    name = archive_name(task.archive_sha256)
    if task.set != "hidden":
        return ctx.archives / name
    if ctx.hidden is None:
        raise ValueError("the run has hidden tasks but no hidden directory")
    return ctx.hidden.inside(name)


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
    """Write the launch's files, run it in the repository under the time limit; return its exit
    status and whether it ran out of time. Its stdout and stderr land in `raw`."""
    for path, content in sorted(launch.files.items()):
        done = project.exec_agent(["sh", "-c", WRITE, "sh", path], stdin=content.decode())
        if done.returncode != 0:
            raise TrialError(f"trial project {project.name}: cannot write {path} in the agent")
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
    status, seconds = int(launched[1]), int(launched[2])
    # `date +%s` counts whole seconds, so a limit reached shows at least its whole part.
    return status, status in TIMED_OUT and seconds >= int(ctx.timeout)


def _collect(project: TrialProject, image: Image, raw: Path) -> list[dict]:
    """Copy the shim log and the transcripts out; the shim log's records, or none when it cannot
    be read, which the audit then reports as contamination."""
    try:
        collect(project, image, raw)
    except SessionError:
        pass  # the transcripts are already copied; the audit fails the unreadable log closed
    try:
        return read_shim_log(raw / LOG_FILE)
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
