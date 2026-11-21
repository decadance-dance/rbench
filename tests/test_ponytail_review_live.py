"""The `ponytail-review` agent image on a real Docker engine: docker only, no login, no model call.

Run: RBENCH_LIVE=1 uv run pytest -m live tests/test_ponytail_review_live.py -s
It builds the agent image with both CLIs and the pinned plugin under a tag of its own, then, with
no network, runs the adapter's Codex setup commands into an empty CODEX_HOME and lists what they
installed. It removes the tag it created, also when a step fails.
"""

import subprocess
import uuid
from pathlib import Path

import pytest

from rbench.engines import ponytail_review
from rbench.engines.declaration import load_engine
from rbench.trial import build_agent_image, image_id

pytestmark = pytest.mark.live

ENGINES = Path(__file__).parents[1] / "engines"
TAG = f"live-pr-{uuid.uuid4().hex[:8]}"
# The real codex (the shim on PATH runs only inside a prepared trial), then what it installed.
SETUP = (
    " && ".join("/usr/local/bin/" + " ".join(argv) for argv in ponytail_review.CODEX_SETUP)
    + ' && cat "$CODEX_HOME/config.toml" && find "$CODEX_HOME/plugins" -name SKILL.md'
)


def test_codex_installs_the_pinned_plugin_from_the_image_offline(request):
    engine = load_engine(ENGINES / "ponytail-review.toml")
    request.addfinalizer(
        lambda: subprocess.run(["docker", "image", "rm", f"rbench-agent:{TAG}"], check=False)
    )
    image = build_agent_image(dict(engine.clis), tag=TAG, plugin=ponytail_review.plugin(engine))

    done = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", image_id(image.ref), "sh", "-c", SETUP],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert '[plugins."ponytail@rbench"]' in done.stdout
    assert "/ponytail-review/SKILL.md" in done.stdout
