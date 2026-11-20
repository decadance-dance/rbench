import pytest

from rbench.engines.declaration import (
    EFFORTS,
    DeclarationError,
    Delivery,
    Setting,
    load_engine,
)
from rbench.engines.pins import MODELS, FloatingRefError, check_ref, model_cli

COMMIT = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "ab" * 32
WHERE = "engine 'demo'"

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
        "claude", flags=("--model", "claude-sonnet-5-5", "--effort", "max")
    )
    assert engine.delivery("reviewer", Setting("gpt-6.2-sol", "high")) == Delivery(
        "codex", flags=("-m", "gpt-6.2-sol", "-c", 'model_reasoning_effort="high"')
    )


def test_config_delivery_writes_codex_config_keys_and_no_flags(tmp_path):
    engine = load(tmp_path)

    assert engine.delivery("panel", Setting("gpt-6.2-astra", "xhigh")) == Delivery(
        "codex", config=(("model", "gpt-6.2-astra"), ("model_reasoning_effort", "xhigh"))
    )


def test_role_without_cli_follows_the_models_cli(tmp_path):
    engine = load(tmp_path)

    assert engine.delivery("reviewer", Setting("claude-fable-5-2", "high")).cli == "claude"
    assert engine.delivery("reviewer", Setting("gpt-6.2-sol", "high")).cli == "codex"


def test_model_of_the_other_cli_is_rejected_for_a_fixed_cli(tmp_path):
    engine = load(tmp_path)

    with pytest.raises(DeclarationError, match="'coordinator': runs on claude"):
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
    with pytest.raises(DeclarationError, match="'reviewer': default: .* needs a pinned codex CLI"):
        load(
            tmp_path,
            **{
                'codex = "0.167.0"': "",
                'cli = "codex"\ndelivery = "config"': 'cli = "claude"\ndelivery = "flag"',
                '"gpt-6.2-sol"': '"claude-opus-5-5"',
            },
        )


@pytest.mark.parametrize("cli, effort", [("claude", "ultra"), ("claude", "banana")])
def test_effort_the_cli_does_not_accept_is_rejected_in_a_declaration(tmp_path, cli, effort):
    with pytest.raises(DeclarationError) as error:
        load(tmp_path, **{'effort = "high"': f'effort = "{effort}"'})

    message = str(error.value)
    assert "engine.toml: engine 'demo': role 'coordinator': default:" in message
    assert f"effort {effort!r} is not one {cli} accepts" in message


@pytest.mark.parametrize(
    "role, model", [("coordinator", "claude-opus-5-5"), ("panel", "gpt-6.2-sol")]
)
@pytest.mark.parametrize("effort", ["banana", "", "minimal", "HIGH", 3])
def test_effort_the_cli_does_not_accept_is_rejected_on_override(tmp_path, role, model, effort):
    engine = load(tmp_path)

    with pytest.raises(DeclarationError, match=f"role '{role}': effort"):
        engine.delivery(role, Setting(model, effort))


def test_every_effort_each_cli_accepts_is_delivered(tmp_path):
    engine = load(tmp_path)

    for effort in EFFORTS["claude"]:
        assert (
            engine.delivery("coordinator", Setting("claude-opus-5-5", effort)).flags[-1] == effort
        )
    for effort in EFFORTS["codex"]:
        assert engine.delivery("panel", Setting("gpt-6.2-sol", effort)).config[-1][1] == effort


@pytest.mark.parametrize("key", ["unknown_key = 1\n", "[extra]\n"])
def test_unknown_keys_are_rejected(tmp_path, key):
    with pytest.raises(DeclarationError, match="unknown"):
        load(tmp_path, DECLARATION.replace('name = "demo"\n', f'name = "demo"\n{key}', 1))


def test_invalid_toml_names_the_file(tmp_path):
    with pytest.raises(DeclarationError, match="engine.toml: not a valid UTF-8 TOML file"):
        load(tmp_path, "name = ")


def test_undecodable_file_is_rejected_naming_the_file(tmp_path):
    path = tmp_path / "engine.toml"
    path.write_bytes(b'name = "\xff"\n')

    with pytest.raises(DeclarationError, match="engine.toml: not a valid UTF-8 TOML file"):
        load_engine(path)


def test_engine_pinned_by_its_cli_version(tmp_path):
    engine = load(tmp_path, **{f'kind = "git", ref = "{COMMIT}"': 'kind = "cli", ref = "codex"'})

    assert engine.version == "0.167.0"
    with pytest.raises(DeclarationError, match="pin.ref names CLI 'npm'"):
        load(tmp_path, **{f'kind = "git", ref = "{COMMIT}"': 'kind = "cli", ref = "npm"'})


@pytest.mark.parametrize(
    "kind, ref",
    [("git", COMMIT), ("git", "refs/tags/v1.4.0"), ("git", "refs/tags/release-2026-11-21")]
    + [("git", "refs/tags/team/v2"), ("version", "0.14.0"), ("version", "1.2.3+build.7")]
    + [("version", "1.0.0-rc.1+exp.sha.5114f85"), ("version", "2.1.316")]
    + [("sha256", "ab" * 32), ("image", f"rbench/demo@{DIGEST}")]
    + [("image", f"rbench/demo:1.0@{DIGEST}"), ("image", f"runner:latest@{DIGEST}")]
    + [("image", f"registry.example/latest-tools/runner@{DIGEST}")],
)
def test_exact_refs_are_accepted(kind, ref):
    assert check_ref(WHERE, kind, ref) == ref


