"""One trial's sandbox (spec trial-isolation, design D3): its images and its compose project."""

from rbench.trial.compose import PROXY_LOG, PROXY_URL, TrialProject, compose_spec
from rbench.trial.images import (
    AGENT_NAME,
    PROXY_NAME,
    TrialError,
    build_agent_image,
    build_proxy_image,
    image_id,
)

__all__ = [
    "AGENT_NAME",
    "PROXY_LOG",
    "PROXY_NAME",
    "PROXY_URL",
    "TrialError",
    "TrialProject",
    "build_agent_image",
    "build_proxy_image",
    "compose_spec",
    "image_id",
]
