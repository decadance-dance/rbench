import pytest

from rbench.engines.declaration import (
    DeclarationError,
    Delivery,
    Setting,
    load_engine,
)
from rbench.engines.pins import FloatingRefError, check_ref, model_cli

COMMIT = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "ab" * 32

# A synthetic engine with a Claude role (flags), a Codex role (config.toml) and a role whose CLI
# follows the model.
DECLARATION = f"""
name = "demo"
pin = {{ kind = "git", ref = "{COMMIT}" }}

[cli]
claude = "2.1.316"
codex = "0.167.0"

[roles.coordinator]
cli = "claude"
delivery = "flag"
default = {{ model = "claude-opus-5-5", effort = "high" }}

[roles.panel]
cli = "codex"
delivery = "config"
default = {{ model = "gpt-6.2-sol", effort = "medium" }}

[roles.reviewer]
delivery = "flag"
default = {{ model = "gpt-6.2-astra", effort = "xhigh" }}
"""


def load(tmp_path, text=DECLARATION, **replace):
    for old, new in replace.items():
        assert old in text
        text = text.replace(old, new)
    path = tmp_path / "engine.toml"
    path.write_text(text)
    return load_engine(path)


def test_flag_delivery_adds_shim_flags_per_cli(tmp_path):
    engine = load(tmp_path)

    assert engine.delivery("coordinator", Setting("claude-sonnet-5-5", "max")) == Delivery(
        flags=("--model", "claude-sonnet-5-5", "--effort", "max")
    )
    assert engine.delivery("reviewer", Setting("gpt-6.2-sol", "high")) == Delivery(
        flags=("-m", "gpt-6.2-sol", "-c", 'model_reasoning_effort="high"')
    )


def test_config_delivery_writes_codex_config_keys_and_no_flags(tmp_path):
    engine = load(tmp_path)

    assert engine.delivery("panel", Setting("gpt-6.2-astra", "xhigh")) == Delivery(
        config=(("model", "gpt-6.2-astra"), ("model_reasoning_effort", "xhigh"))
    )


def test_role_without_cli_follows_the_models_provider(tmp_path):
    engine = load(tmp_path)

    assert engine.cli_for("reviewer", "claude-fable-5-2") == "claude"
    assert engine.cli_for("reviewer", "gpt-6.2-sol") == "codex"


def test_model_of_the_other_provider_is_rejected_for_a_fixed_cli(tmp_path):
    engine = load(tmp_path)

    with pytest.raises(DeclarationError, match="'coordinator' runs on claude"):
        engine.delivery("coordinator", Setting("gpt-6.2-sol", "high"))


def test_config_delivery_on_claude_is_rejected(tmp_path):
    with pytest.raises(DeclarationError, match="config delivery needs cli = 'codex'"):
        load(
            tmp_path, **{'cli = "claude"\ndelivery = "flag"': 'cli = "claude"\ndelivery = "config"'}
        )


def test_unknown_delivery_is_rejected(tmp_path):
    with pytest.raises(DeclarationError, match="delivery must be flag or config"):
        load(tmp_path, **{'cli = "claude"\ndelivery = "flag"': 'cli = "claude"\ndelivery = "env"'})


def test_default_true_uses_the_declared_default_for_every_role(tmp_path):
    engine = load(tmp_path)

    assert engine.default_configuration() == {
        "coordinator": Setting("claude-opus-5-5", "high"),
        "panel": Setting("gpt-6.2-sol", "medium"),
        "reviewer": Setting("gpt-6.2-astra", "xhigh"),
    }


def test_role_without_a_default_is_rejected(tmp_path):
    no_default = 'delivery = "config"\ndefault = { model = "gpt-6.2-sol", effort = "medium" }'
    with pytest.raises(DeclarationError, match="role 'panel': missing default"):
        load(tmp_path, **{no_default: 'delivery = "config"'})


def test_default_needing_an_unpinned_cli_is_rejected(tmp_path):
    with pytest.raises(DeclarationError, match="'reviewer' needs a pinned codex CLI"):
        load(
            tmp_path,
            **{
                'codex = "0.167.0"': "",
                'cli = "codex"\ndelivery = "config"': 'cli = "claude"\ndelivery = "flag"',
                '"gpt-6.2-sol"': '"claude-opus-5-5"',
            },
        )


