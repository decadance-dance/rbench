"""The `plain` baseline engine (spec engine-configurations: Plain baseline prompt and role, Plain
baseline tools, Plain baseline output; design D12).

`engines/plain.toml` declares it; `engines/plain/` next to it holds what every trial reads, the
same for both CLIs: `prompt.md`, `findings.schema.json`, and the Codex model catalog per CLI
version (`rbench.engines.catalog`). The declaration pins the prompt's SHA-256, so the prompt hash
is the engine's version; `bind` adds the schema hash, and for Codex the catalog hash, which join
the settings of every configuration on that CLI. Editing any of them therefore gives every
affected `plain` configuration a new id.

A trial runs one `reviewer` session, through `claude` for Claude models and `codex` for OpenAI
models. The prompt goes on stdin, the shims (design D5) add the model, the effort and the policy
flags, and the final answer is the CLI's own structured output. It is validated against the
schema and becomes findings with no model step; an answer off the schema fails the trial, and
nothing repairs it.
"""

import dataclasses
import hashlib
import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from rbench.engines.catalog import catalog_path
from rbench.engines.declaration import DeclarationError, Engine
from rbench.engines.pins import MODELS

NAME = "plain"
ROLE = "reviewer"
PROMPT_FILE = "prompt.md"
SCHEMA_FILE = "findings.schema.json"
SCHEMA_INPUT = "findings_schema_sha256"
CATALOG_INPUT = "codex_model_catalog_sha256"
# File reading and the shell, nothing else (spec Plain baseline tools). Claude Code in `-p` denies
# a tool that asks for permission, so Bash needs the bypass; the container is the sandbox (D6).
CLAUDE_TOOLS = "Read,Grep,Glob,Bash"
# Design D12: no web search, goal, image or apps tools, no subagents, no `request_user_input`.
CODEX_SWITCHES = (
    'web_search="disabled"',
    "features.goals=false",
    "features.view_image=false",
    "features.image_generation=false",
    "features.apps=false",
    "agents.enabled=false",
    "tools.experimental_request_user_input.enabled=false",
)
# Where the launch's files go in the agent container: outside the reviewed repository.
FILES_DIR = "/home/node/plain"
SCHEMA_PATH = f"{FILES_DIR}/{SCHEMA_FILE}"
CATALOG_PATH = f"{FILES_DIR}/model-catalog.json"
OUTPUT_PATH = f"{FILES_DIR}/answer.json"
SEVERITIES = ("must_fix", "should_fix", "nit")
# The JSON Schema keywords `validate` implements; a schema using any other is refused on load.
_KEYWORDS = {
    "type",
    "description",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
}
_TYPES = {"object": dict, "array": list, "string": str, "integer": int}


class PlainOutputError(ValueError):
    """The final answer does not follow the findings schema: the trial is `failed`."""


@dataclass(frozen=True)
class Launch:
    """One `plain` session: run `argv` in the trial's repository through the shims."""

    cli: str
    argv: tuple[str, ...]
    stdin: str  # the prompt
    files: dict[str, bytes]  # container path -> content, written before the launch
    output: str | None  # container path of the final answer; None: the CLI's stdout


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path, where: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as e:
        raise DeclarationError(f"{where}: cannot read {path}: {e.strerror}") from None


def check_schema(schema: object, at: str = "$") -> None:
    """Refuse a schema that `validate` would not check exactly as written."""
    if not (isinstance(schema, dict) and schema.get("type") in _TYPES):
        raise ValueError(f"{at}: expected a schema with a type of {', '.join(_TYPES)}")
    if unknown := schema.keys() - _KEYWORDS:
        raise ValueError(f"{at}: unsupported keywords {', '.join(sorted(unknown))}")
    if schema["type"] == "object":
        properties = schema.get("properties")
        if not (isinstance(properties, dict) and schema.get("additionalProperties") is False):
            raise ValueError(f"{at}: an object needs properties and additionalProperties false")
        if set(schema.get("required", [])) != properties.keys():
            raise ValueError(f"{at}: every property must be required (structured outputs)")
        for key, sub in properties.items():
            check_schema(sub, f"{at}.{key}")
    elif schema["type"] == "array":
        check_schema(schema.get("items"), f"{at}[]")
    if "enum" in schema and not all(type(v) is _TYPES[schema["type"]] for v in schema["enum"]):
        raise ValueError(f"{at}: enum values must have the schema's type")


