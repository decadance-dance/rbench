"""One trial's sandbox (spec trial-isolation, design D3, D5): its images, its compose project, its
hermetic configuration and the audit's session binding."""

from rbench.trial.compose import CODEX_LOGIN, PROXY_LOG, PROXY_URL, TrialProject, compose_spec
from rbench.trial.credentials import CLAUDE_TOKEN_ENV, check_login_home, claude_token
from rbench.trial.hermetic import codex_config, collect, prepare, shim_settings
from rbench.trial.images import (
    AGENT_NAME,
    PROXY_NAME,
    TrialError,
    build_agent_image,
    build_proxy_image,
    image_id,
)
from rbench.trial.repository import (
    REPO_DIR,
    REPOSITORY_FILE,
    RepositoryError,
    RepositoryState,
    agent_runner,
    copy_comments,
    count_objects,
    inspect_repository,
    prepare_repository,
)
from rbench.trial.sessions import SessionError, launch_record, read_shim_log, unbound_sessions

__all__ = [
    "AGENT_NAME",
    "CLAUDE_TOKEN_ENV",
    "CODEX_LOGIN",
    "PROXY_LOG",
    "PROXY_NAME",
    "PROXY_URL",
    "REPOSITORY_FILE",
    "REPO_DIR",
    "RepositoryError",
    "RepositoryState",
    "SessionError",
    "TrialError",
    "TrialProject",
    "agent_runner",
    "build_agent_image",
    "build_proxy_image",
    "check_login_home",
    "claude_token",
    "codex_config",
    "collect",
    "compose_spec",
    "copy_comments",
    "count_objects",
    "image_id",
    "inspect_repository",
    "launch_record",
    "prepare",
    "prepare_repository",
    "read_shim_log",
    "shim_settings",
    "unbound_sessions",
]
