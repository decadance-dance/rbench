"""Matrix files (spec engine-configurations: Matrix expansion, Stable configuration ids, Rounds,
Stages).

A matrix file is TOML:

    stage = "screening"   # smoke | screening | full; fixes tasks, max_tasks and attempts
    tasks = "core"        # core | all
    attempts = 1
    # max_tasks = 1       # the first N tasks of the list (the smoke stage)

    [[rounds]]            # in order of importance; a run executes one round
    name = "medium"
    budget = { claude = 2000000, codex = "undecided" }  # tokens per provider (the model's CLI)

    [[rounds.entries]]
    engine = "plain"
    roles.reviewer = { model = ["claude-opus-5-5", "gpt-6.2-sol"], effort = ["medium", "high"] }

    [[rounds.entries]]
    engine = "rreview"
    roles.panel = { models = ["gpt-6.2-sol", "gpt-6.2-astra"], effort = "high" }

    [[rounds.entries]]
    engine = "*"          # every declared engine; only with default = true
    default = true

A list under `model` or `effort` expands as a cartesian product within its entry only. `models` is
one set of models for one role and does not expand. A role that is not listed, and a field a listed
role leaves out, keep the engine's default. Models go through `models.toml`, so an alias is rejected.
A configuration appears once in a matrix; listing it again, in any round, is an error.
"""

import hashlib
import itertools
import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rbench.engines import normalization
from rbench.engines.declaration import CLIS, Engine, Setting
from rbench.engines.pins import MODELS
from rbench.engines.plain import NAME as PLAIN

ALL_ENGINES = "*"
# A budget the maintainer has not approved yet; decision O6 (issue #16) sets each from the
# round's plan estimate.
UNDECIDED = "undecided"
# stage: (tasks, max_tasks, attempts), as spec Stages defines them.
STAGES = {
    "smoke": ("core", 1, 1),
    "screening": ("core", None, 1),
    "full": ("all", None, 3),
}
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


class MatrixError(ValueError):
    """A malformed matrix file."""


@dataclass(frozen=True)
class RoleSetting:
    models: tuple[str, ...]  # one model, or the set that `models` names, sorted
    effort: str


