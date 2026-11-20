from rbench.tasks.license import ALLOWED_LICENSES


def test_allowed_list_starts_with_the_redistributable_licenses() -> None:
    permissive = ("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MPL-2.0")
    assert ALLOWED_LICENSES[: len(permissive)] == permissive
    for gnu in ("GPL-2.0", "GPL-3.0", "LGPL-2.1", "LGPL-3.0", "AGPL-3.0"):
        assert gnu in ALLOWED_LICENSES
