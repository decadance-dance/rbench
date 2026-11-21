"""The trial images (design D3): an agent image per set of CLI versions, and the proxy image.

Images are built locally from `images/` and referenced as `<name>@sha256:<hex>`, the form the
ledger pins (`Image`). With Docker's classic image store a local build has no registry digest, so
the digest is the image ID, the content digest of the image's configuration, which names its
layers. Trials run an image by that ID, never by a tag.
"""

import re
import subprocess
from pathlib import Path

from rbench.engines.declaration import CLIS
from rbench.engines.pins import check_ref
from rbench.ledger.plan import CHECKOUT_ROOT, Image

AGENT_NAME = "rbench-agent"
PROXY_NAME = "rbench-proxy"
IMAGES_DIR = CHECKOUT_ROOT / "images"
_ID = re.compile(r"sha256:[0-9a-f]{64}")
# All that `<cli> --version` prints, e.g. "2.1.316 (Claude Code)" and "codex-cli 0.171.0".
_VERSION_OUTPUT = {"claude": "{} (Claude Code)", "codex": "codex-cli {}"}


class TrialError(RuntimeError):
    """Docker could not build, start, inspect or stop a trial's images or project."""


def docker(*argv: str, what: str) -> str:
    """Run `docker <argv>` and return its stdout; raise `TrialError` naming `what` on failure."""
    try:
        result = subprocess.run(
            ["docker", *argv],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except OSError as e:
        raise TrialError(f"{what}: cannot run docker: {e}") from e
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1:] or ["no output"]
        raise TrialError(f"{what}: docker {argv[0]} exited {result.returncode}: {detail[0]}")
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
    clis: dict[str, str], images_dir: Path = IMAGES_DIR, tag: str | None = None
) -> Image:
    """Build the agent image with exactly these CLI versions and read them back from it.

    `clis` maps each CLI the engine declares to its exact version; a CLI not in it is not
    installed. The result is what the ledger pins for an engine. `tag` replaces the default tag,
    which names the CLI versions; the reference is the digest either way.
    """
    if not (isinstance(clis, dict) and clis):
        raise TrialError("agent image: clis must name at least one CLI")
    for cli, version in clis.items():
        if cli not in CLIS:
            raise TrialError(f"agent image: unknown CLI {cli!r}; expected one of {CLIS}")
        check_ref("agent image", "version", version, f"clis.{cli}")
    # Docker tags allow [A-Za-z0-9_.-]; SemVer build metadata uses "+".
    tag = tag or "-".join(f"{cli}-{clis[cli]}" for cli in sorted(clis)).replace("+", "_")
    build_args = {f"{cli.upper()}_VERSION": clis.get(cli, "") for cli in CLIS}
    ref = _build(AGENT_NAME, tag, images_dir / "agent", build_args)
    for cli, version in sorted(clis.items()):
        what = f"reading the {cli} version in {ref}"
        out = docker("run", "--rm", "--network", "none", image_id(ref), cli, "--version", what=what)
        if out.strip() != _VERSION_OUTPUT[cli].format(version):
            raise TrialError(f"{ref}: expected {cli} {version}, the image reports {out.strip()!r}")
    return Image(ref, dict(clis))


def build_proxy_image(images_dir: Path = IMAGES_DIR, tag: str = "latest") -> str:
    """Build the proxy image, which carries the allowlist and squid.conf; return its reference."""
    return _build(PROXY_NAME, tag, images_dir / "proxy", {})