def validate(schema: dict, value: object, at: str = "$") -> None:
    """Check `value` against a schema `check_schema` accepted. Errors name the place and the
    rule, never the value: it is engine output, and may quote a hidden task."""
    kind = schema["type"]
    if type(value) is not _TYPES[kind]:  # `type`, so that a bool is no integer
        raise PlainOutputError(f"{at}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise PlainOutputError(f"{at}: not one of {', '.join(map(str, schema['enum']))}")
    if kind == "object":
        if value.keys() != schema["properties"].keys():
            raise PlainOutputError(
                f"{at}: expected exactly the fields {', '.join(sorted(schema['properties']))}"
            )
        for key, sub in schema["properties"].items():
            validate(sub, value[key], f"{at}.{key}")
    elif kind == "array":
        for i, item in enumerate(value):
            validate(schema["items"], item, f"{at}[{i}]")


def _schema(data: bytes, where: str) -> dict:
    try:
        schema = json.loads(data)
        check_schema(schema)
    except (ValueError, RecursionError) as e:
        raise DeclarationError(
            f"{where}: {SCHEMA_FILE}: not a findings schema rbench checks: {e}"
        ) from None
    return schema


def bind(engine: Engine, engines_dir: Path) -> Engine:
    """Check the `plain` declaration against `engines_dir/plain/` and attach the input hashes."""
    where = f"{engines_dir / (NAME + '.toml')}: engine {NAME!r}"
    if engine.pin_kind != "sha256":
        raise DeclarationError(f"{where}: pin.kind must be sha256, the hash of {PROMPT_FILE}")
    role = engine.roles.get(ROLE)
    if engine.roles.keys() != {ROLE} or role is None or (role.cli, role.delivery) != (None, "flag"):
        raise DeclarationError(
            f"{where}: needs exactly one role {ROLE!r}, with no cli (the model picks it) and"
            " delivery = 'flag'"
        )
    directory = Path(engines_dir) / NAME
    prompt = _sha256(_read(directory / PROMPT_FILE, where))
    if prompt != engine.pin_ref:
        raise DeclarationError(
            f"{where}: {directory / PROMPT_FILE} has SHA-256 {prompt}, but pin.ref is"
            f" {engine.pin_ref}; set pin.ref to the new hash"
        )
    schema = _read(directory / SCHEMA_FILE, where)
    _schema(schema, where)
    inputs = {cli: {SCHEMA_INPUT: _sha256(schema)} for cli in engine.clis}
    if "codex" in engine.clis:
        version = engine.clis["codex"]
        catalog = catalog_path(directory, version)
        if not catalog.is_file():
            raise DeclarationError(
                f"{where}: no Codex model catalog {catalog} for codex {version}; generate it with"
                f" `uv run python -m rbench.engines.catalog --codex-version {version} --image"
                " <agent image>`"
            )
        inputs["codex"][CATALOG_INPUT] = _sha256(_read(catalog, where))
    return dataclasses.replace(engine, inputs=inputs)


def launch(engines_dir: Path, settings: Mapping, clis: Mapping[str, str]) -> Launch:
    """The launch of the `plain` configuration with these frozen `settings`, for the engine's
    pinned `clis`. Refuses when a file no longer hashes to the settings: a run never mixes
    trials of two prompts, schemas or catalogs."""
    where = f"plain configuration {settings.get('roles')}"
    directory = Path(engines_dir) / NAME
    try:
        (model,) = settings["roles"][ROLE]["models"]
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{where}: not the settings of a plain configuration") from None
    cli = MODELS[model]
    inputs = settings.get("inputs", {})
    files = {PROMPT_FILE: (directory / PROMPT_FILE, settings["version"])}
    files[SCHEMA_FILE] = (directory / SCHEMA_FILE, inputs.get(SCHEMA_INPUT))
    if cli == "codex":
        files["catalog"] = (catalog_path(directory, clis["codex"]), inputs.get(CATALOG_INPUT))
    data = {}
    for name, (path, want) in files.items():
        data[name] = _read(path, where)
        if want is None or _sha256(data[name]) != want:
            raise ValueError(
                f"{where}: {path} does not match the run's plan; the plan was frozen with"
                " another version of it, so finish the run with that version or start a new run"
            )
    prompt, schema = data[PROMPT_FILE].decode(), data[SCHEMA_FILE]
    if cli == "claude":
        compact = json.dumps(json.loads(schema), separators=(",", ":"))
        argv = ("claude", "-p", "--tools", CLAUDE_TOOLS, "--permission-mode", "bypassPermissions")
        argv += ("--output-format", "json", "--json-schema", compact)
        return Launch(cli, argv, prompt, {}, None)
    argv = ("codex", "exec", "--sandbox", "danger-full-access", "--output-schema", SCHEMA_PATH)
    argv += ("--output-last-message", OUTPUT_PATH)
    for switch in (*CODEX_SWITCHES, f"model_catalog_json={json.dumps(CATALOG_PATH)}"):
        argv += ("-c", switch)
    files_out = {SCHEMA_PATH: schema, CATALOG_PATH: data["catalog"]}
    return Launch(cli, (*argv, "-"), prompt, files_out, OUTPUT_PATH)


def findings(launch: Launch, answer: str, schema: dict) -> list[dict]:
    """The trial's findings from its final answer: Claude's `--output-format json` result on
    stdout, or the file Codex wrote with `--output-last-message`. Raises `PlainOutputError`
    when the answer does not follow `schema` (the parsed `findings.schema.json`)."""
    try:
        value = json.loads(answer)
    except (ValueError, RecursionError):
        raise PlainOutputError(f"{launch.cli}: the final answer is not JSON") from None
    if launch.cli == "claude":
        if not (isinstance(value, dict) and value.get("type") == "result"):
            raise PlainOutputError("claude: stdout is not a result object")
        if value.get("is_error") is not False or value.get("subtype") != "success":
            raise PlainOutputError(f"claude: the session ended with {value.get('subtype')!r}")
        if "structured_output" not in value:
            raise PlainOutputError("claude: the result has no structured_output")
        value = value["structured_output"]
    validate(schema, value)
    result = []
    for i, f in enumerate(value["findings"]):
        at = f"$.findings[{i}]"
        if not (1 <= f["start_line"] <= f["end_line"]):
            raise PlainOutputError(f"{at}: expected 1 <= start_line <= end_line")
        if not (f["file"].strip() and f["message"].strip()):
            raise PlainOutputError(f"{at}: file and message must not be blank")
        keys = ("file", "start_line", "end_line", "message", "severity")
        result.append({k: f[k] for k in keys})
    return result


def check_launches(records: list[dict], launch: Launch) -> list[str]:
    """What in the shim log shows that the trial did not run `launch` as built: it must hold
    exactly one launch, of the same CLI and argv, with the D12 switches in the argv the CLI got
    (design D12: the adapter checks them in the argv log)."""
    launches = [r for r in records if r.get("kind") == "launch"]
    if len(launches) != 1:
        return [f"expected one launch in the shim log, found {len(launches)}"]
    (record,) = launches
    problems = []
    if record.get("cli") != launch.cli or record.get("argv") != list(launch.argv[1:]):
        problems.append(f"the launch is not the {launch.cli} argv the plain adapter built")
    if record.get("rewrites"):
        problems.append("the shim rewrote the plain launch's argv")
    final = record.get("final_argv") or []
    if launch.cli == "claude":
        pairs = [("--tools", CLAUDE_TOOLS), ("--json-schema", launch.argv[-1])]
    else:
        pairs = [("--output-schema", SCHEMA_PATH), *(("-c", s) for s in CODEX_SWITCHES)]
    present = set(itertools.pairwise(final))
    problems += [f"the CLI did not get {a} {b}" for a, b in pairs if (a, b) not in present]
    return problems
