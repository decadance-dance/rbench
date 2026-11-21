import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

FAKE_BIN = Path(__file__).parent / "fixtures" / "bin"
FAKE_TOOLS = ("claude", "codex", "docker", "gh", "security")


def require_fakes(bin_dir: Path, path: str) -> None:
    """Fail unless every fake tool on `path` resolves to the executable shim in `bin_dir`.

    Otherwise a missing or broken shim (a checkout without symlinks) falls through to the real CLI.
    """
    shim = (bin_dir / "fake").resolve()
    for tool in FAKE_TOOLS:
        found = shutil.which(tool, path=path)
        if found is None or Path(found).resolve() != shim:
            pytest.fail(f"fake {tool} does not resolve to {shim} on PATH: found {found}")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("RBENCH_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test; run with RBENCH_LIVE=1 uv run pytest -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@dataclass
class FakeBin:
    """Fake `claude`, `codex`, `docker`, `gh` and `security` first on PATH; see
    tests/fixtures/bin/fake."""

    log: Path
    monkeypatch: pytest.MonkeyPatch
    scenarios: dict = field(default_factory=dict)

    def scenario(self, tool: str, **scenario: object) -> None:
        self.scenarios[tool] = scenario
        self.monkeypatch.setenv("RBENCH_FAKE_SCENARIO", json.dumps(self.scenarios))

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBin:
    log = tmp_path / "fake-calls.jsonl"
    path = f"{FAKE_BIN}{os.pathsep}{os.environ['PATH']}"
    require_fakes(FAKE_BIN, path)
    monkeypatch.setenv("PATH", path)
    monkeypatch.setenv("RBENCH_FAKE_LOG", str(log))
    monkeypatch.delenv("RBENCH_FAKE_SCENARIO", raising=False)
    monkeypatch.delenv("RBENCH_FAKE_ENV", raising=False)
    return FakeBin(log, monkeypatch)
