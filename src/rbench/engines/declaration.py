"""Engine declarations (spec engine-configurations, Engine declarations).

A declaration is a TOML file:

    name = "example"
    pin = { kind = "git", ref = "refs/tags/v1.2.3" }
    output = "markdown"  # or "structured" (the default): how its findings come out

    [cli]  # exact versions of the CLIs installed in the engine's image
    claude = "2.1.316"
    codex = "0.167.0"

    [roles.coordinator]
    cli = "claude"     # omitted: the model's CLI (models.toml) picks it
    delivery = "flag"  # flag: the shim adds model and effort flags;
                       # config: the adapter writes them into the trial's config.toml
    default = { model = "claude-opus-5-5", effort = "high" }

A `markdown` engine's report goes through the pinned normalization step
(`rbench.engines.normalization`), whose pin joins its configurations' settings; `structured`
findings are converted without a model step (spec engine-configurations, Findings normalization).

The roles' defaults together are the engine's default configuration.

Pin kinds: `git` (a commit, or a tag that planning resolves to its commit), `version`, `sha256`
(a prompt hash) and `cli` (`ref` names the CLI whose version pins the engine). The loader checks
that the ref is exact for the declared kind; which kind an engine must use (spec Engine adapters:
rreview by commit or tag, code-review by the Claude Code version, ...) is checked by its adapter.
"""

import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from rbench.engines import normalization
from rbench.engines.pins import MODELS, check_ref, model_cli

CLIS = ("claude", "codex")
DELIVERIES = ("flag", "config")
OUTPUTS = ("structured", "markdown")
# The effort values each CLI accepts, checked against:
# - Claude Code 2.1.316: `claude --help`, `--effort <level>`;
# - Codex CLI 0.167.0: the union of the `supported_reasoning_levels` its bundled catalog
#   (`codex debug models --bundled`) advertises across models; its config schema takes any string.
# Whether a model honors the effort is recorded per session (spec: Applied model and effort).
EFFORTS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("low", "medium", "high", "xhigh", "max", "ultra"),
}
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_ROLE = re.compile(r"[a-z][a-z0-9_-]*")


class DeclarationError(ValueError):
    """A malformed engine declaration or setting."""


@dataclass(frozen=True)
class Setting:
    model: str
    effort: str


@dataclass(frozen=True)
class Delivery:
    """How a role's model and effort reach its CLI session."""

    cli: str
    flags: tuple[str, ...] = ()  # added by the shim
    config: tuple[tuple[str, str], ...] = ()  # (key, value) for the trial's config.toml


@dataclass(frozen=True)
class Role:
    name: str
    cli: str | None  # None: the model's CLI
    delivery: str
    default: Setting


@dataclass(frozen=True)
class Engine:
    name: str
    pin_kind: str
    pin_ref: str
    clis: dict[str, str]  # CLI name -> exact version
    roles: dict[str, Role]
    output: str = "structured"
    # CLI -> {input name -> SHA-256}: content hashes of files a session on that CLI reads, which
    # join the settings of every configuration on it (`plain`: its schema and Codex catalog).
    inputs: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def version(self) -> str:
        """The engine's exact version; for kind `cli`, the version of that CLI."""
        return self.clis[self.pin_ref] if self.pin_kind == "cli" else self.pin_ref

    def inputs_for(self, clis: Iterable[str]) -> tuple[tuple[str, str], ...]:
        """The input hashes of a configuration whose models run on `clis`, sorted."""
        merged: dict[str, str] = {}
        for cli in clis:
            merged |= self.inputs.get(cli, {})
        return tuple(sorted(merged.items()))

    def default_configuration(self) -> dict[str, Setting]:
        """The declared default of every role (a matrix entry with `default: true`)."""
        return {name: role.default for name, role in self.roles.items()}

    def delivery(self, role: str, setting: Setting, where: str | None = None) -> Delivery:
        """Check `setting` for `role` and map it to the shim flags or the config.toml keys that
        carry it to the role's CLI. `where` prefixes errors (default: the engine and role)."""
        if role not in self.roles:
            raise DeclarationError(f"engine {self.name!r}: no role {role!r}")
        where = where or f"engine {self.name!r}: role {role!r}"
        model, effort = setting.model, setting.effort
        cli = model_cli(where, model)
        declared = self.roles[role].cli
        if declared is not None and declared != cli:
            raise DeclarationError(f"{where}: runs on {declared}, model {model!r} on {cli}")
        if cli not in self.clis:
            raise DeclarationError(f"{where}: model {model!r} needs a pinned {cli} CLI")
        if not (isinstance(effort, str) and effort in EFFORTS[cli]):
            raise DeclarationError(
                f"{where}: effort {effort!r} is not one {cli} accepts: {', '.join(EFFORTS[cli])}"
            )
        if self.roles[role].delivery == "config":
            return Delivery(cli, config=(("model", model), ("model_reasoning_effort", effort)))
        if cli == "claude":
            return Delivery(cli, flags=("--model", model, "--effort", effort))
        return Delivery(cli, flags=("-m", model, "-c", f'model_reasoning_effort="{effort}"'))


