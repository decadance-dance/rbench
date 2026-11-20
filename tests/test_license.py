import pytest

from rbench.tasks.license import (
    ALLOWED_LICENSES,
    LicenseError,
    check_license,
    identify_license,
    is_license_file,
)

# The passages of each license text that identify it, with the line breaks of the real files.
MIT = """MIT License

Copyright (c) 2026 Example

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction.

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
"""
MIT_0 = MIT.replace("The above copyright notice and this permission notice shall be", "")
BSD = """Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice.
2. Redistributions in binary form must reproduce the above copyright notice.
"""
BSD_3 = BSD + "3. Neither the name of the copyright holder nor the names of its contributors.\n"
BSD_4 = BSD_3 + "4. All advertising materials mentioning features or use of this software.\n"
ISC = """ISC License

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.
"""
ZERO_BSD = """Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted.
"""
APACHE = """
                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/
"""
MPL = "Mozilla Public License Version 2.0\n==================================\n"
GPL_2 = (
    "                    GNU GENERAL PUBLIC LICENSE\n                       Version 2, June 1991\n"
)
# GPL-3.0 names the Affero and Lesser licenses in its body, in mixed case.
GPL_3 = (
    "                    GNU GENERAL PUBLIC LICENSE\n                       Version 3, 29 June 2007\n"
    "  13. Use with the GNU Affero General Public License.\n"
    "use the GNU Lesser General Public License instead of this License.\n"
)
LGPL_20 = "                  GNU LIBRARY GENERAL PUBLIC LICENSE\n                       Version 2, June 1991\n"
LGPL_21 = (
    "                  GNU LESSER GENERAL PUBLIC LICENSE\n"
    "                       Version 2.1, February 1999\n"
)
LGPL_3 = "                   GNU LESSER GENERAL PUBLIC LICENSE\n                       Version 3, 29 June 2007\n"
AGPL_3 = "                    GNU AFFERO GENERAL PUBLIC LICENSE\n                       Version 3, 19 November 2007\n"


def test_allowed_list_starts_with_the_redistributable_licenses() -> None:
    assert ALLOWED_LICENSES[:6] == (
        "MIT",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "MPL-2.0",
    )
    for gnu in ("GPL-2.0", "GPL-3.0", "LGPL-2.0", "LGPL-2.1", "LGPL-3.0", "AGPL-3.0"):
        assert gnu in ALLOWED_LICENSES


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (MIT, "MIT"),
        (BSD, "BSD-2-Clause"),
        (BSD_3, "BSD-3-Clause"),
        (BSD_4, "BSD-4-Clause"),
        (ISC, "ISC"),
        (ISC.replace("and/or", "and"), "ISC"),
        (APACHE, "Apache-2.0"),
        ("Licensed under the Apache License, Version 2.0 (the License);", "Apache-2.0"),
        (MPL, "MPL-2.0"),
        ("subject to the terms of the Mozilla Public License, v. 2.0.", "MPL-2.0"),
        (GPL_2, "GPL-2.0"),
        (GPL_3, "GPL-3.0"),
        (LGPL_20, "LGPL-2.0"),
        (LGPL_21, "LGPL-2.1"),
        (LGPL_3, "LGPL-3.0"),
        (LGPL_3 + GPL_3, "LGPL-3.0"),
        (AGPL_3, "AGPL-3.0"),
        (MIT_0, None),
        (ZERO_BSD, None),
        ("Copyright 2026 Example. All rights reserved.", None),
        ("", None),
        (MIT + APACHE, None),
    ],
)
def test_identify_license(text: str, expected: str | None) -> None:
    assert identify_license(text) == expected


@pytest.mark.parametrize(
    "name",
    ["LICENSE", "license.md", "LICENSE.txt", "LICENCE", "COPYING", "COPYING.LESSER", "LICENSE-MIT"],
)
def test_license_file_names(name: str) -> None:
    assert is_license_file(name)


@pytest.mark.parametrize("name", ["README.md", "license.py", "LICENSES", "src", "MY_LICENSE"])
def test_not_license_file_names(name: str) -> None:
    assert not is_license_file(name)


def test_check_license_returns_the_id() -> None:
    assert check_license("acme/widget", {"LICENSE": MIT}) == "MIT"
    assert check_license("acme/widget", {"COPYING": GPL_3, "COPYING.LESSER": LGPL_3}) == "LGPL-3.0"


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ({}, "no license file"),
        ({"LICENSE": "All rights reserved."}, "not recognized in LICENSE"),
        ({"LICENSE": MIT, "NOTICE.LICENSE": "x"}, "not recognized"),
        ({"LICENSE-MIT": MIT, "LICENSE-APACHE": APACHE}, "different licenses"),
        ({"LICENSE": BSD_4}, "BSD-4-Clause is not on the allowed list"),
    ],
)
def test_check_license_refuses_and_names_the_repository(files: dict, reason: str) -> None:
    with pytest.raises(LicenseError, match=reason) as err:
        check_license("acme/widget", files)
    assert str(err.value).startswith("acme/widget: ")
