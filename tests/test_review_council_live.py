"""review-council's agent image on a real Docker engine: docker only, no login, no model call.

Run: RBENCH_LIVE=1 uv run pytest -m live tests/test_review_council_live.py -s
It builds the agent image with the pinned plugin (and yq) once, under a tag of its own, then, with
no network, runs the plugin's own config reader on the state the adapter writes, in a fresh
repository and in a checkout that commits a conflicting state. It removes the tag and the
container it created, also when a step fails.
"""

import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_review_council import COMMITTED, settings

from rbench.engines import review_council
from rbench.engines.declaration import load_engine
from rbench.trial import build_agent_image, image_id
from rbench.trial.repository import REPO_DIR

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


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    engine = load_engine(ENGINES / "review-council.toml")
    try:
        built = build_agent_image(
            {"claude": engine.clis["claude"]},
            tag=TAG,
            plugin=review_council.plugin(engine.version),
        )
        yield image_id(built.ref)
    finally:
        subprocess.run(["docker", "image", "rm", f"rbench-agent:{TAG}"], check=False)


def test_image_holds_the_pinned_plugin_and_its_config_reader_drops_the_seats(image):
    config = (ENGINES / "review-council" / "config.local.yml").read_text()

    done = subprocess.run(
        ["docker", "run", "--rm", "-i", "--network", "none", image]
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


def test_committed_state_in_the_checkout_changes_nothing_the_reader_sees(image, request):
    name = f"live-rc-{uuid.uuid4().hex[:8]}"
    request.addfinalizer(lambda: subprocess.run(["docker", "rm", "-f", name], check=False))
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "--network", "none", image, "sleep", "600"],
        check=True,
        capture_output=True,
    )

    def run(argv: list[str], data: str | bytes = b"") -> str:
        done = subprocess.run(
            ["docker", "exec", "-i", name, *argv],
            input=data.encode() if isinstance(data, str) else data,
            capture_output=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr.decode()
        return done.stdout.decode()

    def write(path: str, data: str | bytes) -> None:
        run(["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path], data)

    def reader_output(repo: str, committed: dict[str, str]) -> str:
        git = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.com"]
        run(["git", "init", "-q", "-b", "reviewed", repo])
        for path, text in {"a.py": "x = 1\n", **committed}.items():
            write(f"{repo}/{path}", text)
        run([*git, "add", "-A"])
        run([*git, "commit", "-q", "-m", "reviewed"])
        assert review_council.check_checkout(run, repo) == []
        for path, data in review_council.launch(ENGINES, settings()).files.items():
            write(path.replace(REPO_DIR, repo, 1), data)
        assert review_council.prepare_checkout(run, repo) == []
        assert run([*git, "status", "--porcelain", "--untracked-files=all"]) == ""
        assert run(["cat", f"{repo}/.review-council/learnings.md"]) == ""
        script = f"{review_council.PLUGIN_DIR}/scripts/rc-config.sh"
        return run(["sh", "-c", 'cd "$1" && "$2" .review-council 2>/dev/null', "sh", repo, script])

    committed = reader_output("/home/node/committed", COMMITTED)
    fresh = reader_output("/home/node/fresh", {})

    assert committed == fresh
    lines = set(committed.splitlines())
    assert {"reviewer.claude.enabled=true", "settings.verify=true"} <= lines
    assert {"pr_comments.enabled=false", "lens.security.replaces_dedicated=false"} <= lines
    assert {"reviewer.google.enabled=false", "reviewer.perplexity.enabled=false"} <= lines
    assert "custom" not in committed
