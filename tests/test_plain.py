"""The `plain` baseline engine (spec engine-configurations, Plain baseline; design D12)."""

import dataclasses
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from conftest import REPO, FakeBin
from test_ledger import BUDGET, PLAIN_TWO_MODELS, freeze, plan_json
from test_matrix import engines, matrix

from rbench.engines import catalog as catalog_module
from rbench.engines.catalog import CODEX_MODELS, OVERRIDES, CatalogError, generate, transform
from rbench.engines.declaration import DeclarationError, load_engine
from rbench.engines.plain import (
    CATALOG_INPUT,
    CATALOG_PATH,
    OUTPUT_PATH,
    SCHEMA_INPUT,
    SCHEMA_PATH,
    PlainOutputError,
    check_launches,
    findings,
    launch,
)
from rbench.ledger import PLAN_FILE, LedgerError, load_plan
from rbench.matrix.expansion import MatrixError, parse_matrix

ENGINES = REPO / "engines"
BUNDLED = Path(__file__).parent / "fixtures" / "codex-bundled-0.171.0.json"
CLAUDE, GPT = "claude-sonnet-5-5", "gpt-6.2-sol"
# Design D12, written out here so that the tests do not take them from the adapter.
D12_OVERRIDES = {
    "apply_patch_tool_type": None,
    "tool_mode": "direct",
    "experimental_supported_tools": [],
}
D12_SWITCHES = [
    'web_search="disabled"',
    "features.goals=false",
    "features.view_image=false",
    "features.image_generation=false",
    "features.apps=false",
    "agents.enabled=false",
    "tools.experimental_request_user_input.enabled=false",
]
FINDING = {
    "file": "src/app.py",
    "start_line": 3,
    "end_line": 4,
    "severity": "must_fix",
    "message": "Divides by a count that can be zero.",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def engines_dir(tmp_path: Path) -> Path:
    """A copy of the repository's engine declarations, safe to edit."""
    return Path(shutil.copytree(ENGINES, tmp_path / "engines"))


def configurations(engines_dir: Path) -> dict[str, dict]:
    """The settings of `plain` on one Claude and one OpenAI model, by model."""
    engine = load_engine(engines_dir / "plain.toml")
    entry = {"engine": "plain", "roles": {"reviewer": {"model": [CLAUDE, GPT], "effort": "high"}}}
    data = {"stage": "screening", "tasks": "core", "attempts": 1}
    data["rounds"] = [{"name": "r1", "budget": {"claude": 1, "codex": 1}, "entries": [entry]}]
    (r,) = parse_matrix(data, "m.toml", {"plain": engine}).rounds
    return {c.roles[0][1].models[0]: {"id": c.id, "settings": c.settings} for c in r.configurations}


def test_declaration_pins_the_prompt_and_hashes_the_schema_and_catalog():
    engine = load_engine(ENGINES / "plain.toml")
    assert engine.version == sha256(ENGINES / "plain" / "prompt.md")
    schema = sha256(ENGINES / "plain" / "findings.schema.json")
    catalog = sha256(ENGINES / "plain" / f"codex-catalog-{engine.clis['codex']}.json")
    assert engine.inputs == {
        "claude": {SCHEMA_INPUT: schema},
        "codex": {SCHEMA_INPUT: schema, CATALOG_INPUT: catalog},
    }


def test_committed_catalog_overrides_the_benchmark_models_only():
    engine = load_engine(ENGINES / "plain.toml")
    path = ENGINES / "plain" / f"codex-catalog-{engine.clis['codex']}.json"
    models = {m["slug"]: m for m in json.loads(path.read_text())["models"]}
    assert OVERRIDES == D12_OVERRIDES
    for slug in ("gpt-6.2-sol", "gpt-6.2-astra"):
        assert {k: models[slug][k] for k in D12_OVERRIDES} == D12_OVERRIDES
    assert any(m.get("apply_patch_tool_type") is not None for m in models.values())


def test_same_prompt_and_schema_through_both_clis(engines_dir: Path):
    by_model = configurations(engines_dir)
    claude, codex = by_model[CLAUDE]["settings"], by_model[GPT]["settings"]
    assert claude["version"] == codex["version"]
    assert claude["inputs"][SCHEMA_INPUT] == codex["inputs"][SCHEMA_INPUT]
    assert CATALOG_INPUT in codex["inputs"] and CATALOG_INPUT not in claude["inputs"]
    clis = load_engine(engines_dir / "plain.toml").clis
    a, b = launch(engines_dir, claude, clis), launch(engines_dir, codex, clis)
    assert a.stdin == b.stdin == (engines_dir / "plain" / "prompt.md").read_text()
    schema = json.loads((engines_dir / "plain" / "findings.schema.json").read_text())
    assert json.loads(a.argv[a.argv.index("--json-schema") + 1]) == schema
    assert json.loads(b.files[SCHEMA_PATH]) == schema


def test_claude_argv_has_only_the_reading_and_shell_tools_and_the_schema(engines_dir: Path):
    settings = configurations(engines_dir)[CLAUDE]["settings"]
    run = launch(engines_dir, settings, load_engine(engines_dir / "plain.toml").clis)
    assert run.cli == "claude" and run.argv[:2] == ("claude", "-p")
    i = run.argv.index("--tools")
    assert run.argv[i + 1] == "Read,Grep,Glob,Bash"
    assert run.argv[i + 2 : i + 6] == ("--permission-mode", "dontAsk", "--allowedTools", "Bash")
    assert "bypassPermissions" not in run.argv
    assert "--json-schema" in run.argv and run.output is None and run.files == {}
    # The model and effort come from the shim (delivery by flag), never from the adapter.
    assert "--model" not in run.argv and "--effort" not in run.argv


def test_codex_argv_has_the_schema_the_catalog_and_the_switches(engines_dir: Path):
    settings = configurations(engines_dir)[GPT]["settings"]
    engine = load_engine(engines_dir / "plain.toml")
    run = launch(engines_dir, settings, engine.clis)
    argv = list(run.argv)
    assert argv[:2] == ["codex", "exec"] and argv[-1] == "-"
    assert argv[argv.index("--output-schema") + 1] == SCHEMA_PATH
    assert argv[argv.index("--output-last-message") + 1] == run.output == OUTPUT_PATH
    configs = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert configs == [*D12_SWITCHES, f'model_catalog_json="{CATALOG_PATH}"']
    catalog = engines_dir / "plain" / f"codex-catalog-{engine.clis['codex']}.json"
    assert run.files[CATALOG_PATH] == catalog.read_bytes()
    assert "-m" not in argv and "--model" not in argv


def test_editing_the_prompt_needs_a_new_pin_and_changes_every_plain_id(engines_dir: Path):
    before = configurations(engines_dir)
    prompt = engines_dir / "plain" / "prompt.md"
    old = sha256(prompt)
    prompt.write_text(prompt.read_text() + "\nBe brief.\n")
    with pytest.raises(DeclarationError, match="set pin.ref to the new hash"):
        load_engine(engines_dir / "plain.toml")
    declaration = engines_dir / "plain.toml"
    declaration.write_text(declaration.read_text().replace(old, sha256(prompt)))
    after = configurations(engines_dir)
    assert all(after[m]["id"] != before[m]["id"] for m in (CLAUDE, GPT))


def test_schema_change_moves_every_id_and_catalog_change_only_codex(engines_dir: Path):
    before = configurations(engines_dir)
    catalog = next((engines_dir / "plain").glob("codex-catalog-*.json"))
    catalog.write_text(catalog.read_text() + " ")
    after = configurations(engines_dir)
    assert after[CLAUDE]["id"] == before[CLAUDE]["id"]
    assert after[GPT]["id"] != before[GPT]["id"]
    schema = engines_dir / "plain" / "findings.schema.json"
    schema.write_text(schema.read_text() + " ")
    last = configurations(engines_dir)
    assert last[CLAUDE]["id"] != after[CLAUDE]["id"] and last[GPT]["id"] != after[GPT]["id"]


def test_launch_refuses_files_that_changed_since_the_plan(engines_dir: Path):
    settings = configurations(engines_dir)[GPT]["settings"]
    clis = load_engine(engines_dir / "plain.toml").clis
    next((engines_dir / "plain").glob("codex-catalog-*.json")).write_text("{}")
    with pytest.raises(ValueError, match="does not match the run's plan"):
        launch(engines_dir, settings, clis)
    # A plan frozen before the inputs joined the settings has none, so it is refused too.
    old = {k: v for k, v in settings.items() if k != "inputs"}
    with pytest.raises(ValueError, match="does not match the run's plan"):
        launch(engines_dir, old, clis)


def test_missing_catalog_refuses_with_the_command(engines_dir: Path):
    next((engines_dir / "plain").glob("codex-catalog-*.json")).unlink()
    with pytest.raises(DeclarationError, match="python -m rbench.engines.catalog"):
        load_engine(engines_dir / "plain.toml")


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ('pin = { kind = "git", ref = "0123456789abcdef0123456789abcdef01234567" }', "sha256"),
        ('delivery = "flag"\ncli = "claude"', "exactly one role"),
    ],
)
def test_declaration_shape_is_checked(engines_dir: Path, edit: str, message: str):
    path = engines_dir / "plain.toml"
    text = path.read_text()
    old = next(line for line in text.splitlines() if line.startswith(edit.split(" ")[0]))
    path.write_text(text.replace(old, edit))
    with pytest.raises(DeclarationError, match=message):
        load_engine(path)