@dataclass(frozen=True)
class Configuration:
    engine: str
    version: str
    roles: tuple[tuple[str, RoleSetting], ...]  # every role of the engine, sorted by name
    inputs: tuple[tuple[str, str], ...] = ()  # sorted; `Engine.inputs_for` its providers
    markdown: bool = False  # the engine's output goes through the normalization step
    id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", f"{self.name}-{self.hash}")

    @property
    def settings(self) -> dict:
        """The full settings the id hashes: the engine, its exact version and every role.

        The id is only as exact as `version`. Before planning freezes ids or checks reuse:
        - a git tag pin (`refs/tags/<name>`) must be resolved to its commit, so the engine passed
          in carries the commit;
        - for `plain`, the version is its prompt hash, and `inputs` holds the findings schema
          hash and, for a Codex model, the model catalog hash (design D12); an engine without
          inputs has no `inputs` key, so its ids do not depend on this field.

        A Markdown engine's settings also hold the normalization step's pin, so a new normalizer
        model or prompt gives new ids; a structured engine's do not.
        """
        roles = {r: {"models": list(s.models), "effort": s.effort} for r, s in self.roles}
        settings = {"engine": self.engine, "version": self.version, "roles": roles}
        if self.markdown:
            settings["normalization"] = dict(normalization.SETTINGS)
        return settings | ({"inputs": dict(self.inputs)} if self.inputs else {})

    @property
    def hash(self) -> str:
        text = json.dumps(self.settings, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()

    @property
    def name(self) -> str:
        """Readable, and derived from the settings alone, so equal settings give equal names."""

        def part(s: RoleSetting) -> str:
            return f"{'+'.join(s.models)}-{s.effort}"

        if len(self.roles) == 1:
            return f"{self.engine}-{part(self.roles[0][1])}"
        return "-".join([self.engine, *(f"{r}-{part(s)}" for r, s in self.roles)])

    @property
    def providers(self) -> frozenset[str]:
        """The CLIs its models run on, which its engine tokens are on."""
        return frozenset(MODELS[m] for _, s in self.roles for m in s.models)

    @property
    def budget_providers(self) -> frozenset[str]:
        """The CLIs a trial spends on: its models' and, for a Markdown engine, the normalizer's.
        A round's budget covers each; engine estimates use `providers` alone."""
        extra = {MODELS[normalization.MODEL]} if self.markdown else set()
        return self.providers | extra


@dataclass(frozen=True)
class Round:
    name: str
    budget: dict[str, int | None]  # provider -> tokens; None: listed as undecided
    configurations: tuple[Configuration, ...]

    def budget_for(self, provider: str) -> int:
        """The token budget for `provider`; planning a run or starting one goes through this.

        Raises if the round lists no budget for it, or lists it as undecided: the maintainer
        approves each number from the round's plan estimate (decision O6, issue #16).
        """
        if provider not in self.budget:
            raise MatrixError(f"round {self.name!r}: no budget for {provider}")
        if (tokens := self.budget[provider]) is None:
            raise MatrixError(
                f"round {self.name!r}: the {provider} budget is still {UNDECIDED!r};"
                " set it in the matrix file before running this round"
            )
        return tokens


@dataclass(frozen=True)
class Matrix:
    stage: str
    tasks: str
    max_tasks: int | None
    attempts: int
    rounds: tuple[Round, ...]  # in the file's order, which is the order of importance

    def select_tasks(self, core: Sequence[str], all_tasks: Sequence[str]) -> list[str]:
        """The task ids this matrix plans, from the core list or all tasks, in their order.
        Raises when that list is empty."""
        tasks = list(core if self.tasks == "core" else all_tasks)[: self.max_tasks]
        if not tasks:
            raise MatrixError(
                f"the {self.stage} stage selects {self.tasks} tasks, but there are none"
            )
        return tasks


def can_reuse(stage: str) -> bool:
    """Whether a new run of `stage` may reuse done trials of an earlier run (spec run-ledger,
    Reuse of earlier trials).

    The full stage runs fresh attempts (spec engine-configurations, Stages), so it never reuses
    trials of an earlier run, screening or full; its scores count only its own trials. Resuming
    the same full run keeps its done trials: that is resume, not reuse.
    """
    if not (isinstance(stage, str) and stage in STAGES):
        raise MatrixError(f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}")
    return stage != "full"


def _table(
    value: object, where: str, required: set[str], optional: frozenset[str] = frozenset()
) -> dict:
    if not isinstance(value, dict):
        raise MatrixError(f"{where}: expected a table, got {value!r}")
    if missing := required - value.keys():
        raise MatrixError(f"{where}: missing {', '.join(sorted(missing))}")
    if unknown := value.keys() - required - optional:
        raise MatrixError(f"{where}: unknown {', '.join(sorted(unknown))}")
    return value


def _strings(value: object, where: str, listed: bool = False) -> list[str]:
    """A string, or a non-empty list of distinct strings; `listed` requires the list."""
    items = value if isinstance(value, list) else None if listed else [value]
    if not items or not all(isinstance(i, str) for i in items) or len(set(items)) != len(items):
        want = "a" if listed else "a string or a"
        raise MatrixError(f"{where}: expected {want} non-empty list of distinct strings")
    return items


def configuration(engine: Engine, roles: Sequence[tuple[str, RoleSetting]]) -> Configuration:
    """The configuration of `engine` with these role settings (sorted by role), with the input
    hashes of the CLIs its models run on."""
    clis = {MODELS[m] for _, s in roles for m in s.models}
    return Configuration(
        engine.name,
        engine.version,
        tuple(roles),
        engine.inputs_for(clis),
        markdown=engine.output == "markdown",
    )


def _default(engine: Engine) -> Configuration:
    roles = engine.default_configuration()
    return configuration(
        engine, [(r, RoleSetting((s.model,), s.effort)) for r, s in sorted(roles.items())]
    )


def _expand(entry: object, where: str, engines: Mapping[str, Engine]) -> list[Configuration]:
    entry = _table(entry, where, {"engine"}, frozenset({"default", "roles"}))
    name, default = entry["engine"], entry.get("default", False)
    if not isinstance(name, str):
        raise MatrixError(f"{where}: engine: expected an engine name, got {name!r}")
    if default is not True and "default" in entry:
        raise MatrixError(f"{where}: default must be true when given, got {default!r}")
    if default == ("roles" in entry):
        raise MatrixError(f"{where}: give either default = true or roles")
    if name == ALL_ENGINES:
        if not default:
            raise MatrixError(f"{where}: engine '*' is only allowed with default = true")
        if not engines:
            raise MatrixError(f"{where}: engine '*' but no engine is declared")
        return [_default(engines[n]) for n in sorted(engines)]
    if name not in engines:
        known = ", ".join(sorted(engines)) or "none"
        raise MatrixError(f"{where}: unknown engine {name!r}; declared: {known}")
    engine = engines[name]
    if default:
        return [_default(engine)]

    roles = _table(entry["roles"], f"{where}: roles", set(), frozenset(engine.roles))
    if not roles:
        raise MatrixError(f"{where}: roles: list at least one role, or use default = true")
    choices = []  # per role, in name order: the settings it expands to
    for role in sorted(engine.roles):
        at = f"{where}: role {role!r}"
        d = engine.roles[role].default
        raw = _table(roles.get(role, {}), at, set(), frozenset({"model", "models", "effort"}))
        if "model" in raw and "models" in raw:
            raise MatrixError(f"{at}: give model or models, not both")
        if "models" in raw:
            model_sets = [tuple(sorted(_strings(raw["models"], f"{at}: models", listed=True)))]
            if engine.name == PLAIN and len(model_sets[0]) > 1:
                raise MatrixError(
                    f"{at}: plain runs one model per trial, not a set; give model = [...] to plan"
                    " one configuration per model"
                )
        else:
            model_sets = [(m,) for m in _strings(raw.get("model", d.model), f"{at}: model")]
        efforts = _strings(raw.get("effort", d.effort), f"{at}: effort")
        settings = [RoleSetting(ms, e) for ms in model_sets for e in efforts]
        for s in settings:
            for model in s.models:
                engine.delivery(role, Setting(model, s.effort), at)
        choices.append(settings)
    names = sorted(engine.roles)
    return [
        configuration(engine, tuple(zip(names, combo, strict=True)))
        for combo in itertools.product(*choices)
    ]


def _budget(value: object, where: str, used: frozenset[str]) -> dict[str, int | None]:
    budget = _table(value, where, set(), frozenset(CLIS))
    if missing := used - budget.keys():
        raise MatrixError(f"{where}: no budget for {', '.join(sorted(missing))}, which it uses")
    for provider, tokens in budget.items():
        if tokens != UNDECIDED and not (type(tokens) is int and tokens > 0):
            raise MatrixError(
                f"{where}: {provider}: expected a positive token count or {UNDECIDED!r},"
                f" got {tokens!r}"
            )
    return {p: None if t == UNDECIDED else t for p, t in sorted(budget.items())}


def parse_matrix(data: object, source: str, engines: Mapping[str, Engine]) -> Matrix:
    """Validate a parsed matrix and expand it against the declared `engines` (name -> engine).

    `source` (its file) prefixes every error. Raises `ValueError` (a `MatrixError`, or the
    engine's error for a bad model, alias or effort).
    """
    data = _table(data, source, {"stage", "tasks", "attempts", "rounds"}, frozenset({"max_tasks"}))
    stage = data["stage"]
    if not (isinstance(stage, str) and stage in STAGES):
        raise MatrixError(f"{source}: stage: expected one of {', '.join(STAGES)}, got {stage!r}")
    for key, want in zip(("tasks", "max_tasks", "attempts"), STAGES[stage], strict=True):
        got = data.get(key)
        if type(got) is not type(want) or got != want:
            raise MatrixError(f"{source}: {key}: the {stage} stage needs {want!r}, got {got!r}")

    if not isinstance(data["rounds"], list):
        raise MatrixError(f"{source}: rounds: expected a list of rounds")
    rounds: list[Round] = []
    seen: dict[str, str] = {}  # configuration id -> the entry that first gave it
    for i, raw in enumerate(data["rounds"], 1):
        raw = _table(raw, f"{source}: round {i}", {"name", "budget", "entries"})
        name = raw["name"]
        if not (isinstance(name, str) and _NAME.fullmatch(name)):
            raise MatrixError(f"{source}: round {i}: bad round name {name!r}")
        if any(r.name == name for r in rounds):
            raise MatrixError(f"{source}: round {i}: round {name!r} is listed twice")
        at = f"{source}: round {name!r}"
        if not (isinstance(raw["entries"], list) and raw["entries"]):
            raise MatrixError(f"{at}: entries: expected a non-empty list of entries")
        configurations: list[Configuration] = []
        for j, entry in enumerate(raw["entries"], 1):
            where = f"{at}: entry {j}"
            if stage == "smoke" and not (isinstance(entry, dict) and entry.get("default") is True):
                raise MatrixError(f"{where}: the smoke stage runs engine defaults only")
            for c in _expand(entry, where, engines):
                if c.id in seen:
                    raise MatrixError(f"{where}: configuration {c.id} repeats {seen[c.id]}")
                seen[c.id] = where
                configurations.append(c)
        used = frozenset().union(*(c.budget_providers for c in configurations))
        budget = _budget(raw["budget"], f"{at}: budget", used)
        rounds.append(Round(name, budget, tuple(configurations)))
    if stage == "smoke":
        # A smoke run that misses an engine would look complete while leaving it untested.
        if not rounds:
            raise MatrixError(f"{source}: rounds: the smoke stage needs a round")
        for r in rounds:
            if missing := engines.keys() - {c.engine for c in r.configurations}:
                raise MatrixError(
                    f"{source}: round {r.name!r}: the smoke stage runs every engine's default;"
                    f" missing {', '.join(sorted(missing))}"
                )
    tasks, max_tasks, attempts = STAGES[stage]
    return Matrix(stage, tasks, max_tasks, attempts, tuple(rounds))


def load_matrix(path: Path, engines: Mapping[str, Engine]) -> Matrix:
    """Read the matrix file at `path` and expand it against `engines`."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise MatrixError(f"{path}: not a valid UTF-8 TOML file: {e}") from e
    return parse_matrix(data, str(path), engines)
