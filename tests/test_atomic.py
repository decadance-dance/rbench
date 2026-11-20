import os
import signal
import stat
import subprocess
import sys
import textwrap

import pytest

from rbench import atomic
from rbench.atomic import atomic_write


def test_writes_and_replaces(tmp_path):
    target = tmp_path / "status.json"

    atomic_write(target, '{"state": "running"}')
    atomic_write(target, b'{"state": "done"}')

    assert target.read_text() == '{"state": "done"}'
    assert os.listdir(tmp_path) == ["status.json"]


@pytest.mark.parametrize("umask, mode", [(0o022, 0o644), (0o077, 0o600)])
def test_new_file_honors_umask(tmp_path, monkeypatch, umask, mode):
    monkeypatch.setattr(atomic, "_UMASK", umask)
    target = tmp_path / "status.json"

    atomic_write(target, "new")

    assert stat.S_IMODE(target.stat().st_mode) == mode


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o664], ids=oct)
def test_replacement_keeps_the_mode(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(atomic, "_UMASK", 0o022)
    target = tmp_path / "status.json"
    target.write_text("old")
    target.chmod(mode)

    atomic_write(target, "new")

    assert stat.S_IMODE(target.stat().st_mode) == mode


def test_failed_write_keeps_old_file_and_removes_temp(tmp_path, monkeypatch):
    target = tmp_path / "status.json"
    target.write_text("old")

    def fail(fd):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="disk full"):
        atomic_write(target, "new")

    assert target.read_text() == "old"
    assert os.listdir(tmp_path) == ["status.json"]


@pytest.mark.parametrize("existed", [True, False])
def test_killed_write_leaves_no_partial_file(tmp_path, existed):
    target = tmp_path / "findings.json"
    if existed:
        target.write_text("old")
    # The child blocks in the first fsync, after the data is written to the temp file and before
    # the rename; the test kills it there.
    child = textwrap.dedent(
        f"""
        import os, sys, time
        from rbench.atomic import atomic_write

        def hang(fd):
            print("writing", flush=True)
            time.sleep(60)

        os.fsync = hang
        atomic_write({str(target)!r}, "new" * 100_000)
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline() == "writing\n"
        proc.send_signal(signal.SIGKILL)
        assert proc.wait(timeout=10) == -signal.SIGKILL
    finally:
        proc.kill()
        proc.stdout.close()

    if existed:
        assert target.read_text() == "old"
    else:
        assert not target.exists()
    leftovers = [p for p in tmp_path.iterdir() if p != target]
    assert len(leftovers) == 1 and leftovers[0].name.startswith(".findings.json.")
    # The staging file was still private when the writer died.
    assert stat.S_IMODE(leftovers[0].stat().st_mode) == 0o600