def test_unsupported_schema_keyword_is_refused(engines_dir: Path):
    path = engines_dir / "plain" / "findings.schema.json"
    schema = json.loads(path.read_text())
    schema["properties"]["findings"]["minItems"] = 1
    path.write_text(json.dumps(schema))
    with pytest.raises(DeclarationError, match="minItems"):
        load_engine(engines_dir / "plain.toml")


def run_of(engines_dir: Path, model: str):
    settings = configurations(engines_dir)[model]["settings"]
    return launch(engines_dir, settings, load_engine(engines_dir / "plain.toml").clis)


def schema() -> dict:
    return json.loads((ENGINES / "plain" / "findings.schema.json").read_text())


def claude_result(structured: object, **fields: object) -> str:
    result = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
    return json.dumps(result | {"structured_output": structured} | fields)


def test_schema_valid_answers_become_findings(engines_dir: Path):
    want = [{k: FINDING[k] for k in ("file", "start_line", "end_line", "message", "severity")}]
    claude = run_of(engines_dir, CLAUDE)
    assert findings(claude, claude_result({"findings": [FINDING]}), schema()) == want
    codex = run_of(engines_dir, GPT)
    assert findings(codex, json.dumps({"findings": [FINDING]}), schema()) == want
    assert findings(codex, '{"findings": []}', schema()) == []


