"""The source license check of an import (spec review-tasks, Source license).

The license is GitHub's own detection for the repository at the reviewed head
(`gh api repos/<repo>/license?ref=<head>`), so rbench does not guess a license from free text.
A missing license, one GitHub cannot identify (`NOASSERTION`) or one not on `ALLOWED_LICENSES`
refuses the import.
"""

import json
import re
import subprocess
from dataclasses import dataclass

from rbench.tasks.loader import COMMIT, REPO

# Licenses that allow redistribution of the source in a published archive, as GitHub's SPDX ids.
ALLOWED_LICENSES = (
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "MPL-2.0",
    "GPL-2.0",
    "GPL-3.0",
    "LGPL-2.0",
    "LGPL-2.1",
    "LGPL-3.0",
    "AGPL-3.0",
)

CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class LicenseError(ValueError):
    """A license that refuses the import or cannot be read; the message names the repository."""


class LicenseLookupError(LicenseError):
    """The license could not be read at all (gh missing or failing, a malformed reply).

    Unlike a refusal, this says nothing about the license, so an import stops rather than skips.
    """


@dataclass(frozen=True)
class SourceLicense:
    """The SPDX id of a repository's license and the path of its license file."""

    id: str
    path: str


def read_license(repo: str, head: str) -> SourceLicense:
    """The allowed license of `repo` (`owner/name` on GitHub) at commit `head`.

    Raise `LicenseError` naming the repository when GitHub finds no license, cannot identify it,
    identifies one that is not on `ALLOWED_LICENSES`, or cannot be asked.
    """
    if not REPO.fullmatch(repo) or not COMMIT.fullmatch(head):
        raise LicenseLookupError(
            f"{repo}: not an owner/name repository and a full commit id: {head!r}"
        )
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/license?ref={head}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as err:
        raise LicenseLookupError(f"{repo}: cannot run gh to read the license: {err}") from err
    if result.returncode != 0:
        if "HTTP 404" in result.stderr:
            raise LicenseError(f"{repo}: no license file at {head}")
        raise LicenseLookupError(f"{repo}: reading the license failed: {result.stderr.strip()}")
    try:
        reply = json.loads(result.stdout, object_pairs_hook=_unique_keys)
        spdx_id = reply["license"]["spdx_id"]
        path = reply["path"]
    except (KeyError, TypeError, ValueError) as err:
        raise LicenseLookupError(f"{repo}: malformed license reply from GitHub: {err}") from None
    if not isinstance(spdx_id, str) or not isinstance(path, str) or not _tree_path(path):
        raise LicenseLookupError(f"{repo}: malformed license reply from GitHub: spdx_id or path")
    if spdx_id == "NOASSERTION":
        raise LicenseError(f"{repo}: license in {path} is not recognized")
    if spdx_id not in ALLOWED_LICENSES:
        raise LicenseError(
            f"{repo}: license {spdx_id} in {path} is not on the allowed list"
            f" ({', '.join(ALLOWED_LICENSES)})"
        )
    return SourceLicense(spdx_id, path)


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    # `json` keeps the last of repeated keys; a reply that names two licenses is malformed.
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError("a key is repeated")
    return data


def _tree_path(path: str) -> bool:
    """A relative path inside the repository tree: no control characters, `.` or `..` parts."""
    parts = path.split("/")
    return not CONTROL.search(path) and all(part not in ("", ".", "..") for part in parts)
