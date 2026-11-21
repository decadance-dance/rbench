"""The trial images (design D3): an agent image per set of CLI versions, and the proxy image.

Images are built locally from `images/` and referenced as `<name>@sha256:<hex>`, the form the
ledger pins (`Image`). With Docker's classic image store a local build has no registry digest, so
the digest is the image ID, the content digest of the image's configuration, which names its
layers. Trials run an image by that ID, never by a tag.
"""

import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

from rbench.engines.declaration import CLIS
from rbench.engines.pins import check_ref
from rbench.ledger.plan import CHECKOUT_ROOT, Image

AGENT_NAME = "rbench-agent"
PROXY_NAME = "rbench-proxy"
IMAGES_DIR = CHECKOUT_ROOT / "images"
_ID = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
# All that `<cli> --version` prints, e.g. "2.1.316 (Claude Code)" and "codex-cli 0.171.0".
_VERSION_OUTPUT = {"claude": "{} (Claude Code)", "codex": "codex-cli {}", "rreview": "rreview {}"}
_PLUGIN_REPO = re.compile(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+")
PLUGINS_DIR = "/opt/rbench/plugins"
# Prints "<dir> <name> <version>" for each installed plugin, from its .claude-plugin/plugin.json;
# `.agents` there is the local Codex marketplace, not a plugin.
_PLUGINS_SCRIPT = (
    "const fs = require('fs'); const d = process.argv[1];"
    " for (const n of fs.existsSync(d) ? fs.readdirSync(d).filter((n) => !n.startsWith('.')).sort() : []) {"
    " const p = require(`${d}/${n}/.claude-plugin/plugin.json`);"
    " console.log(`${n} ${p.name} ${p.version}`); }"
)
# The yq the Dockerfile installs with a plugin (images/agent/Dockerfile).
_YQ_OUTPUT = "yq (https://github.com/mikefarah/yq/) version v4.44.3"
# The variable that carries the Claude token into a trial (spec trial-isolation, Credentials).
CLAUDE_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
REDACTED = "<redacted>"


class TrialError(RuntimeError):
    """Docker could not build, start, inspect or stop a trial's images or project."""


def host_env(claude_token: str | None = None) -> dict[str, str]:
    """The environment of a host-side docker call: the operator's, never with a Claude token the
    operator's shell exports; with `claude_token`, this trial's token."""
    env = {k: v for k, v in os.environ.items() if k != CLAUDE_TOKEN_ENV}
    return env | ({CLAUDE_TOKEN_ENV: claude_token} if claude_token is not None else {})


def redact(text: str, secrets: Iterable[str]) -> str:
    """`text` with every known credential value replaced."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def docker(
    *argv: str,
    what: str,
    env: Mapping[str, str] | None = None,
    secrets: Iterable[str] = (),
) -> str:
    """Run `docker <argv>` and return its stdout; raise `TrialError` naming `what` on failure.

    `env` is docker's whole environment, `host_env()` by default. `secrets` are redacted from the
    diagnostics an error carries.
    """
    secrets = tuple(secrets)
    try:
        result = subprocess.run(
            ["docker", *argv],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            env=dict(env) if env is not None else host_env(),
        )
    except OSError as e:
        raise TrialError(redact(f"{what}: cannot run docker: {e}", secrets)) from None
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1:] or ["no output"]
        message = f"{what}: docker {argv[0]} exited {result.returncode}: {detail[0]}"
        raise TrialError(redact(message, secrets))
    return result.stdout


def image_id(ref: str) -> str:
    """The `sha256:<hex>` digest of an image reference `<name>@sha256:<hex>`."""
    digest = ref.rpartition("@")[2]
    if not _ID.fullmatch(digest):
        raise TrialError(f"image {ref!r} is not referenced by digest")
    return digest


def _build(name: str, tag: str, context: Path, build_args: dict[str, str]) -> str:
    argv = ["build", "--quiet", "--tag", f"{name}:{tag}"]
    for key, value in sorted(build_args.items()):
        argv += ["--build-arg", f"{key}={value}"]
    out = docker(*argv, str(context), what=f"building {name}:{tag}").strip()
    if not _ID.fullmatch(out):
        raise TrialError(f"building {name}:{tag}: docker build printed no image id: {out[-200:]!r}")
    return f"{name}@{out}"


def build_agent_image(
    clis: dict[str, str],
    images_dir: Path = IMAGES_DIR,
    tag: str | None = None,
    plugin: tuple[str, str, str] | None = None,
    rreview: tuple[str, str] | None = None,
) -> Image:
    """Build the agent image with exactly these CLI versions and read them back from it.

    `clis` maps each CLI the engine declares to its exact version; a CLI not in it is not
    installed. `plugin` is the (https repository, commit, version) of the engine's Claude Code
    plugin: the image holds that commit in `/opt/rbench/plugins/<name>` (with yq), and its
    `plugin.json` must give that version. `rreview` is the (commit, release) of rreview to build
    into the image, for the rreview engine (`rbench.engines.rreview.RELEASES`); None installs no
    rreview. The result is what the ledger pins for an engine. `tag` replaces the default tag,
    which names the CLI versions (and the plugin's commit); the reference is the digest either way.
    """
    if not (isinstance(clis, dict) and clis):
        raise TrialError("agent image: clis must name at least one CLI")
    for cli, version in clis.items():
        if cli not in CLIS:
            raise TrialError(f"agent image: unknown CLI {cli!r}; expected one of {CLIS}")
        check_ref("agent image", "version", version, f"clis.{cli}")
    versions = dict(clis)
    build_args = {f"{cli.upper()}_VERSION": clis.get(cli, "") for cli in CLIS}
    build_args["RREVIEW_COMMIT"] = ""
    if rreview is not None:
        commit, release = rreview
        if not _COMMIT.fullmatch(commit):
            raise TrialError(f"agent image: rreview {commit!r} is not a full commit id")
        check_ref("agent image", "version", release, "rreview release")
        build_args["RREVIEW_COMMIT"] = commit
        versions["rreview"] = release
    # Docker tags allow [A-Za-z0-9_.-]; SemVer build metadata uses "+".
    default_tag = "-".join(f"{name}-{versions[name]}" for name in sorted(versions)).replace(
        "+", "_"
    )
    if plugin is not None:
        repo, commit, version = plugin
        if not (isinstance(repo, str) and _PLUGIN_REPO.fullmatch(repo)):
            raise TrialError(f"agent image: plugin repository {repo!r} is not an https URL")
        if not (isinstance(commit, str) and _COMMIT.fullmatch(commit)):
            raise TrialError(f"agent image: plugin commit {commit!r} is not a full commit id")
        check_ref("agent image", "version", version, "plugin version")
        build_args |= {"PLUGIN_REPO": repo, "PLUGIN_COMMIT": commit}
        tag = tag or f"{default_tag}-plugin-{commit[:12]}"
    tag = tag or default_tag
    ref = _build(AGENT_NAME, tag, images_dir / "agent", build_args)
    for name, version in sorted(versions.items()):
        what = f"reading the {name} version in {ref}"
        # The real binary: the shim on PATH runs only inside a prepared trial (design D5).
        real = f"/usr/local/bin/{name}"
        out = docker(
            "run", "--rm", "--network", "none", image_id(ref), real, "--version", what=what
        )
        if out.strip() != _VERSION_OUTPUT[name].format(version):
            raise TrialError(f"{ref}: expected {name} {version}, the image reports {out.strip()!r}")
    if plugin is not None:
        _check_plugin(ref, plugin[2])
    return Image(ref, dict(clis))


def _check_plugin(ref: str, version: str) -> None:
    """The built image holds one plugin, of `version`, in a directory named as the plugin, and
    the pinned yq."""
    run = ("run", "--rm", "--network", "none", image_id(ref))
    what = f"reading the plugin in {ref}"
    found = docker(*run, "node", "-e", _PLUGINS_SCRIPT, PLUGINS_DIR, what=what).split("\n")
    plugins = [line.split(" ") for line in found if line]
    if len(plugins) != 1 or len(plugins[0]) != 3 or plugins[0][0] != plugins[0][1]:
        raise TrialError(f"{ref}: expected one plugin in {PLUGINS_DIR}, found {found!r}")
    if plugins[0][2] != version:
        raise TrialError(
            f"{ref}: expected plugin {plugins[0][1]} {version}, the image has {plugins[0][2]}"
        )
    out = docker(*run, "/usr/local/bin/yq", "--version", what=f"reading the yq version in {ref}")
    if out.strip() != _YQ_OUTPUT:
        raise TrialError(f"{ref}: expected {_YQ_OUTPUT!r}, the image reports {out.strip()!r}")


def build_proxy_image(images_dir: Path = IMAGES_DIR, tag: str = "latest") -> str:
    """Build the proxy image, which carries the allowlist and squid.conf; return its reference."""
    return _build(PROXY_NAME, tag, images_dir / "proxy", {})