SECRET = "text only the engine wrote"


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ("not json", "not JSON"),
        ([FINDING], r"\$: expected object"),
        ({"findings": [FINDING], "summary": SECRET}, "expected exactly the fields findings"),
        ({"findings": [FINDING | {"severity": SECRET}]}, "severity: not one of"),
        ({"findings": [FINDING | {"start_line": True}]}, "start_line: expected integer"),
        ({"findings": [FINDING | {"start_line": 5}]}, "start_line <= end_line"),
        ({"findings": [FINDING | {"start_line": 0}]}, "start_line <= end_line"),
        ({"findings": [{k: v for k, v in FINDING.items() if k != "file"}]}, "exactly the fields"),
        ({"findings": [FINDING | {"message": " "}]}, "must not be blank"),
    ],
)
def test_off_schema_answer_fails_the_trial_without_quoting_it(engines_dir, answer, message):
    codex = run_of(engines_dir, GPT)
    text = answer if isinstance(answer, str) else json.dumps(answer)
    with pytest.raises(PlainOutputError, match=message) as error:
        findings(codex, text, schema())
    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        (json.dumps({"type": "assistant"}), "not a result object"),
        (claude_result(None, subtype="error_max_turns", is_error=True), "error_max_turns"),
        (json.dumps({"type": "result", "subtype": "success", "is_error": False}), "no structured"),
        (claude_result({"findings": "none"}), "expected array"),
    ],
)
def test_claude_result_without_valid_structured_output_fails(engines_dir, stdout, message):
    with pytest.raises(PlainOutputError, match=message):
        findings(run_of(engines_dir, CLAUDE), stdout, schema())


def test_repeated_key_anywhere_fails_the_answer(engines_dir: Path):
    codex, claude = run_of(engines_dir, GPT), run_of(engines_dir, CLAUDE)
    with pytest.raises(PlainOutputError, match="repeats a key"):
        findings(codex, '{"findings": null, "findings": []}', schema())
    nested = claude_result({"findings": []})[:-1] + ', "structured_output": {"findings": []}}'
    with pytest.raises(PlainOutputError, match="repeats a key"):
        findings(claude, nested, schema())
    inner = json.dumps(FINDING)[:-1] + ', "message": "again"}'
    with pytest.raises(PlainOutputError, match="repeats a key"):
        findings(codex, '{"findings": [' + inner + "]}", schema())


