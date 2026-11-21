"""What the executor asks of an engine adapter (spec engine-configurations, Engine adapters).

An adapter is registered under its engine's name. For one trial the executor asks it, in order:
- `homes`: the shim settings by flag and the settings written into the trial's homes;
- `launch`: the one process to run in the reviewed repository (`rbench.engines.plain.Launch`:
  argv, stdin, files written before it, and where the final answer lands);
- `check_checkout` and `prepare_checkout`, only when the adapter defines them (design D13,
  Adapter checkout hooks): run in the agent before and after `Launch.files` are written, each
  returns the reasons that make the trial `misconfigured` before the engine starts;
- `setup`: commands run in the agent before the launch, such as a plugin install;
- `check`: what in the shim log shows the trial did not run the launch as built;
- `result`: the findings from a structured engine's output, or, for an engine whose declaration
  says `output = "markdown"`, `report`: its Markdown report, which the executor sends through the
  pinned normalization step (design D9). Either raises `EngineFailure` / `NotEligible`.

Everything an adapter reads in `Output` is engine output: untrusted, and never quoted in an error.

`NotEligible` is the path for an engine that refuses a PR (spec run-ledger, PR not eligible). No
registered adapter raises it: on a scrubbed task no engine can see an ineligible PR. `plain` and
`codex-review` review whatever diff they get, rreview's exits are clean, gated or failed, and the
engines that read the PR through `gh` (`code-review`, `review-council`, `ponytail-review`) get the
trial's stub, which reports every PR open, not a draft, with no reviews (design D4). An adapter
whose engine does report one raises it from `result` or `report`.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from rbench.engines import (
    code_review,
    codex_review,
    plain,
    ponytail_review,
    review_council,
    rreview,
)
from rbench.engines.declaration import Setting
from rbench.engines.pins import MODELS
from rbench.engines.plain import Launch
from rbench.ledger.plan import PlannedEngine
from rbench.trial.repository import Runner


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
    """One trial as an adapter sees it: the frozen configuration, its engine, the PR, and how
    errors name the trial (`rbench.run.trial.label`: its position in a run with hidden tasks)."""

    settings: Mapping
    engine: PlannedEngine
    engines_dir: Path
    pr: int
    label: str


@dataclass(frozen=True)
class Homes:
    """`by_flag`: per CLI, the model and effort the shim adds; the rest goes into the homes."""

    by_flag: dict[str, Setting] = field(default_factory=dict)
    codex_config: str | None = None
    claude_settings: dict | None = None


@dataclass(frozen=True)
class Output:
    """What the engine left: its exit status, its stdout, the answer file the launch names (None
    when it names none or the engine wrote none), the shim log's records, and the directory of
    the collected transcripts (at their container paths)."""

    status: int
    stdout: str
    answer: str | None
    records: list[dict]
    transcripts: Path


class Adapter:
    def homes(self, trial: EngineTrial) -> Homes:
        return Homes()

    def launch(self, trial: EngineTrial) -> Launch:
        raise NotImplementedError

    def setup(self, trial: EngineTrial) -> tuple[tuple[str, ...], ...]:
        return ()

    def check(self, trial: EngineTrial, launch: Launch, records: list[dict]) -> list[str]:
        return []

    def result(self, trial: EngineTrial, launch: Launch, output: Output) -> list[dict]:
        raise NotImplementedError

    def report(self, trial: EngineTrial, launch: Launch, output: Output) -> str:
        raise NotImplementedError


def _exited(launch: Launch, output: Output) -> None:
    if output.status != 0:
        raise EngineFailure(f"engine error: {launch.cli} exited {output.status}")


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
        _exited(launch, output)
        answer = output.stdout if launch.output is None else output.answer
        if answer is None:
            raise EngineFailure(f"invalid output: {launch.cli} wrote no final answer")
        # The schema the trial launched with, whose hash `launch` checked against the plan; the
        # file may have changed since.
        if launch.cli == "claude":
            schema = json.loads(launch.argv[launch.argv.index("--json-schema") + 1])
        else:
            schema = json.loads(launch.files[plain.SCHEMA_PATH])
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


class CodexReview(Adapter):
    """`codex-review` (design D13): `codex exec review`, model and effort on its own argv; the
    findings are the `review_output` in the root thread's rollout."""

    def launch(self, trial: EngineTrial) -> Launch:
        return codex_review.launch(trial.settings)

    def check(self, trial: EngineTrial, launch: Launch, records: list[dict]) -> list[str]:
        return codex_review.check_launches(records, launch)

    def result(self, trial: EngineTrial, launch: Launch, output: Output) -> list[dict]:
        _exited(launch, output)
        where = codex_review.NAME
        try:
            review = codex_review.review_output(output.records, output.transcripts, where)
            return codex_review.findings(review, where)
        except codex_review.ReviewOutputError as e:
            raise EngineFailure(f"invalid output: {e}") from None


