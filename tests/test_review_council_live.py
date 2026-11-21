"""review-council's agent image on a real Docker engine: docker only, no login, no model call.

Run: RBENCH_LIVE=1 uv run pytest -m live tests/test_review_council_live.py -s
It builds the agent image with the pinned plugin (and yq) under a tag of its own, then, with no
network, runs the plugin's own config reader on the seat config the adapter writes. It removes the
tag it created, also when a step fails.
"""

import subprocess
import uuid
from pathlib import Path

import pytest

from rbench.engines import review_council
from rbench.engines.declaration import load_engine
from rbench.trial import build_agent_image, image_id

pytestmark = pytest.mark.live

ENGINES = Path(__file__).parents[1] / "engines"
TAG = f"live-rc-{uuid.uuid4().hex[:8]}"
# Runs the plugin's config reader in a fresh git repository holding the seat config from stdin,
# as review-council's Step 0.1 does in the reviewed checkout.
READ_CONFIG = (
    'cd "$(mktemp -d)" && git init -q && mkdir .review-council'
    " && cat > .review-council/config.local.yml"
    ' && "$1/scripts/rc-config.sh" .review-council'
)


def test_image_holds_the_pinned_plugin_and_its_config_reader_drops_the_seats(request):
    engine = load_engine(ENGINES / "review-council.toml")
    request.addfinalizer(
        lambda: subprocess.run(["docker", "image", "rm", f"rbench-agent:{TAG}"], check=False)
    )
    image = build_agent_image(
        {"claude": engine.clis["claude"]}, tag=TAG, plugin=review_council.plugin(engine.version)
    )
    config = (ENGINES / "review-council" / "config.local.yml").read_text()

    done = subprocess.run(
        ["docker", "run", "--rm", "-i", "--network", "none", image_id(image.ref)]
        + ["sh", "-c", READ_CONFIG, "sh", review_council.PLUGIN_DIR],
        input=config,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    lines = set(done.stdout.splitlines())
    assert {"reviewer.google.enabled=false", "reviewer.perplexity.enabled=false"} <= lines
    assert {"reviewer.claude.enabled=true", "reviewer.codex.enabled=true"} <= lines
    assert "reviewer.codex.model=" in lines  # Codex keeps the model of the trial's config.toml
    assert "yq not found" not in done.stderr
