"""Engine declarations (spec engine-configurations, Engine declarations).

A declaration is a TOML file:

    name = "example"
    pin = { kind = "git", ref = "v1.2.3" }  # git, version, sha256, or cli (ref names the CLI)

    [cli]  # exact versions of the CLIs installed in the engine's image
    claude = "2.1.316"
    codex = "0.167.0"

    [roles.coordinator]
    cli = "claude"     # omitted: the model's provider picks the CLI
    delivery = "flag"  # flag: the shim adds model and effort flags;
                       # config: the adapter writes them into the trial's config.toml
    default = { model = "claude-opus-5-5", effort = "high" }

The roles' defaults together are the engine's default configuration.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from rbench.engines.pins import check_ref, model_cli

CLIS = ("claude", "codex")
DELIVERIES = ("flag", "config")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_ROLE = re.compile(r"[a-z][a-z0-9_-]*")
_EFFORT = re.compile(r"[a-z]+")


class DeclarationError(ValueError):
    """A malformed engine declaration."""


@dataclass(frozen=True)
class Setting:
    model: str
    effort: str


@dataclass(frozen=True)
class Delivery:
    """How a role's model and effort reach its CLI session."""

    flags: tuple[str, ...] = ()  # added by the shim
    config: tuple[tuple[str, str], ...] = ()  # (key, value) for the trial's config.toml


@dataclass(frozen=True)
class Role:
    name: str
    cli: str | None  # None: picked per model by its provider
    delivery: str
    default: Setting


@dataclass(frozen=True)
class Engine:
    name: str
    pin_kind: str
    pin_ref: str
    clis: dict[str, str]  # CLI name -> exact version
    roles: dict[str, Role]

    @property
    def version(self) -> str:
        """The engine's exact version; for kind `cli`, the version of that CLI."""
        return self.clis[self.pin_ref] if self.pin_kind == "cli" else self.pin_ref

    def default_configuration(self) -> dict[str, Setting]:
        """The declared default of every role (a matrix entry with `default: true`)."""
        return {name: role.default for name, role in self.roles.items()}

    def delivery(self, role: str, setting: Setting) -> Delivery:
        """Map `setting` to the shim flags or config.toml keys that carry it to `role`'s CLI."""
        cli = self.cli_for(role, setting.model)
        if not (isinstance(setting.effort, str) and _EFFORT.fullmatch(setting.effort)):
            raise DeclarationError(
                f"engine {self.name!r}: role {role!r}: bad effort {setting.effort!r}"
            )
        if self.roles[role].delivery == "config":
            return Delivery(
                config=(("model", setting.model), ("model_reasoning_effort", setting.effort))
            )
        if cli == "claude":
            return Delivery(flags=("--model", setting.model, "--effort", setting.effort))
        return Delivery(
            flags=("-m", setting.model, "-c", f'model_reasoning_effort="{setting.effort}"')
        )

    def cli_for(self, role: str, model: str) -> str:
        """The CLI that runs `model` in `role`; reject an alias, a model the role's CLI cannot
        run, or a CLI the engine does not pin."""
        if role not in self.roles:
            raise DeclarationError(f"engine {self.name!r}: no role {role!r}")
        cli = model_cli(self.name, model)
        declared = self.roles[role].cli
        if declared is not None and declared != cli:
            raise DeclarationError(
                f"engine {self.name!r}: role {role!r} runs on {declared}, model {model!r} on {cli}"
            )
        if cli not in self.clis:
            raise DeclarationError(f"engine {self.name!r}: role {role!r} needs a pinned {cli} CLI")
        return cli


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
    """Validate a parsed declaration; `source` names it in errors."""
    data = _table(data, source, {"name", "pin", "cli", "roles"})
    name = data["name"]
    if not (isinstance(name, str) and _NAME.fullmatch(name)):
        raise DeclarationError(f"{source}: bad engine name {name!r}")
    where = f"{source}: engine {name!r}"

    clis = _table(data["cli"], f"{where}: cli", set(), frozenset(CLIS))
    for cli, version in clis.items():
        check_ref(name, "version", version, f"{cli} CLI version")

    pin = _table(data["pin"], f"{where}: pin", {"kind", "ref"})
    kind, ref = pin["kind"], pin["ref"]
    if kind == "cli":
        if not isinstance(ref, str) or ref not in clis:
            raise DeclarationError(f"{where}: pin names CLI {ref!r}, which [cli] does not pin")
    elif kind in ("git", "version", "sha256"):
        check_ref(name, kind, ref, "engine ref")
    else:
        raise DeclarationError(f"{where}: unknown pin kind {kind!r}")

    roles = data["roles"]
    if not (isinstance(roles, dict) and roles):
        raise DeclarationError(f"{where}: roles must be a table of at least one role")
    engine = Engine(name, kind, ref, dict(clis), {})
    for role_name, raw in roles.items():
        at = f"{where}: role {role_name!r}"
        if not _ROLE.fullmatch(role_name):
            raise DeclarationError(f"{where}: bad role name {role_name!r}")
        raw = _table(raw, at, {"delivery", "default"}, frozenset({"cli"}))
        cli, delivery = raw.get("cli"), raw["delivery"]
        if cli is not None and cli not in CLIS:
            raise DeclarationError(f"{at}: unknown cli {cli!r}")
        if delivery not in DELIVERIES:
            raise DeclarationError(f"{at}: delivery must be flag or config, got {delivery!r}")
        if delivery == "config" and cli != "codex":
            # Only Codex reads model and effort from the trial's config.toml (design D5).
            raise DeclarationError(f"{at}: config delivery needs cli = 'codex'")
        default = _table(raw["default"], f"{at}: default", {"model", "effort"})
        engine.roles[role_name] = Role(role_name, cli, delivery, Setting(**default))
        engine.delivery(role_name, engine.roles[role_name].default)
    return engine


def load_engine(path: Path) -> Engine:
    """Read and validate the declaration at `path`."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise DeclarationError(f"{path}: not valid TOML: {e}") from e
    return parse_engine(data, str(path))