@pytest.mark.parametrize(
    "kind, ref",
    # A version-shaped name may be a branch; a tag needs its namespace.
    [("git", "v1.2.3"), ("git", "release-2026-11-21"), ("git", "main"), ("git", "HEAD")]
    + [("git", "latest"), ("git", COMMIT[:7]), ("git", "feature/x"), ("git", "refs/heads/main")]
    + [("git", "refs/tags/latest"), ("git", "refs/tags/"), ("git", "refs/tags/a..b")]
    + [("git", "refs/tags/v1.lock"), ("git", "refs/tags/v1/"), ("git", "refs/tags/a b")]
    # git check-ref-format rejects these.
    + [("git", "refs/tags/bad" + chr(1) + "tag"), ("git", "refs/tags/del" + chr(127))]
    + [("git", f"refs/tags/a{c}b") for c in "~^:?*[\\"]
    + [("git", "refs/tags/.hidden"), ("git", "refs/tags/a/.b"), ("git", "refs/tags/a//b")]
    + [("git", "refs/tags/a/b.lock/c"), ("git", "refs/tags/x@{1}"), ("git", "refs/tags/v1.")]
    + [("git", COMMIT[:39]), ("git", COMMIT + "0"), ("git", "g" * 40)]
    + [("version", "^0.14.0"), ("version", "~0.14"), ("version", "0.14"), ("version", "v1.2.3")]
    + [("version", ">=0.14.0"), ("version", "0.x"), ("version", "latest"), ("version", "*")]
    + [("version", "01.2.3"), ("version", "1.2.3 - 1.4.0"), ("version", 14)]
    # Arabic-Indic digits are digits to Unicode but not to SemVer.
    + [("version", "1.2.3-1" + chr(0x660)), ("version", chr(0x661) + ".2.3")]
    + [("sha256", "abc"), ("image", "rbench/demo:latest"), ("image", "rbench/demo:1.0")]
    + [("image", "rbench/demo"), ("image", "rbench/demo@sha256:abc")],
)
def test_floating_refs_are_rejected_naming_engine_and_ref(kind, ref):
    with pytest.raises(FloatingRefError) as error:
        check_ref(WHERE, kind, ref)

    assert WHERE in str(error.value)
    assert repr(ref) in str(error.value)


def test_commit_id_in_either_case_is_accepted_and_lowercased(tmp_path):
    assert check_ref(WHERE, "git", COMMIT.upper()) == COMMIT

    assert load(tmp_path, **{COMMIT: COMMIT.upper()}).pin_ref == COMMIT


def test_non_ascii_tag_is_rejected_naming_the_ascii_limit():
    # git itself accepts this tag; rbench restricts tags to printable ASCII.
    with pytest.raises(FloatingRefError, match="printable ASCII"):
        check_ref(WHERE, "git", "refs/tags/release" + chr(160) + "candidate")


@pytest.mark.parametrize(
    "model, cli",
    # Every model design D13 screens, and the O1 judge and second judge.
    [("claude-fable-5-2", "claude"), ("claude-opus-5-5", "claude"), ("claude-sonnet-5-5", "claude")]
    + [("gpt-6.2-sol", "codex"), ("gpt-6.2-astra", "codex")],
)
def test_listed_full_model_ids_map_to_their_cli(model, cli):
    assert model_cli(WHERE, model) == cli == MODELS[model]


@pytest.mark.parametrize(
    "model",
    ["opus", "sonnet", "fable", "opus[1m]", "claude-opus-5", "claude-opus-latest"]
    + ["claude-sonnet-4-5", "gpt-5", "gpt-4o-2024-08-06", "gpt-5-chat-latest", "default", ""]
    + [None, ["claude-opus-5-5"]],
)
def test_model_aliases_and_unlisted_ids_are_rejected_asking_for_the_full_id(model):
    with pytest.raises(FloatingRefError, match="give the full model id") as error:
        model_cli(WHERE, model)

    assert WHERE in str(error.value) and repr(model) in str(error.value)
    assert "models.toml" in str(error.value)


def test_floating_refs_in_a_declaration_name_file_engine_field_and_ref(tmp_path):
    prefix = "engine.toml: engine 'demo': "
    cases = {
        f'ref = "{COMMIT}"': ('ref = "main"', "pin.ref 'main'"),
        'claude = "2.1.316"': ('claude = "latest"', "cli.claude 'latest'"),
        'codex = "0.167.0"': ('codex = "^0.167"', "cli.codex '^0.167'"),
        '"claude-opus-5-5"': ('"opus"', "role 'coordinator': default: model 'opus'"),
    }
    for old, (new, named) in cases.items():
        with pytest.raises(FloatingRefError) as error:
            load(tmp_path, **{old: new})
        assert prefix in str(error.value) and named in str(error.value)
