"""One whole trial attempt on a real Docker engine: docker only, no credentials, no model call.

Run: RBENCH_LIVE=1 uv run pytest -m live tests/test_executor_live.py -s
The engine is `claude --version` through the image's shim, so the real collector logs a launch and
its exit without a session or a request. The attempt reads that shim log back (`read_shim_log`),
audits the trial and records it done. It builds both images under tags of its own and removes
them, and the trial's compose project, by name.
"""

import subprocess
import uuid

import pytest
from test_repository import plumbed_archive

from rbench.engines.declaration import Setting
from rbench.engines.plain import Launch
from rbench.ledger import TRIALS_DIR, read_results, read_status, set_state
from rbench.ledger.plan import (
    MatrixFile,
    Pin,
    Plan,
    PlannedConfiguration,
    PlannedEngine,
    PlannedRole,
    PlannedRound,
    PlannedTask,
    PlannedTrial,
    run_identity,
)
from rbench.run.adapters import Adapter, EngineFailure
from rbench.run.trial import PROJECT_DIR, RunContext, project_name, run_attempt
from rbench.trial import build_agent_image, build_proxy_image, read_shim_log
from rbench.trial.compose import TrialProject

pytestmark = pytest.mark.live

CLIS = {"claude": "2.1.316"}
TAG = f"live-{uuid.uuid4().hex[:8]}"


class Version(Adapter):
    def launch(self, trial):
        return Launch("claude", ("claude", "--version"), "", {}, None)

    def result(self, trial, launch, output):
        if output.status != 0:
            raise EngineFailure(f"engine error: exited {output.status}")
        return []


def docker(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *argv], check=False, capture_output=True, text=True)


@pytest.fixture(scope="module")
def images(request):
    request.addfinalizer(lambda: docker("image", "rm", f"rbench-proxy:{TAG}"))
    proxy = build_proxy_image(tag=TAG)
    request.addfinalizer(lambda: docker("image", "rm", f"rbench-agent:{TAG}"))
    return build_agent_image(CLIS, tag=TAG), proxy


def test_a_trial_attempt_reads_the_real_collectors_shim_log(images, tmp_path, request):
    agent, proxy = images
    task, archive = plumbed_archive(tmp_path, {b"app.py": b"x = 1\n"}, {b"app.py": b"x = 2\n"})
    configuration = PlannedConfiguration(
        "version-1", ("claude",), {"engine": "version", "version": "1", "roles": {}}
    )
    trial = PlannedTrial("t-1--version--1", 1, task.id, configuration.id)
    engine = PlannedEngine(
        Pin("cli", "claude"),
        "1",
        CLIS,
        agent.ref,
        {"reviewer": PlannedRole("claude", "flag", Setting("claude-sonnet-5-5", "low"))},
    )
    plan = Plan(
        1,
        MatrixFile("m.toml", "0" * 64),
        "smoke",
        PlannedRound("r1", {"claude": 1}),
        1,
        (configuration,),
        {"version": engine},
        {"ours": 1},
        (PlannedTask(task.id, task.set, "0" * 64),),
        (trial,),
    )
    run_dir = tmp_path / "run"
    trial_dir = run_dir / TRIALS_DIR / trial.id
    (trial_dir / "raw").mkdir(parents=True)
    set_state(trial_dir, "pending")
    identity = run_identity(run_dir)
    name = project_name(identity, trial.id)
    request.addfinalizer(lambda: TrialProject(name, trial_dir / PROJECT_DIR).down())
    ctx = RunContext(
        run_dir,
        identity,
        plan,
        tmp_path,
        {task.id: task},
        (task,),
        archive.parent,
        proxy,
        {"version": Version()},
        lambda: "not-a-real-token",
    )
    # The archive is named by its hash in the archive directory.
    (archive.parent / f"{task.archive_sha256}.bundle").write_bytes(archive.read_bytes())

    state = run_attempt(ctx, trial)

    print(f"state: {state} {read_status(trial_dir)}")
    records = read_shim_log(trial_dir / "raw" / "shim.jsonl")
    print("shim log:", records)
    assert [r["kind"] for r in records] == ["launch", "child", "exit"]
    assert records[0]["argv"] == ["--version"] and records[-1]["status"] == 0
    assert state == "done"
    assert read_results(trial_dir)[0] == []
    leftovers = docker(
        "ps", "--all", "--quiet", "--filter", f"label=com.docker.compose.project={name}"
    )
    assert leftovers.stdout.strip() == ""
