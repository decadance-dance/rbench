"""Exact-reference checks (spec engine-configurations, Pinned engines and CLIs).

Planning rejects a floating reference: a branch name, `latest`, a version range or a model alias.
Each check accepts only the exact form, so every other form counts as floating, listed or not.
"""

import re

_VERSION = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?"

# kind: (exact form, what to ask for instead)
EXACT = {
    "git": (
        re.compile(rf"[0-9a-f]{{40}}|v?{_VERSION}"),
        "a full 40-character commit SHA or a version tag such as v1.2.3",
    ),
    "version": (re.compile(_VERSION), "an exact version such as 1.2.3"),
    "sha256": (re.compile(r"[0-9a-f]{64}"), "a SHA-256 digest of 64 lowercase hex characters"),
    "image": (
        re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}"),
        "an image referenced by digest, <name>@sha256:<64 hex>",
    ),
}

# limit: only Claude and OpenAI GPT ids are known; another provider's models need a pattern here.
_MODEL_CLI = (
    ("claude", re.compile(r"claude-[a-z]+-\d+-\d+(?:-\d{8})?")),
    ("codex", re.compile(r"gpt-\d+(?:\.\d+)*(?:-[a-z0-9]+)*")),
)


class FloatingRefError(ValueError):
    """A reference that does not name one exact engine, CLI, image or model."""


def _floating(value: object) -> bool:
    return not isinstance(value, str) or "latest" in value.lower()


def check_ref(engine: str, kind: str, ref: object, what: str | None = None) -> str:
    """Return `ref` if it is exact for `kind` (a key of `EXACT`); raise naming the engine and ref."""
    pattern, want = EXACT[kind]
    if _floating(ref) or not pattern.fullmatch(ref):
        raise FloatingRefError(
            f"engine {engine!r}: {what or kind} {ref!r} is a floating reference; use {want}"
        )
    return ref


def model_cli(engine: str, model: object) -> str:
    """Return the CLI that runs `model` (`claude` or `codex`); reject an alias or unknown id."""
    if not _floating(model):
        for cli, pattern in _MODEL_CLI:
            if pattern.fullmatch(model):
                return cli
    raise FloatingRefError(
        f"engine {engine!r}: model {model!r} is an alias or not a full model id;"
        " give the full model id, such as claude-opus-5-5 or gpt-6.2-sol"
    )
