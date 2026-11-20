"""The source license check of an import (spec review-tasks, Source license).

The license is read from the license files at the root of the reviewed tree and identified by its
text, so the check needs nothing beyond the commit being imported. A missing, ambiguous or
unrecognized license refuses the import; only ids on `ALLOWED_LICENSES` pass.
"""

import re

# Licenses that allow redistribution of the source in a published archive.
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

LICENSE_FILE = re.compile(
    r"(licen[cs]e|copying)(\.lesser)?(-[a-z0-9.-]+)?(\.(md|txt|rst))?", re.IGNORECASE
)

# The upper-case titles appear only in the GNU license headers; the texts name each other in
# mixed case, so a GPL-3.0 text that mentions the Affero license is not read as AGPL.
GNU_TITLE = re.compile(
    r"GNU (AFFERO |LESSER |LIBRARY )?GENERAL PUBLIC LICENSE Version (\d(?:\.\d)?)\b"
)
GNU_IDS = {
    ("", "2"): "GPL-2.0",
    ("", "3"): "GPL-3.0",
    ("LIBRARY ", "2"): "LGPL-2.0",
    ("LESSER ", "2.1"): "LGPL-2.1",
    ("LESSER ", "3"): "LGPL-3.0",
    ("AFFERO ", "3"): "AGPL-3.0",
}
MIT_GRANT = (
    "permission is hereby granted, free of charge, to any person obtaining a copy of this software"
)
MIT_CONDITION = (
    "the above copyright notice and this permission notice shall be included in all copies or "
    "substantial portions of the software"
)
ISC_GRANT = re.compile(
    r"permission to use, copy, modify, and(/or)? distribute this software for any purpose with or "
    r"without fee is hereby granted"
)
ISC_CONDITION = (
    "provided that the above copyright notice and this permission notice appear in all copies"
)
BSD_GRANT = (
    "redistribution and use in source and binary forms, with or without modification, are "
    "permitted provided that the following conditions are met"
)
BSD_ADVERTISING = "all advertising materials mentioning features"
BSD_ENDORSEMENT = ("neither the name", "endorse or promote products derived from this software")
APACHE_2 = re.compile(r"apache license,? version 2\.0")
MPL_2 = re.compile(r"mozilla public license,? (version|v\.) 2\.0")


class LicenseError(ValueError):
    """A source whose license refuses the import; the message names the repository."""


def is_license_file(name: str) -> bool:
    """True for a root file name that holds a license: LICENSE, COPYING.LESSER, LICENSE-MIT.md."""
    return LICENSE_FILE.fullmatch(name) is not None


def identify_license(text: str) -> str | None:
    """The SPDX id of the one license in `text`, or None when it holds none or several."""
    flat = " ".join(text.split())
    low = flat.lower()
    found = {GNU_IDS.get((m[1] or "", m[2]), "GNU-other") for m in GNU_TITLE.finditer(flat)}
    if MIT_GRANT in low and MIT_CONDITION in low:
        found.add("MIT")
    if ISC_GRANT.search(low) and ISC_CONDITION in low:
        found.add("ISC")
    if BSD_GRANT in low:
        if BSD_ADVERTISING in low:
            found.add("BSD-4-Clause")
        elif any(clause in low for clause in BSD_ENDORSEMENT):
            found.add("BSD-3-Clause")
        else:
            found.add("BSD-2-Clause")
    if APACHE_2.search(low):
        found.add("Apache-2.0")
    if MPL_2.search(low):
        found.add("MPL-2.0")
    return _single(found)


def check_license(repo: str, files: dict[str, str]) -> str:
    """The license id of `repo` from its root license files (name to text); raise `LicenseError`.

    The import is refused when there is no license file, when the files name no license or
    several different ones, or when the license is not on `ALLOWED_LICENSES`.
    """
    if not files:
        raise LicenseError(f"{repo}: no license file at the root of the reviewed head")
    ids = {name: identify_license(text) for name, text in sorted(files.items())}
    unknown = [name for name, license_id in ids.items() if license_id is None]
    if unknown:
        raise LicenseError(f"{repo}: license not recognized in {', '.join(unknown)}")
    # limit: several license files must name one license (LGPL with its GPL text counts as one);
    # a dual-licensed repository such as MIT plus Apache-2.0 is refused until the list says how
    # to record an SPDX expression.
    license_id = _single({i for i in ids.values() if i is not None})
    if license_id is None:
        named = ", ".join(f"{name} ({license_id})" for name, license_id in ids.items())
        raise LicenseError(f"{repo}: license files name different licenses: {named}")
    if license_id not in ALLOWED_LICENSES:
        raise LicenseError(
            f"{repo}: license {license_id} is not on the allowed list ({', '.join(ALLOWED_LICENSES)})"
        )
    return license_id


def _single(ids: set[str]) -> str | None:
    # An LGPL is distributed as its own text plus the GPL text it adds permissions to.
    lgpl = {i for i in ids if i.startswith("LGPL-")}
    if len(lgpl) == 1 and ids - lgpl <= {"GPL-2.0", "GPL-3.0"}:
        return lgpl.pop()
    return ids.pop() if len(ids) == 1 else None
