"""Trial counts and token estimates (spec engine-configurations: Plan with estimate, Rounds;
spec run-ledger: Round budget, Soft threshold of the subscription window; design O6).

Tokens are uncached input plus output, the cost measure of design D7; cache reads and writes are
left out. An estimate is per trial, split by kind (`engine`, `normalization`, `judge`) and by
provider (the CLI a model runs on), and comes from one of:

- the smoke run's median for the same configuration id (`smoke`);
- the smoke run's median for the same engine's default configuration (`smoke: <id>`). Normalization
  and judge tokens are kept as measured, since their models are pinned apart from the
  configuration. Engine tokens keep their per-provider split when the configuration uses the same
  providers; otherwise their total goes to the configuration's providers in equal shares, and the
  source says `split inferred`;
- the documented default below, when the smoke run did not measure the engine or no smoke run is
  given (`DEFAULT`).

Smoke medians file, JSON, given with `rbench plan --smoke FILE`; the live smoke run (issue #60)
stores its medians in this form:

    {"version": 1,
     "configurations": {
       "<configuration id>": {
         "engine": "plain",
         "tokens": {"engine": {"claude": 182000},
                    "normalization": {},
                    "judge": {"claude": 41000}}}}}

Each number is the median, over the smoke run's done trials of that configuration, of one trial's
tokens of that kind on that provider. The smoke stage runs one configuration per engine, so the
file lists at most one per engine. Engine tokens may not be empty or zero: a trial always spends
some, and an empty record would estimate every configuration of its engine at nothing.

A round's providers are those its configurations run on plus those its normalization and judge
estimates use; each needs a budget in the round, or planning refuses the round.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from rbench.engines.declaration import CLIS
from rbench.matrix.expansion import UNDECIDED, Configuration, Matrix, Round

KINDS = ("engine", "normalization", "judge")
SMOKE_VERSION = 1

# Default estimate per trial, used where no smoke median exists. A guess, not a measurement, so
# the output labels it DEFAULT; the smoke stage replaces it before any budget is approved (O6).
DEFAULT_ENGINE_TOKENS = 300_000  # on each provider the configuration uses
# limit: the normalization model is not pinned yet (issue #45); the default assumes Claude.
DEFAULT_NORMALIZATION_TOKENS = {"claude": 30_000}
DEFAULT_JUDGE_TOKENS = {"claude": 60_000}  # the judge is claude-opus-5-5 (design O1)
NO_NORMALIZATION = frozenset({"plain"})  # structured output, no model step (spec Plain output)

# Decision O6: a round's budget per provider is its plan estimate x 1.2.
BUDGET_MARGIN = (6, 5)
# Decision O6: the soft threshold is 80% of every subscription window the CLI reports.
SOFT_THRESHOLD_PERCENT = 80
# The windows each CLI reports a used share for (design D14). Claude Code is absent until spike
# #23 shows it reports one, so its soft threshold is not enforced.
REPORTED_WINDOWS = {"codex": ("5-hour", "weekly")}

Tokens = dict[str, dict[str, int]]  # kind -> provider -> tokens per trial
# The form of `Configuration.id`: `<engine>-<readable name of models, roles and efforts>-<sha256>`.
_CONFIG_ID = r"{engine}-[a-z0-9._+-]+-[0-9a-f]{{64}}"
_ENGINE = re.compile(r"[a-z0-9][a-z0-9-]*")


class SmokeError(ValueError):
    """A malformed smoke medians file."""


@dataclass(frozen=True)
class SmokeMedians:
    source: str
    configurations: dict[str, tuple[str, Tokens]]  # configuration id -> (engine, medians)

    def for_engine(self, engine: str) -> tuple[str, Tokens] | None:
        """The engine's measured configuration id and medians, if the smoke run measured it."""
        for config_id, (name, tokens) in self.configurations.items():
            if name == engine:
                return config_id, tokens
        return None


def _unique(pairs: list[tuple[str, object]]) -> dict:
    data = {}
    for key, value in pairs:
        if key in data:
            raise SmokeError(f"repeated key {key!r}")
        data[key] = value
    return data


def _object(value: object, where: str, keys: set[str]) -> dict:
    if not isinstance(value, dict):
        raise SmokeError(f"{where}: expected an object")
    if missing := keys - value.keys():
        raise SmokeError(f"{where}: missing {', '.join(sorted(missing))}")
    if unknown := value.keys() - keys:
        raise SmokeError(f"{where}: unknown {', '.join(sorted(unknown))}")
    return value


