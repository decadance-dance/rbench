"""Exact-reference checks (spec engine-configurations, Pinned engines and CLIs).

Planning rejects a floating reference: a branch name, `latest`, a version range or a model alias.
Each check accepts only the exact form, so every other form counts as floating, listed or not.
`where` prefixes every error; it names the engine and, where known, the file and the field.
"""

import re
import tomllib
from pathlib import Path

# SemVer 2.0, from semver.org: pre-release and build metadata allowed; ranges and wildcards not.
_SEMVER = (
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*))*)?"
    r"(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?"
)
_TAG_PREFIX = "refs/tags/"
# limit: tag names are printable ASCII only, stricter than git, which also allows other bytes
# that are not control characters; such a tag must be pinned by its commit instead.
_TAG_CHARS = re.compile(r"[!-~]+", re.ASCII)
_TAG_WANT = "a tag as refs/tags/<name> (printable ASCII, git ref-name rules)"

# kind: (exact form, what to ask for instead)
EXACT = {
    "git": (
        re.compile(r"[0-9a-fA-F]{40}", re.ASCII),
        f"a full 40-character commit SHA or {_TAG_WANT}",
    ),
    "version": (re.compile(_SEMVER, re.ASCII), "an exact SemVer version such as 1.2.3"),
    "sha256": (
        re.compile(r"[0-9a-f]{64}", re.ASCII),
        "a SHA-256 digest of 64 lowercase hex characters",
    ),
    # A tag next to the digest is allowed: the digest decides which image runs.
    "image": (
        re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}", re.ASCII),
        "an image referenced by digest, <name>@sha256:<64 hex>",
    ),
}

MODELS_FILE = Path(__file__).with_name("models.toml")
# Full model id -> the CLI that runs it.
MODELS: dict[str, str] = tomllib.loads(MODELS_FILE.read_text(encoding="utf-8"))["models"]


class FloatingRefError(ValueError):
    """A reference that does not name one exact engine, CLI, image or model."""


def _git_tag(ref: str) -> bool:
    """A tag in git's tag namespace that `git check-ref-format` accepts, other than `latest`.

    The spelling cannot tell a tag from a branch of the same name, so a tag must be given as
    `refs/tags/<name>`; planning resolves it to its commit.
    """
    name = ref.removeprefix(_TAG_PREFIX)
    return (
        ref.startswith(_TAG_PREFIX)
        and _TAG_CHARS.fullmatch(name) is not None
        and not any(c in name for c in "~^:?*[\\")
        and ".." not in name
        and "@{" not in name
        and not name.endswith((".", "/"))
        and all(p and not p.startswith(".") and not p.endswith(".lock") for p in name.split("/"))
        and name.rsplit("/", 1)[-1].lower() != "latest"
    )


def check_ref(where: str, kind: str, ref: object, field: str | None = None) -> str:
    """Return `ref` if it is exact for `kind` (a key of `EXACT`), a commit id in lowercase;
    raise naming `where` and `ref`."""
    pattern, want = EXACT[kind]
    if isinstance(ref, str):
        if pattern.fullmatch(ref):
            return ref.lower() if kind == "git" else ref
        if kind == "git" and _git_tag(ref):
            return ref
    raise FloatingRefError(f"{where}: {field or kind} {ref!r} is a floating reference; use {want}")


def model_cli(where: str, model: object) -> str:
    """Return the CLI that runs `model`; reject an alias or any id not in `MODELS`."""
    if isinstance(model, str) and model in MODELS:
        return MODELS[model]
    raise FloatingRefError(
        f"{where}: model {model!r} is an alias or not a known full model id; give the full model"
        f" id, or add it to {MODELS_FILE.parent.name}/{MODELS_FILE.name} in the rbench package"
    )