@pytest.mark.parametrize(
    ("subtype", "message"),
    [
        (SECRET, "ended with an unknown subtype$"),
        (["error_max_turns"], "ended with an unknown subtype$"),
        ("error_max_structured_output_retries", "subtype error_max_structured_output_retries$"),
    ],
)
def test_failed_claude_session_names_only_a_known_subtype(engines_dir, subtype, message):
    stdout = claude_result(None, subtype=subtype, is_error=True)
    with pytest.raises(PlainOutputError, match=message) as error:
        findings(run_of(engines_dir, CLAUDE), stdout, schema())
    assert SECRET not in str(error.value)


def log_of(run, **changes: object) -> list[dict]:
    """A shim log with one launch of `run`, as the shim records it."""
    final = ["--strict-config", *run.argv[1:]] if run.cli == "codex" else list(run.argv[1:])
    record = {"kind": "launch", "cli": run.cli, "argv": list(run.argv[1:]), "final_argv": final}
    record |= {"rewrites": []} | changes
    return [{"seq": 1, "launch": 1, **record}, {"seq": 2, "launch": 1, "kind": "exit"}]


@pytest.mark.parametrize("model", [CLAUDE, GPT])
def test_argv_log_check(engines_dir: Path, model: str):
    run = run_of(engines_dir, model)
    assert check_launches(log_of(run), run) == []
    assert check_launches(log_of(run) * 2, run) == ["expected one launch in the shim log, found 2"]
    dropped = [a for a in run.argv[1:] if a not in ("features.goals=false", "Read,Grep,Glob,Bash")]
    problems = check_launches(log_of(run, final_argv=dropped), run)
    assert problems and all("did not get" in p for p in problems)
    assert "not the" in check_launches(log_of(run, argv=dropped), run)[0]


@pytest.mark.parametrize(
    ("model", "pair"),
    [
        (GPT, ["-c", 'model_catalog_json="/home/node/plain/model-catalog.json"']),
        (GPT, ["--output-schema", "/home/node/plain/findings.schema.json"]),
        (GPT, ["-c", 'web_search="disabled"']),
        (CLAUDE, ["--permission-mode", "dontAsk"]),
        (CLAUDE, ["--allowedTools", "Bash"]),
    ],
)
def test_argv_log_check_needs_each_pair_in_the_argv_the_cli_got(engines_dir, model, pair):
    run = run_of(engines_dir, model)
    final = list(run.argv[1:])
    i = next(i for i in range(len(final)) if final[i : i + 2] == pair)
    del final[i : i + 2]
    assert check_launches(log_of(run, final_argv=final), run) == [
        f"the CLI did not get {' '.join(pair)}"
    ]


def test_plain_refuses_a_model_set_for_its_reviewer(engines_dir: Path):
    plain = load_engine(engines_dir / "plain.toml")
    entry = {"engine": "plain", "roles": {"reviewer": {"models": [CLAUDE, "claude-opus-5-5"]}}}
    data = {"stage": "screening", "tasks": "core", "attempts": 1}
    data["rounds"] = [{"name": "r1", "budget": {"claude": 1}, "entries": [entry]}]
    with pytest.raises(MatrixError, match=r"one model per trial.*model = \[\.\.\.\]"):
        parse_matrix(data, "m.toml", {"plain": plain})
    entry["roles"]["reviewer"]["models"] = [CLAUDE]
    (r,) = parse_matrix(data, "m.toml", {"plain": plain}).rounds
    assert len(r.configurations) == 1


def bound(name: str, inputs: dict) -> dict:
    """The synthetic engines of test_matrix, with `inputs` on engine `name`."""
    engines_ = engines()
    engines_[name] = dataclasses.replace(engines_[name], inputs=inputs)
    return engines_


SCHEMA_HASH, CATALOG_HASH = "1" * 64, "2" * 64
PLAIN_INPUTS = {
    "claude": {SCHEMA_INPUT: SCHEMA_HASH},
    "codex": {SCHEMA_INPUT: SCHEMA_HASH, CATALOG_INPUT: CATALOG_HASH},
}


def test_plan_holds_plain_inputs_per_provider(tmp_path: Path):
    engines_ = bound("plain", PLAIN_INPUTS)
    m = matrix(PLAIN_TWO_MODELS, budget=BUDGET, engines_=engines_)
    plan = freeze(tmp_path, m, engines_=engines_)  # freeze_plan reads its plan back
    assert [c.settings["inputs"] for c in plan.configurations] == [
        {SCHEMA_INPUT: SCHEMA_HASH},
        {SCHEMA_INPUT: SCHEMA_HASH, CATALOG_INPUT: CATALOG_HASH},
    ]