class CodeReview(Adapter):
    """`code-review` (design D13): `/code-review <PR>` on stdin to one Claude session, model and
    effort by flag; the report is the result text of its JSON output."""

    def homes(self, trial: EngineTrial) -> Homes:
        return Homes(by_flag={"claude": code_review.requested(trial.settings)})

    def launch(self, trial: EngineTrial) -> Launch:
        return code_review.launch(trial.pr)

    def report(self, trial: EngineTrial, launch: Launch, output: Output) -> str:
        _exited(launch, output)
        try:
            return code_review.report(output.stdout, f"{trial.label}: {code_review.NAME}")
        except code_review.CodeReviewOutputError as e:
            raise EngineFailure(f"invalid output: {e}") from None


class ReviewCouncil(Adapter):
    """review-council (design D13): one Claude session, the Claude side by flag and the Codex side
    by the trial's config.toml, with its `.review-council/` overlay in the checkout."""

    def homes(self, trial: EngineTrial) -> Homes:
        want = review_council.requested(trial.settings)
        _, config = review_council.trial_homes(trial.settings)
        return Homes(by_flag={"claude": want["claude"]}, codex_config=config)

    def launch(self, trial: EngineTrial) -> Launch:
        return review_council.launch(trial.engines_dir, trial.settings)

    def check_checkout(self, run: Runner) -> list[str]:
        return review_council.check_checkout(run)

    def prepare_checkout(self, run: Runner) -> list[str]:
        return review_council.prepare_checkout(run)

    def check(self, trial: EngineTrial, launch: Launch, records: list[dict]) -> list[str]:
        return review_council.check_launches(records, launch, trial.settings)

    def report(self, trial: EngineTrial, launch: Launch, output: Output) -> str:
        _exited(launch, output)
        try:
            return review_council.report(output.stdout)
        except review_council.ReviewCouncilOutputError as e:
            raise EngineFailure(
                f"invalid output: {trial.label}: {review_council.NAME}: {e}"
            ) from None


class PluginReview(Adapter):
    """`ponytail-review` (design D13): the plugin's review on the CLI of the reviewer's model,
    model and effort by flag; on Codex the plugin is installed into the trial's home first."""

    def homes(self, trial: EngineTrial) -> Homes:
        setting = ponytail_review.requested(trial.settings)
        return Homes(by_flag={MODELS[setting.model]: setting})

    def setup(self, trial: EngineTrial) -> tuple[tuple[str, ...], ...]:
        return ponytail_review.setup(trial.settings)

    def launch(self, trial: EngineTrial) -> Launch:
        return ponytail_review.launch(trial.settings, trial.pr)

    def report(self, trial: EngineTrial, launch: Launch, output: Output) -> str:
        _exited(launch, output)
        try:
            return ponytail_review.report(
                launch,
                output.status,
                output.stdout,
                output.answer,
                f"{trial.label}: {ponytail_review.NAME}",
            )
        except ponytail_review.PluginReviewOutputError as e:
            raise EngineFailure(f"invalid output: {e}") from None


ADAPTERS: dict[str, Adapter] = {
    plain.NAME: Plain(),
    rreview.NAME: Rreview(),
    codex_review.NAME: CodexReview(),
    code_review.NAME: CodeReview(),
    review_council.NAME: ReviewCouncil(),
    ponytail_review.NAME: PluginReview(),
}