def _table(
    value: object, where: str, required: set[str], optional: frozenset[str] = frozenset()
) -> dict:
    if not isinstance(value, dict):
        raise DeclarationError(f"{where}: expected a table, got {value!r}")
    if missing := required - value.keys():
        raise DeclarationError(f"{where}: missing {', '.join(sorted(missing))}")
    if unknown := value.keys() - required - optional:
        raise DeclarationError(f"{where}: unknown {', '.join(sorted(unknown))}")
    return value


def parse_engine(data: object, source: str) -> Engine:
    """Validate a parsed declaration; `source` (its file) prefixes every error."""
    data = _table(data, source, {"name", "pin", "cli", "roles"}, frozenset({"output"}))
    name = data["name"]
    if not (isinstance(name, str) and _NAME.fullmatch(name)):
        raise DeclarationError(f"{source}: name: bad engine name {name!r}")
    where = f"{source}: engine {name!r}"

    clis = _table(data["cli"], f"{where}: cli", set(), frozenset(CLIS))
    for cli, version in clis.items():
        check_ref(where, "version", version, f"cli.{cli}")

    pin = _table(data["pin"], f"{where}: pin", {"kind", "ref"})
    kind, ref = pin["kind"], pin["ref"]
    if kind == "cli":
        if not isinstance(ref, str) or ref not in clis:
            raise DeclarationError(f"{where}: pin.ref names CLI {ref!r}, which [cli] does not pin")
    elif kind in ("git", "version", "sha256"):
        ref = check_ref(where, kind, ref, "pin.ref")
    else:
        raise DeclarationError(f"{where}: pin.kind: unknown pin kind {kind!r}")

    output = data.get("output", "structured")
    if output not in OUTPUTS:
        raise DeclarationError(f"{where}: output must be structured or markdown, got {output!r}")
    # The normalizer runs in the engine's own image (design D9), so that image needs its CLI.
    normalizer_cli = MODELS[normalization.MODEL]
    if output == "markdown" and normalizer_cli not in clis:
        raise DeclarationError(
            f"{where}: output = 'markdown' needs a pinned {normalizer_cli} CLI for the"
            f" normalization step ({normalization.MODEL}); add cli.{normalizer_cli}"
        )

    roles = data["roles"]
    if not (isinstance(roles, dict) and roles):
        raise DeclarationError(f"{where}: roles must be a table of at least one role")
    engine = Engine(name, kind, ref, dict(clis), {}, output)
    for role_name, raw in roles.items():
        at = f"{where}: role {role_name!r}"
        if not _ROLE.fullmatch(role_name):
            raise DeclarationError(f"{at}: bad role name")
        raw = _table(raw, at, {"delivery", "default"}, frozenset({"cli"}))
        cli, delivery = raw.get("cli"), raw["delivery"]
        if cli is not None and cli not in CLIS:
            raise DeclarationError(f"{at}: cli: unknown cli {cli!r}")
        if delivery not in DELIVERIES:
            raise DeclarationError(f"{at}: delivery must be flag or config, got {delivery!r}")
        if delivery == "config" and cli != "codex":
            # Only Codex reads model and effort from the trial's config.toml (design D5).
            raise DeclarationError(f"{at}: config delivery needs cli = 'codex'")
        default = _table(raw["default"], f"{at}: default", {"model", "effort"})
        engine.roles[role_name] = Role(role_name, cli, delivery, Setting(**default))
        engine.delivery(role_name, engine.roles[role_name].default, f"{at}: default")
    return engine


def load_engine(path: Path) -> Engine:
    """Read and validate the declaration at `path`."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise DeclarationError(f"{path}: not a valid UTF-8 TOML file: {e}") from e
    engine = parse_engine(data, str(path))
    # The adapters import this module.
    if engine.name == "plain":
        from rbench.engines.plain import bind

        engine = bind(engine, Path(path).parent)
    elif engine.name == "rreview":
        from rbench.engines.rreview import bind

        engine = bind(engine, Path(path).parent)
    return engine