def parse_smoke(data: object, source: str) -> SmokeMedians:
    """Validate parsed smoke medians; `source` (the file) prefixes every error."""
    data = _object(data, source, {"version", "configurations"})
    if type(data["version"]) is not int or data["version"] != SMOKE_VERSION:
        raise SmokeError(f"{source}: version: expected {SMOKE_VERSION}, got {data['version']!r}")
    configurations = data["configurations"]
    if not (isinstance(configurations, dict) and configurations):
        raise SmokeError(f"{source}: configurations: expected a non-empty object")
    parsed: dict[str, tuple[str, Tokens]] = {}
    engines: dict[str, str] = {}
    for config_id, raw in configurations.items():
        where = f"{source}: configuration {config_id!r}"
        raw = _object(raw, where, {"engine", "tokens"})
        engine = raw["engine"]
        if not (isinstance(engine, str) and _ENGINE.fullmatch(engine)):
            raise SmokeError(f"{where}: engine: expected an engine name, got {engine!r}")
        if not re.fullmatch(_CONFIG_ID.format(engine=re.escape(engine)), config_id):
            raise SmokeError(
                f"{where}: not a configuration id of engine {engine!r}:"
                f" expected {engine}-<name>-<64 lowercase hex>"
            )
        if engine in engines:
            raise SmokeError(f"{where}: engine {engine!r} is already measured by {engines[engine]}")
        engines[engine] = config_id
        tokens = _object(raw["tokens"], f"{where}: tokens", set(KINDS))
        for kind in KINDS:
            at = f"{where}: tokens: {kind}"
            per_provider = tokens[kind]
            if not isinstance(per_provider, dict):
                raise SmokeError(f"{at}: expected an object of provider -> tokens")
            for provider, count in per_provider.items():
                if provider not in CLIS:
                    raise SmokeError(f"{at}: unknown provider {provider!r}")
                if type(count) is not int or count < 0:
                    raise SmokeError(f"{at}: {provider}: expected a token count, got {count!r}")
        if not sum(tokens["engine"].values()):
            raise SmokeError(f"{where}: tokens: engine: no engine usage measured")
        parsed[config_id] = (engine, {k: dict(sorted(tokens[k].items())) for k in KINDS})
    return SmokeMedians(source, parsed)


