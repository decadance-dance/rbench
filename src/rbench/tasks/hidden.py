"""Hidden tasks: read only from the operator's hidden directory (spec review-tasks).

The environment variable `RBENCH_HIDDEN_DIR` names the hidden task directory: a task set directory
outside this repository, such as a checkout of the private task repository. It holds its own
configuration in `rbench-hidden.toml`:

    canary_prefix = "<the hidden canary prefix>"

So the hidden canary prefix stays with the hidden tasks and never enters this repository.
Results of a run that includes hidden tasks go only to a path git ignores, such as `runs/`.
"""

import os
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from rbench.tasks.loader import (
    HIDDEN_ID_PREFIX,
    Task,
    TaskError,
    check_hidden_canary_prefix,
    load_tasks,
)

HIDDEN_DIR_ENV = "RBENCH_HIDDEN_DIR"
HIDDEN_CONFIG_FILE = "rbench-hidden.toml"
HIDDEN_CONFIG_KEYS = {"canary_prefix"}

# The checkout this module runs from.
# limit: assumes the editable install `uv sync` makes from `src/`; under a wheel install this is a
# path inside the environment, and the overlap check guards nothing until the root is passed in.
PUBLIC_ROOT = Path(__file__).resolve().parents[3]

HOW_TO_CONFIGURE = (
    f"set {HIDDEN_DIR_ENV} to the hidden task directory, outside this repository, and put its"
    f' canary prefix in {HIDDEN_CONFIG_FILE} there as canary_prefix = "..."'
)


@dataclass(frozen=True)
class HiddenConfig:
    path: Path
    canary_prefix: str = field(repr=False)  # kept out of reprs, so out of logs and tracebacks


def hidden_config(
    environ: Mapping[str, str] = os.environ, public_root: Path = PUBLIC_ROOT
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
        raise TaskError(
            f"{HIDDEN_DIR_ENV} names {hidden_dir}, which is not a directory: {HOW_TO_CONFIGURE}"
        )
    public_root = Path(public_root).resolve()
    if hidden_dir.is_relative_to(public_root) or public_root.is_relative_to(hidden_dir):
        raise TaskError(
            f"{HIDDEN_DIR_ENV} names {hidden_dir}, which overlaps the public repository"
            f" {public_root}; hidden tasks must live outside it"
        )
    config_path = hidden_dir / HIDDEN_CONFIG_FILE
    if not config_path.is_file():
        raise TaskError(
            f"the hidden directory {hidden_dir} has no {HIDDEN_CONFIG_FILE}: {HOW_TO_CONFIGURE}"
        )
    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    # ValueError covers bad TOML and bad UTF-8; `from None` keeps the file's text out of tracebacks.
    except (OSError, ValueError) as e:
        raise TaskError(f"cannot read {config_path}: {e}") from None
    # Keys are not quoted: a misplaced prefix must not reach the message.
    if data.keys() != HIDDEN_CONFIG_KEYS:
        raise TaskError(f"{config_path} must hold exactly one key, canary_prefix")
    try:
        check_hidden_canary_prefix(data["canary_prefix"])
    except TaskError as e:
        raise TaskError(f"{config_path}: {e}") from None
    return HiddenConfig(hidden_dir, data["canary_prefix"])


def load_task_sets(*public_dirs: Path, hidden: HiddenConfig | None = None) -> list[Task]:
    """Load public task sets and, when `hidden` is given, the hidden one; sorted by id.

    Hidden tasks load only from `hidden.path`: a hidden task in a public set directory, or a task
    of another set in the hidden directory, fails the load.
    """
    for set_dir in map(Path, public_dirs):
        for entry in sorted(set_dir.iterdir()) if set_dir.is_dir() else ():
            if entry.is_dir() and entry.name.startswith(HIDDEN_ID_PREFIX):
                raise TaskError(
                    f"task {entry} has a hidden id outside the hidden directory; hidden tasks are"
                    f" read only from the directory {HIDDEN_DIR_ENV} names"
                )
    # Without the hidden prefix, the loader also refuses any task whose set is hidden.
    tasks = load_tasks(*public_dirs)
    if hidden is not None:
        hidden_tasks = load_tasks(hidden.path, hidden_canary_prefix=hidden.canary_prefix)
        for task in hidden_tasks:
            if task.set != "hidden":
                raise TaskError(
                    f"task {task.path} is in {task.set!r}; the hidden directory holds only hidden tasks"
                )
        # The two loads cannot clash: ids differ by the hidden id prefix, canaries by their prefix.
        tasks += hidden_tasks
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