@pytest.mark.parametrize("key", ["unknown_key = 1\n", "[extra]\n"])
def test_unknown_keys_are_rejected(tmp_path, key):
    with pytest.raises(DeclarationError, match="unknown"):
        load(tmp_path, DECLARATION.replace('name = "demo"\n', f'name = "demo"\n{key}', 1))


def test_invalid_toml_names_the_file(tmp_path):
    with pytest.raises(DeclarationError, match="engine.toml: not valid TOML"):
        load(tmp_path, "name = ")


def test_engine_pinned_by_its_cli_version(tmp_path):
    engine = load(tmp_path, **{f'kind = "git", ref = "{COMMIT}"': 'kind = "cli", ref = "codex"'})

    assert engine.version == "0.167.0"
    with pytest.raises(DeclarationError, match="pin names CLI 'npm'"):
        load(tmp_path, **{f'kind = "git", ref = "{COMMIT}"': 'kind = "cli", ref = "npm"'})


@pytest.mark.parametrize(
    "kind, ref",
    [("git", COMMIT), ("git", "v1.4.0"), ("git", "1.4.0-rc.1"), ("version", "0.14.0")]
    + [("sha256", "ab" * 32), ("image", f"rbench/demo@{DIGEST}")]
    + [("image", f"rbench/demo:1.0@{DIGEST}")],
)
def test_exact_refs_are_accepted(kind, ref):
    assert check_ref("demo", kind, ref) == ref


@pytest.mark.parametrize(
    "kind, ref",
    [("git", "main"), ("git", "HEAD"), ("git", "latest"), ("git", "v1"), ("git", COMMIT[:7])]
    + [("git", "feature/x"), ("version", "^0.14.0"), ("version", "~0.14"), ("version", "0.14")]
    + [("version", ">=0.14.0"), ("version", "0.x"), ("version", "latest"), ("version", "*")]
    + [("version", "1.0.0-latest"), ("version", 14), ("sha256", "abc")]
    + [("image", "rbench/demo:latest"), ("image", "rbench/demo:1.0"), ("image", "rbench/demo")],
)
def test_floating_refs_are_rejected_naming_engine_and_ref(kind, ref):
    with pytest.raises(FloatingRefError) as error:
        check_ref("demo", kind, ref)

    assert "engine 'demo'" in str(error.value)
    assert repr(ref) in str(error.value)


@pytest.mark.parametrize(
    "model, cli",
    [("claude-opus-5-5", "claude"), ("claude-haiku-4-5-20251001", "claude")]
    + [("gpt-6.2-sol", "codex"), ("gpt-5", "codex")],
)
def test_full_model_ids_pick_their_cli(model, cli):
    assert model_cli("demo", model) == cli


@pytest.mark.parametrize(
    "model",
    ["opus", "sonnet", "fable", "opus[1m]", "claude-opus-5", "claude-opus-latest"]
    + ["gpt-5-chat-latest", "default", "", None],
)
def test_model_aliases_are_rejected_asking_for_the_full_id(model):
    with pytest.raises(FloatingRefError, match="give the full model id") as error:
        model_cli("demo", model)

    assert "engine 'demo'" in str(error.value) and repr(model) in str(error.value)


def test_floating_refs_in_a_declaration_are_rejected_naming_engine_and_ref(tmp_path):
    cases = {
        f'ref = "{COMMIT}"': ('ref = "main"', "engine ref 'main'"),
        'claude = "2.1.316"': ('claude = "latest"', "claude CLI version 'latest'"),
        'codex = "0.167.0"': ('codex = "^0.167"', "codex CLI version '^0.167'"),
        '"claude-opus-5-5"': ('"opus"', "model 'opus'"),
    }
    for old, (new, named) in cases.items():
        with pytest.raises(FloatingRefError, match="engine 'demo'") as error:
            load(tmp_path, **{old: new})
        assert named in str(error.value)
