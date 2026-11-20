import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

FAKE_BIN = Path(__file__).parent / "fixtures" / "bin"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("RBENCH_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test; run with RBENCH_LIVE=1 uv run pytest -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@dataclass
class FakeBin:
    """Fake `claude`, `codex`, `docker` and `gh` first on PATH; see tests/fixtures/bin/fake."""

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
    monkeypatch.setenv("PATH", f"{FAKE_BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("RBENCH_FAKE_LOG", str(log))
    monkeypatch.delenv("RBENCH_FAKE_SCENARIO", raising=False)
    return FakeBin(log, monkeypatch)
