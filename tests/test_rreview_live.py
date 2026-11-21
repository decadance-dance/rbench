"""rreview in the agent image on a real Docker engine: docker only, no credentials, no model call.

Run: RBENCH_LIVE=1 uv run pytest -m live tests/test_rreview_live.py -s
It builds the agent image with rreview under a tag of its own and removes that tag, also when a
step fails. The container has no network, so no pass can reach a model.
"""

import re
import subprocess
import uuid

import pytest
from test_rreview import settings

from rbench.engines.rreview import RELEASES, launch
from rbench.trial import build_agent_image, image_id

pytestmark = pytest.mark.live

CLIS = {"claude": "2.1.316", "codex": "0.171.0"}
TAG = f"live-rreview-{uuid.uuid4().hex[:8]}"
# A synthetic PR in a fresh repository, laid out as a trial's (/rbench, here RBENCH_TRIAL_ROOT).
SETUP = """set -eu
R=/tmp/rbench; mkdir -p "$R/repo"; cd "$R/repo"
g() { git -c user.name=t -c user.email=t@example.com "$@"; }
g init -q -b base; g commit -q --allow-empty -m base
g checkout -q -b reviewed; echo x > a.txt; g add a.txt; g commit -q -m change
echo '{"number": 7, "title": "t", "body": "b"}' > "$R/pr.json"
"""


@pytest.fixture(scope="module")
def image(request):
    request.addfinalizer(
        lambda: subprocess.run(["docker", "image", "rm", f"rbench-agent:{TAG}"], check=False)
    )
    ((commit, release),) = RELEASES.items()
    return build_agent_image(CLIS, tag=TAG, rreview=(commit, release))


def run(image, script: str) -> subprocess.CompletedProcess[str]:
    argv = ["docker", "run", "--rm", "--network", "none", "-e", "RBENCH_TRIAL_ROOT=/tmp/rbench"]
    argv += [image_id(image.ref), "sh", "-c", script]
    return subprocess.run(argv, check=False, capture_output=True, text=True)


def test_image_has_rreview_and_zsh(image):
    out = run(image, "rreview --version && command -v zsh && cat /opt/rbench/zsh.version")

    assert out.returncode == 0, out.stderr
    print(out.stdout)
    version, path, zsh = out.stdout.splitlines()
    assert (version, path) == ("rreview 0.1.1", "/usr/bin/zsh")
    assert re.fullmatch(r"\d[\w.:~+-]*", zsh)  # the Debian package version


def test_rreview_resolves_the_trial_pr_through_the_gh_stub(image):
    argv = " ".join(launch(settings()).argv)
    out = run(image, SETUP + f'cd "$R/repo"; {argv}')

    print(out.stderr)
    # Past `gh pr view`, the merge base and the review worktree: the run dir exists. The passes
    # then fail for want of a login and of the shim's settings, and the review exits 2.
    assert "warning: --sandbox external" in out.stderr
    assert "run dir: /home/node/.local/state/rreview/rbench-trial/repo/7-" in out.stderr
    assert "gh pr view" not in out.stderr
    assert out.returncode == 2
