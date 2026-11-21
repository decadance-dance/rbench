"""What the executor asks of an engine adapter (spec engine-configurations, Engine adapters).

An adapter is registered under its engine's name. For one trial the executor asks it, in order:
- `homes`: the shim settings by flag and the settings written into the trial's homes;
- `launch`: the one process to run in the reviewed repository (`rbench.engines.plain.Launch`:
  argv, stdin, files written before it, and where the final answer lands);
- `check`: what in the shim log shows the trial did not run the launch as built;
- `result`: the findings from the engine's output, or `EngineFailure` / `NotEligible`.

Everything an adapter reads in `Output` is engine output: untrusted, and never quoted in an error.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from rbench.engines import plain, rreview
from rbench.engines.declaration import Setting
from rbench.engines.pins import MODELS
from rbench.engines.plain import Launch
from rbench.ledger.plan import PlannedEngine


class EngineFailure(Exception):
    """The engine failed, or its output is not a review: the trial is `failed` with `reason`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NotEligible(EngineFailure):
    """The engine refused the PR (closed, a draft, already reviewed): `failed`, never `done`."""

    def __init__(self, why: str) -> None:
        super().__init__(f"PR not eligible: {why}")


@dataclass(frozen=True)
class EngineTrial:
    """One trial as an adapter sees it: the frozen configuration, its engine, and the PR."""

    settings: Mapping
    engine: PlannedEngine
    engines_dir: Path
    pr: int


@dataclass(frozen=True)
class Homes:
    """`by_flag`: per CLI, the model and effort the shim adds; the rest goes into the homes."""

    by_flag: dict[str, Setting] = field(default_factory=dict)
    codex_config: str | None = None
    claude_settings: dict | None = None


@dataclass(frozen=True)
class Output:
    """What the engine left: its exit status, its stdout, the answer file the launch names (None
    when it names none or the engine wrote none), and the shim log's records."""

    status: int
    stdout: str
    answer: str | None
    records: list[dict]


class Adapter:
    def homes(self, trial: EngineTrial) -> Homes:
        return Homes()

    def launch(self, trial: EngineTrial) -> Launch:
        raise NotImplementedError

    def check(self, trial: EngineTrial, launch: Launch, records: list[dict]) -> list[str]:
        return []

    def result(self, trial: EngineTrial, launch: Launch, output: Output) -> list[dict]:
        raise NotImplementedError


class Plain(Adapter):
    """`plain` (design D12): one reviewer session on its model's CLI, model and effort by flag."""

    def _reviewer(self, trial: EngineTrial) -> Setting:
        try:
            role = trial.settings["roles"][plain.ROLE]
            (model,) = role["models"]
            return Setting(model, role["effort"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("not the settings of a plain configuration") from None

    def homes(self, trial: EngineTrial) -> Homes:
        setting = self._reviewer(trial)
        return Homes(by_flag={MODELS[setting.model]: setting})

    def launch(self, trial: EngineTrial) -> Launch:
        return plain.launch(trial.engines_dir, trial.settings, trial.engine.clis)

    def check(self, trial: EngineTrial, launch: Launch, records: list[dict]) -> list[str]:
        return plain.check_launches(records, launch)

    def result(self, trial: EngineTrial, launch: Launch, output: Output) -> list[dict]:
        if output.status != 0:
            raise EngineFailure(f"engine error: {launch.cli} exited {output.status}")
        answer = output.stdout if launch.output is None else output.answer
        if answer is None:
            raise EngineFailure(f"invalid output: {launch.cli} wrote no final answer")
        path = Path(trial.engines_dir) / plain.NAME / plain.SCHEMA_FILE
        schema = json.loads(path.read_bytes())  # `launch` checked its hash against the plan
        try:
            return plain.findings(launch, answer, schema)
        except plain.PlainOutputError as e:
            raise EngineFailure(f"invalid output: {e}") from None


class Rreview(Adapter):
    """rreview (design D13): one `rreview` process that puts each role's model and effort on its
    own passes' command lines, so the shim adds none; its `result.json` comes on stdout."""

    def launch(self, trial: EngineTrial) -> Launch:
        return Launch(rreview.NAME, rreview.launch(trial.settings).argv, "", {}, None)

    def result(self, trial: EngineTrial, launch: Launch, output: Output) -> list[dict]:
        if output.status not in (rreview.EXIT_CLEAN, rreview.EXIT_GATED):
            raise EngineFailure(f"engine error: {launch.cli} exited {output.status}")
        try:
            return rreview.findings(output.stdout, output.status)
        except rreview.RreviewOutputError as e:
            raise EngineFailure(f"invalid output: {e}") from None


ADAPTERS: dict[str, Adapter] = {plain.NAME: Plain(), rreview.NAME: Rreview()}
