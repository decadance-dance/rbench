"""The benchmark's Codex model catalog for `plain` (design D12), one file per Codex CLI version.

Some Codex tools come only from the model catalog, and no flag turns them off. The catalog is built
from the one the pinned release ships (`codex debug models --bundled`, a frozen input; plain
`codex debug models` fetches a remote one that can change under the same version). For every Codex
model in `models.toml` exactly these fields are overridden and nothing else changes:
`apply_patch_tool_type = null` (no edit tool), `tool_mode = "direct"` (plain functions, not a
JavaScript `exec` runner) and `experimental_supported_tools = []` (no clock, sleep or asynchronous
messaging tools).

The result is committed as `engines/plain/codex-catalog-<version>.json`; planning hashes it into
every `plain` configuration that runs a Codex model. Generate it from the real CLI in the agent
image, offline and without a login:

    uv run python -m rbench.engines.catalog --image rbench-agent@sha256:<id> --codex-version <v>

The image is one `build_agent_image({"codex": "<v>"})` built. Without `--image` the `codex` on
PATH runs; its version must still be the requested one.
"""

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from rbench.atomic import atomic_write
from rbench.engines.pins import MODELS, check_ref

CATALOG_FILE = "codex-catalog-{}.json"
OVERRIDES = {
    "apply_patch_tool_type": None,
    "tool_mode": "direct",
    "experimental_supported_tools": [],
}
REAL_CODEX = "/usr/local/bin/codex"  # the binary in the agent image; the shim needs a trial
TIMEOUT = 120  # seconds per CLI call
# The models the benchmark may run on Codex: every one gets the overrides.
CODEX_MODELS = tuple(sorted(m for m, cli in MODELS.items() if cli == "codex"))


class CatalogError(ValueError):
    """A catalog that cannot be generated or read."""


def catalog_path(plain_dir: Path, version: str) -> Path:
    return Path(plain_dir) / CATALOG_FILE.format(version)


def _no_repeats(pairs: list[tuple[str, object]]) -> dict:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise CatalogError(f"repeated key in {sorted(k for k in set(keys) if keys.count(k) > 1)}")
    return dict(pairs)


def transform(bundled: str, where: str) -> str:
    """The benchmark catalog made from the text of `codex debug models --bundled`."""
    try:
        data = json.loads(bundled, object_pairs_hook=_no_repeats)
    except (ValueError, RecursionError) as e:
        raise CatalogError(f"{where}: not a JSON catalog: {e}") from e
    models = data.get("models") if isinstance(data, dict) else None
    if not (isinstance(models, list) and all(isinstance(m, dict) for m in models)):
        raise CatalogError(f"{where}: expected an object with a list of models")
    for slug in CODEX_MODELS:
        found = [m for m in models if m.get("slug") == slug]
        if len(found) != 1:
            raise CatalogError(f"{where}: model {slug!r} appears {len(found)} times, expected once")
        found[0].update(OVERRIDES)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _run(argv: list[str], what: str) -> str:
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CatalogError(f"{what}: {e}") from e
    if result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or ["no output"])[-1]
        raise CatalogError(f"{what}: exited {result.returncode}: {detail}")
    return result.stdout


def generate(codex: Sequence[str], version: str) -> str:
    """Run `codex` (an argv prefix), check that it is `version`, and transform its bundled catalog."""
    check_ref("codex catalog", "version", version, "codex version")
    got = _run([*codex, "--version"], "codex --version").strip()
    if got != f"codex-cli {version}":
        raise CatalogError(f"expected codex-cli {version}, the CLI reports {got!r}")
    what = f"codex {version} debug models --bundled"
    return transform(_run([*codex, "debug", "models", "--bundled"], what), what)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rbench.engines.catalog", description=__doc__)
    parser.add_argument("--codex-version", required=True, help="the exact Codex CLI version")
    parser.add_argument("--image", help="agent image (<name>@sha256:<id>) to run the real codex in")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("engines/plain"),
        help="directory of the catalog files (default: engines/plain)",
    )
    args = parser.parse_args(argv)
    codex = ["codex"]
    if args.image:
        image = check_ref("codex catalog", "image", args.image, "--image")
        codex = ["docker", "run", "--rm", "--network", "none", image.rpartition("@")[2]]
        codex.append(REAL_CODEX)
    try:
        text = generate(codex, args.codex_version)
    except (CatalogError, ValueError) as e:
        print(f"rbench catalog: {e}", file=sys.stderr)
        return 1
    path = catalog_path(args.out, args.codex_version)
    atomic_write(path, text.encode())
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
