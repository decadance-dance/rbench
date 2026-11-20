"""Hidden tasks: read only from the operator's hidden directory (spec review-tasks).

The environment variable `RBENCH_HIDDEN_DIR` names the hidden task directory: a task set directory
outside the public checkout, such as a checkout of the private task repository. It holds its own
configuration in `rbench-hidden.toml`:

    canary_prefix = "<the hidden canary prefix>"

So the hidden canary prefix stays with the hidden tasks and never enters this repository. Hidden
tasks load only through `hidden_config` and `load_hidden_tasks`; every hidden input must resolve
inside the hidden directory. Errors name the variable and a position, never a hidden path, id,
canary or the prefix. Results of a run that includes hidden tasks go only to a path git ignores,
such as `runs/`.
"""

import os
import re
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from rbench.tasks.loader import (
    HIDDEN_DIR_ENV,
    Task,
    TaskError,
    _load_tasks,
    check_hidden_canary_prefix,
    is_within,
    load_tasks,
)

HIDDEN_CONFIG_FILE = "rbench-hidden.toml"
HIDDEN_CONFIG_KEYS = {"canary_prefix"}
PACKAGE_DIR = Path(__file__).resolve().parent

HOW_TO_CONFIGURE = (
    f"set {HIDDEN_DIR_ENV} to the hidden task directory, outside the public checkout, and put its"
    f' canary prefix in {HIDDEN_CONFIG_FILE} there as canary_prefix = "..."'
)
HIDDEN_DIR = f"the directory {HIDDEN_DIR_ENV} names"
CONFIG_NAME = f"{HIDDEN_DIR_ENV}/{HIDDEN_CONFIG_FILE}"
TOML_POSITION = re.compile(r"\(at (line \d+, column \d+|end of document)\)$")
_ISSUED = object()


@dataclass(frozen=True)
class HiddenConfig:
    """The hidden directory and its canary prefix; only `hidden_config` makes one that loads."""

    path: Path = field(repr=False)
    canary_prefix: str = field(repr=False)
    # Not an init field, so neither a constructor call nor `dataclasses.replace` can forge it.
    _issued: object = field(default=None, init=False, repr=False, compare=False)

    def inside(self, relative: str | Path) -> Path:
        """`relative` in the hidden directory, such as a task archive; refused if it leads out."""
        path = self.path / relative
        if not is_within(path, self.path):
            raise TaskError(f"an entry of {HIDDEN_DIR} resolves outside it")
        return path


def public_checkouts(cwd: Path | None = None, package_dir: Path = PACKAGE_DIR) -> list[Path]:
    """The git checkouts rbench runs from and is installed from, which the hidden directory avoids.

    The hidden directory may be a git checkout of its own; only these two count.
    """
    roots = []
    for start in (Path.cwd() if cwd is None else Path(cwd), Path(package_dir)):
        if start.is_dir():
            result = subprocess.run(
                ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                roots.append(Path(result.stdout.strip()))
    return roots


def hidden_config(
    environ: Mapping[str, str] = os.environ,
    cwd: Path | None = None,
    package_dir: Path = PACKAGE_DIR,
) -> HiddenConfig:
    """Read the hidden directory's configuration; raise `TaskError` saying how to configure it.

    Call it when a run selects hidden tasks: without the directory, the run refuses to start.
    """
    value = environ.get(HIDDEN_DIR_ENV, "")
    if not value.strip():
        raise TaskError(
            f"hidden tasks are selected, but no hidden directory is configured: {HOW_TO_CONFIGURE}"
        )
    hidden_dir = Path(value).expanduser().resolve()
    if not hidden_dir.is_dir():
        raise TaskError(f"{HIDDEN_DIR} is not a directory: {HOW_TO_CONFIGURE}")
    # Fail closed: without a known public checkout, the overlap cannot be ruled out.
    checkouts = public_checkouts(cwd, package_dir)
    if not checkouts:
        raise TaskError(
            f"cannot find the public rbench checkout to keep {HIDDEN_DIR} out of it;"
            " run rbench from its checkout"
        )
    for root in checkouts:
        if is_within(hidden_dir, root) or is_within(root, hidden_dir):
            raise TaskError(
                f"{HIDDEN_DIR} overlaps the git checkout rbench runs from or is installed from;"
                " hidden tasks must live outside the public repository"
            )
    config_path = hidden_dir / HIDDEN_CONFIG_FILE
    if not is_within(config_path, hidden_dir):
        raise TaskError(f"{CONFIG_NAME} resolves outside the hidden directory")
    if not config_path.is_file():
        raise TaskError(f"{CONFIG_NAME} does not exist: {HOW_TO_CONFIGURE}")
    # Parser and OS messages may quote the file or its path: only a position or a reason is kept,
    # and the error is raised outside the handler, so nothing is chained to it.
    error, data = None, {}
    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        position = TOML_POSITION.search(str(e))
        error = f"is not valid TOML (at {position.group(1) if position else 'an unknown position'})"
    except UnicodeDecodeError:
        error = "is not UTF-8"
    except OSError as e:
        error = f"cannot be read: {e.strerror}"
    if error:
        raise TaskError(f"{CONFIG_NAME} {error}")
    # Keys are not quoted: a misplaced prefix must not reach the message.
    if data.keys() != HIDDEN_CONFIG_KEYS:
        raise TaskError(f"{CONFIG_NAME} must hold exactly one key, canary_prefix")
    try:
        check_hidden_canary_prefix(data["canary_prefix"])
    except TaskError as e:
        error = str(e)
    if error:
        raise TaskError(f"{CONFIG_NAME}: {error}")
    config = HiddenConfig(hidden_dir, data["canary_prefix"])
    object.__setattr__(config, "_issued", _ISSUED)
    return config


def load_hidden_tasks(config: HiddenConfig) -> list[Task]:
    """Load the hidden tasks of a configuration `hidden_config` made, sorted by id."""
    if config._issued is not _ISSUED:
        raise TaskError(f"hidden tasks load only through hidden_config(), from {HIDDEN_DIR}")
    return _load_tasks([config.path], config.canary_prefix)


def load_task_sets(*public_dirs: Path, hidden: HiddenConfig | None = None) -> list[Task]:
    """Load public task sets and, when `hidden` is given, the hidden tasks; sorted by id.

    A hidden task in a public set directory fails the load. The two loads cannot clash: ids differ
    by the hidden id prefix, canaries by their prefix.
    """
    tasks = load_tasks(*public_dirs)
    if hidden is not None:
        tasks += load_hidden_tasks(hidden)
    return sorted(tasks, key=lambda t: t.id)


def check_hidden_results_dir(path: Path) -> None:
    """Refuse `path` for results of hidden tasks unless git ignores it.

    A run that includes hidden tasks calls this before writing anything. It fails closed: a path
    git cannot vouch for, including one outside every work tree, is refused.
    """
    path = Path(path).resolve()
    anchor = next(p for p in (path, *path.parents) if p.is_dir())
    result = subprocess.run(
        ["git", "-C", str(anchor), "check-ignore", "-q", "--", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        reason = result.stderr.strip() or "git does not ignore it"
        raise TaskError(
            f"results of hidden tasks go only to a path git ignores, such as runs/ in this"
            f" checkout; {path} is refused: {reason}"
        )
