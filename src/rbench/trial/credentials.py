"""The credentials a trial's CLIs log in with (spec trial-isolation, Credentials and Benchmark-only
Codex login; design D5 and Risks).

The Claude token comes from the operator's macOS Keychain and reaches the agent container only
through the environment of `docker compose up`, by variable name: never an argv, a file in the run
directory, a log or an image. Codex uses a login home only the benchmark uses, a directory that is
bind-mounted into the trial and holds nothing but the login, so that a refresh writes back there.
No value read here goes into an error message.
"""

import subprocess
from pathlib import Path

from rbench.trial.images import TrialError

KEYCHAIN_SERVICE = "rbench-claude-oauth"
CLAUDE_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
LOGIN_FILE = "auth.json"
# What Codex keeps next to `auth.json` (design D5): its lock and its transient save files.
LOGIN_LOCK = "auth.json.lock"
LOGIN_SAVE_PREFIX = ".auth-"


def claude_token() -> str:
    """The Claude OAuth token from the Keychain item `rbench-claude-oauth` (a generic password)."""
    argv = ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"]
    what = f"reading the Claude token from the Keychain item {KEYCHAIN_SERVICE!r}"
    try:
        result = subprocess.run(
            argv, check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL
        )
    except OSError as e:
        raise TrialError(f"{what}: cannot run security (macOS only): {e}") from e
    if result.returncode != 0:
        # stderr names the missing item or the refusal; stdout may hold a value, so it is dropped.
        detail = result.stderr.strip().splitlines()[-1:] or ["no output"]
        raise TrialError(f"{what}: security exited {result.returncode}: {detail[0]}")
    token = result.stdout.removesuffix("\n")
    if not token or not token.isascii() or not token.isprintable() or " " in token:
        raise TrialError(f"{what}: the item does not hold one line of printable ASCII text")
    return token


def check_login_home(home: Path) -> Path:
    """Refuse a Codex login home that holds anything but the login; return its absolute path.

    It must be a directory with a regular `auth.json`, and may hold only `auth.json.lock` and
    transient `.auth-*` files besides it. `codex login` leaves logs and `tmp/` there, which a trial
    must not get (design D5).
    """
    home = home.absolute()
    what = f"Codex login home {home}"
    if not home.is_dir():
        raise TrialError(f"{what}: not a directory")
    unexpected = []
    for entry in sorted(home.iterdir()):
        name = entry.name
        allowed = name in (LOGIN_FILE, LOGIN_LOCK) or name.startswith(LOGIN_SAVE_PREFIX)
        # A symlink or a directory under an allowed name would take writes out of the home.
        if not allowed or entry.is_symlink() or not entry.is_file():
            unexpected.append(name + "/" * entry.is_dir())
    if unexpected:
        raise TrialError(
            f"{what}: holds {', '.join(unexpected)}; it may hold only {LOGIN_FILE}, {LOGIN_LOCK}"
            f" and {LOGIN_SAVE_PREFIX}* files. Remove the rest before starting trials"
        )
    if not (home / LOGIN_FILE).is_file():
        raise TrialError(f"{what}: no {LOGIN_FILE}; run `codex login` with CODEX_HOME set to it")
    return home