def load_smoke(path: Path) -> SmokeMedians:
    """Read and validate the smoke medians file at `path`."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique)
    except (UnicodeDecodeError, json.JSONDecodeError, SmokeError) as e:
        raise SmokeError(f"{path}: not a valid smoke medians file: {e}") from e
    return parse_smoke(data, str(path))


def default_estimate(config: Configuration) -> Tokens:
    normalization = {} if config.engine in NO_NORMALIZATION else DEFAULT_NORMALIZATION_TOKENS
    return {
        "engine": {p: DEFAULT_ENGINE_TOKENS for p in sorted(config.providers)},
        "normalization": dict(normalization),
        "judge": dict(DEFAULT_JUDGE_TOKENS),
    }


def trial_estimate(config: Configuration, smoke: SmokeMedians | None) -> tuple[Tokens, str]:
    """The per-trial estimate of `config` and its source: `smoke`, `smoke: <id>` or `DEFAULT`."""
    if smoke is not None and config.id in smoke.configurations:
        return smoke.configurations[config.id][1], "smoke"
    measured = smoke.for_engine(config.engine) if smoke is not None else None
    if measured is None:
        return default_estimate(config), "DEFAULT"
    measured_id, tokens = measured
    engine = tokens["engine"]
    source = f"smoke: {measured_id}"
    if engine.keys() != config.providers:
        share = -(-sum(engine.values()) // len(config.providers))  # rounded up
        engine = {p: share for p in sorted(config.providers)}
        source += ", split inferred"
    return tokens | {"engine": engine}, source


@dataclass(frozen=True)
class RoundPlan:
    round: Round
    trials_per_configuration: int  # tasks x attempts
    estimates: tuple[tuple[Configuration, Tokens, str], ...]  # in the round's order

    @staticmethod
    def _uses(config: Configuration, tokens: Tokens) -> set[str]:
        return set(config.providers).union(*tokens.values())

    @property
    def providers(self) -> list[str]:
        """Every provider a trial of the round spends on: engine, normalization or judge."""
        return sorted(set().union(*(self._uses(c, t) for c, t, _ in self.estimates)))

    def trials(self, provider: str | None = None) -> int:
        used = [c for c, t, _ in self.estimates if provider is None or provider in self._uses(c, t)]
        return len(used) * self.trials_per_configuration

    def tokens(self, provider: str, kind: str) -> int:
        per_trial = sum(t[kind].get(provider, 0) for _, t, _ in self.estimates)
        return per_trial * self.trials_per_configuration

    def total(self, provider: str) -> int:
        return sum(self.tokens(provider, k) for k in KINDS)

    def refusals(self) -> list[str]:
        """Why planning refuses the round: per provider, no budget, or an estimate over it."""
        reasons = []
        for p in self.providers:
            if p not in self.round.budget:
                reasons.append(
                    f"the {p} estimate {self.total(p)} has no {p} budget in the round;"
                    f' add {p} = "{UNDECIDED}" or a token count to its budget'
                )
            elif (budget := self.round.budget[p]) is not None and self.total(p) > budget:
                reasons.append(f"the {p} estimate {self.total(p)} exceeds its budget {budget}")
        return reasons


def plan_round(round_: Round, tasks: int, attempts: int, smoke: SmokeMedians | None) -> RoundPlan:
    estimates = tuple((c, *trial_estimate(c, smoke)) for c in round_.configurations)
    return RoundPlan(round_, tasks * attempts, estimates)


def suggested_budget(estimate: int) -> int:
    """Decision O6's budget for an estimate: x 1.2, rounded up."""
    numerator, denominator = BUDGET_MARGIN
    return -(-estimate * numerator // denominator)


def _table(rows: list[list[str]], indent: str) -> list[str]:
    """Aligned columns: numbers to the right, text to the left."""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return [
        (
            indent + "  ".join(c.rjust(w) if c.isdigit() else c.ljust(w) for c, w in zip(r, widths))
        ).rstrip()
        for r in rows
    ]


def render(
    source: str,
    matrix: Matrix,
    tasks: int,
    origin: str,
    plans: list[RoundPlan],
    smoke: SmokeMedians | None,
) -> str:
    """The plan as text; the same inputs always give the same text."""
    attempts = f"{matrix.attempts} attempt{'s' if matrix.attempts != 1 else ''}"
    lines = [
        f"plan of {source}: stage {matrix.stage}, {tasks} tasks ({origin}), {attempts} per task",
        "tokens: uncached input plus output per provider (design D7); nothing runs",
    ]
    defaults = sum(1 for p in plans for _, _, s in p.estimates if s == "DEFAULT")
    if smoke is None:
        lines.append(
            "estimate: DEFAULT, no smoke run given; not measured, do not set budgets from it"
        )
    else:
        lines.append(f"estimate: smoke medians from {smoke.source}")
        if defaults:
            lines.append(
                f"  {defaults} configuration(s) use the DEFAULT estimate: their engine was not"
                " measured"
            )
    if defaults:
        normalization = ", ".join(f"{t} on {p}" for p, t in DEFAULT_NORMALIZATION_TOKENS.items())
        judge = ", ".join(f"{t} on {p}" for p, t in DEFAULT_JUDGE_TOKENS.items())
        lines.append(
            f"  DEFAULT per trial: engine {DEFAULT_ENGINE_TOKENS} on each provider the"
            f" configuration uses; normalization {normalization} (none for"
            f" {', '.join(sorted(NO_NORMALIZATION))}); judge {judge}"
        )

    for plan in plans:
        r = plan.round
        n = len(r.configurations)
        lines += [
            "",
            f"round {r.name}: {plan.trials()} trials ({n} configurations x {tasks} tasks x {attempts})",
        ]
        rows = [["provider", "trials", *KINDS, "estimate", "budget", "estimate x 1.2", "status"]]
        for p in plan.providers:
            total, budget = plan.total(p), r.budget.get(p)
            if p not in r.budget:
                status = "NO BUDGET"
            elif budget is None:
                status = "budget undecided"
            elif total > budget:
                status = "OVER BUDGET"
            else:
                status = "within budget"
            rows.append(
                [
                    p,
                    str(plan.trials(p)),
                    *(str(plan.tokens(p, k)) for k in KINDS),
                    str(total),
                    "none" if p not in r.budget else UNDECIDED if budget is None else str(budget),
                    str(suggested_budget(total)),
                    status,
                ]
            )
        lines += _table(rows, "  ")
        lines.append("  configurations (estimate source):")
        lines += _table([[c.id, s] for c, _, s in plan.estimates], "    ")

    if len(plans) > 1:
        providers = sorted(set().union(*(p.providers for p in plans)))
        totals = ", ".join(f"{p} {sum(x.total(p) for x in plans)}" for p in providers)
        lines += ["", f"all rounds: {sum(p.trials() for p in plans)} trials; tokens {totals}"]

    lines += [
        "",
        f"soft threshold: {SOFT_THRESHOLD_PERCENT}% of every subscription window the CLI reports",
    ]
    for p in sorted(set().union(*(p.providers for p in plans))):
        if windows := REPORTED_WINDOWS.get(p):
            lines.append(
                f"  {p}: no new trial once the {' or '.join(windows)} window is"
                f" {SOFT_THRESHOLD_PERCENT}% used"
            )
        else:
            lines.append(f"  {p}: not enforced; its CLI reports no used share")
    return "\n".join(lines) + "\n"