@pytest.mark.parametrize(
    ("name", "inputs"),
    [
        ("panel", {"claude": {"arbitrary": SCHEMA_HASH}, "codex": {"arbitrary": SCHEMA_HASH}}),
        ("plain", {"claude": {SCHEMA_INPUT: SCHEMA_HASH, CATALOG_INPUT: CATALOG_HASH}}),
        ("plain", {"claude": {SCHEMA_INPUT: SCHEMA_HASH}, "codex": {SCHEMA_INPUT: SCHEMA_HASH}}),
        ("plain", {"claude": {"arbitrary": SCHEMA_HASH}, "codex": PLAIN_INPUTS["codex"]}),
    ],
)
def test_plan_refuses_inputs_that_are_not_plains(tmp_path: Path, name: str, inputs: dict):
    engines_ = bound(name, inputs)
    m = matrix({"engine": name, "default": True}, budget=BUDGET, engines_=engines_)
    if name == "plain":
        m = matrix(
            {"engine": "plain", "roles": {"reviewer": {"model": ["claude-opus-5-5", GPT]}}},
            budget=BUDGET,
            engines_=engines_,
        )
    with pytest.raises(LedgerError, match="settings.inputs: not the input files"):
        freeze(tmp_path, m, engines_=engines_)


def test_plan_refuses_a_plain_model_set(tmp_path: Path):
    run_dir, plan = plan_json(tmp_path)
    plan["configurations"][0]["settings"]["roles"]["reviewer"]["models"] = [CLAUDE, GPT]
    (run_dir / PLAN_FILE).write_text(json.dumps(plan))
    with pytest.raises(LedgerError, match="plain runs one model per configuration"):
        load_plan(run_dir)


# The catalog generator, on a recorded `codex debug models --bundled` (0.171.0, trimmed).


def bundled_scenario(fake_bin: FakeBin, version: str = "0.171.0") -> None:
    replies = [
        {"match": "--version", "stdout": f"codex-cli {version}\n"},
        {"match": "debug models --bundled", "stdout": BUNDLED.read_text()},
    ]
    fake_bin.scenario("codex", replies=replies)


def test_generate_overrides_exactly_the_d12_fields(fake_bin: FakeBin):
    bundled_scenario(fake_bin)
    out = json.loads(generate(["codex"], "0.171.0"))
    before = {m["slug"]: m for m in json.loads(BUNDLED.read_text())["models"]}
    after = {m["slug"]: m for m in out["models"]}
    assert list(after) == list(before)
    for slug, model in after.items():
        if slug in CODEX_MODELS:
            assert model == before[slug] | D12_OVERRIDES
            assert before[slug]["tool_mode"] == "code_mode_only"
        else:
            assert model == before[slug]
    calls = [c["argv"] for c in fake_bin.calls()]
    assert calls == [["--version"], ["debug", "models", "--bundled"]]


def test_generate_refuses_another_version(fake_bin: FakeBin):
    bundled_scenario(fake_bin, "0.172.0")
    with pytest.raises(CatalogError, match="reports 'codex-cli 0.172.0'"):
        generate(["codex"], "0.171.0")


@pytest.mark.parametrize(
    ("bundled", "message"),
    [
        ("[]", "list of models"),
        ('{"models": [{"slug": "gpt-6.2-sol"}]}', "'gpt-6.2-astra' appears 0 times"),
        ('{"models": [], "models": []}', "repeated key"),
    ],
)
def test_transform_refuses_a_catalog_it_cannot_override(bundled: str, message: str):
    with pytest.raises(CatalogError, match=message):
        transform(bundled, "bundled")


def test_generator_runs_the_real_codex_offline_in_the_image(fake_bin: FakeBin, tmp_path: Path):
    replies = [
        {"match": "--version", "stdout": "codex-cli 0.171.0\n"},
        {"match": "debug models --bundled", "stdout": BUNDLED.read_text()},
    ]
    fake_bin.scenario("docker", replies=replies)
    image = "rbench-agent@sha256:" + "ab" * 32
    argv = ["--codex-version", "0.171.0", "--image", image, "--out", str(tmp_path)]
    assert catalog_module.main(argv) == 0
    prefix = ["run", "--rm", "--network", "none", "sha256:" + "ab" * 32, "/usr/local/bin/codex"]
    assert [c["argv"] for c in fake_bin.calls()] == [
        [*prefix, "--version"],
        [*prefix, "debug", "models", "--bundled"],
    ]
    written = tmp_path / "codex-catalog-0.171.0.json"
    assert written.read_text() == transform(BUNDLED.read_text(), "bundled")
