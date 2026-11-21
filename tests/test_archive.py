import hashlib
import json
import os
import random
import subprocess
from pathlib import Path

import pytest
from conftest import FakeBin

from rbench.tasks.archive import ArchiveError, build_archive
from rbench.tasks.license import LicenseError

MIT = (
    "Permission is hereby granted, free of charge, to any person obtaining a copy of this software\n"
    "The above copyright notice and this permission notice shall be included in all copies or\n"
    "substantial portions of the Software.\n"
)
REPO = "acme/widget"


def git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "dev",
        "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "dev",
        "GIT_COMMITTER_EMAIL": "dev@example.com",
    }
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def commit(repo: Path, files: dict[str, str | None], message: str) -> str:
    for name, text in files.items():
        if text is None:
            git(repo, "rm", "-q", name)
        else:
            (repo / name).write_text(text)
            git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def license_reply(spdx_id: str, path: str = "LICENSE") -> str:
    """The parts of GitHub's `GET /repos/{owner}/{repo}/license` reply that the import reads."""
    return json.dumps({"path": path, "type": "file", "license": {"spdx_id": spdx_id}})


@pytest.fixture(autouse=True)
def mit_on_github(fake_bin: FakeBin) -> None:
    fake_bin.scenario("gh", stdout=license_reply("MIT"))


@pytest.fixture
def source(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, {"README.md": "first\n"}, "first")
    git(repo, "tag", "-a", "v1", "-m", "v1")
    git(repo, "remote", "add", "origin", "https://example.com/acme/widget.git")
    return repo


def pr(repo: Path, base_license: str | None, head_license: str | None) -> tuple[str, str]:
    """A base commit and a two-commit PR head on top of it, plus a later follow-up commit."""
    base = commit(
        repo, {"app.py": "x = 1\n", **({"LICENSE": base_license} if base_license else {})}, "base"
    )
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, {"app.py": "x = 2\n"}, "first change")
    change = {} if head_license == base_license else {"LICENSE": head_license}
    head = commit(repo, {"app.py": "x = 3\n", **change}, "second change")
    commit(repo, {"app.py": "x = 4\n"}, "follow-up after review")
    return base, head


def unpack(bundle: Path, dest: Path) -> Path:
    git(dest.parent, "init", "-q", "--bare", str(dest))
    git(dest, "fetch", "-q", str(bundle), "refs/heads/*:refs/heads/*")
    return dest


def test_bundle_has_exactly_two_commits_and_two_branches(source: Path, tmp_path: Path) -> None:
    base, head = pr(source, MIT, MIT)
    archive = build_archive(REPO, str(source), base, head, tmp_path / "out" / "t.bundle", "ours")

    heads = git(tmp_path, "bundle", "list-heads", str(archive.path)).splitlines()
    assert sorted(line.split()[1] for line in heads) == ["refs/heads/base", "refs/heads/reviewed"]
    verify = subprocess.run(
        ["git", "bundle", "verify", str(archive.path)],
        cwd=source,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "complete history" in verify.stdout + verify.stderr

    repo = unpack(archive.path, tmp_path / "unpacked")
    assert sorted(git(repo, "rev-list", "--all").splitlines()) == sorted(
        [git(repo, "rev-parse", "reviewed"), git(repo, "rev-parse", "base")]
    )
    assert git(repo, "rev-parse", "reviewed^") == git(repo, "rev-parse", "base")
    assert git(repo, "rev-parse", "base^{tree}") == git(source, "rev-parse", f"{base}^{{tree}}")
    assert git(repo, "rev-parse", "reviewed^{tree}") == git(source, "rev-parse", f"{head}^{{tree}}")
    assert git(repo, "tag", "-l") == ""
    assert git(repo, "remote") == ""
    # The pack holds only what the two commits reach: no tag object, no source history.
    stored = git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)")
    reachable = {
        line.split()[0] for line in git(repo, "rev-list", "--objects", "--all").splitlines()
    }
    assert set(stored.splitlines()) == reachable
    assert git(repo, "log", "--format=%an %ae %ad %s", "--date=unix", "reviewed").splitlines() == [
        "author author@example.com 946684800 reviewed",
        "author author@example.com 946684800 base",
    ]


def test_sha256_is_the_hash_of_the_written_bundle(source: Path, tmp_path: Path) -> None:
    base, head = pr(source, MIT, MIT)
    out = tmp_path / "t.bundle"
    archive = build_archive(REPO, str(source), base, head, out, "reviewbench")
    assert archive.path == out
    assert archive.sha256 == hashlib.sha256(out.read_bytes()).hexdigest()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_archive_hash_survives_an_upstream_repack(source: Path, tmp_path: Path) -> None:
    # Similar large files, so how the source deltifies them shows in a pack that reuses its deltas.
    common = random.Random(0).randbytes(20_000).hex()
    commit(source, {f"story-{i:03}.txt": common + str(i) for i in range(140)}, "files")
    base, head = pr(source, MIT, MIT)
    git(source, "config", "pack.window", "0")
    git(source, "repack", "-adq")
    first = build_archive(REPO, str(source), base, head, tmp_path / "a.bundle", "ours")
    git(source, "-c", "pack.window=10", "repack", "-adfq")
    second = build_archive(REPO, str(source), base, head, tmp_path / "b.bundle", "ours")
    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == second.path.read_bytes()


def test_license_id_recorded_and_license_file_kept(
    source: Path, tmp_path: Path, fake_bin: FakeBin
) -> None:
    base, head = pr(source, MIT, MIT)
    archive = build_archive(REPO, str(source), base, head, tmp_path / "t.bundle", "ours")
    assert (archive.license, archive.license_file) == ("MIT", "LICENSE")
    repo = unpack(archive.path, tmp_path / "unpacked")
    assert git(repo, "show", "reviewed:LICENSE") == MIT.strip()
    # The license is asked for the reviewed head, not the base or a branch.
    assert [c["argv"] for c in fake_bin.calls()] == [["api", f"repos/{REPO}/license?ref={head}"]]


@pytest.mark.parametrize("task_set", ["reviewbench", "ours"])
@pytest.mark.parametrize(
    ("gh", "reason"),
    [
        ({"stderr": "gh: Not Found (HTTP 404)\n", "exit": 1}, "no license file at "),
        ({"stdout": license_reply("NOASSERTION")}, "license in LICENSE is not recognized"),
        (
            {"stdout": license_reply("BSD-4-Clause")},
            "BSD-4-Clause in LICENSE is not on the allowed",
        ),
        ({"stdout": license_reply("MIT", "NOTICE")}, "NOTICE is not in the reviewed tree"),
        ({"stdout": '{"license": null}'}, "malformed license reply"),
        ({"stdout": license_reply("MIT", "LICENSE\0")}, "malformed license reply"),
        ({"stdout": license_reply("MIT", "../LICENSE")}, "malformed license reply"),
        ({"stdout": license_reply("MIT", "/LICENSE")}, "malformed license reply"),
        ({"stdout": license_reply("MIT", "")}, "malformed license reply"),
        (
            {"stdout": '{"path":"LICENSE","license":{"spdx_id":"NOASSERTION","spdx_id":"MIT"}}'},
            "malformed license reply",
        ),
        ({"stderr": "error connecting to api.github.com\n", "exit": 1}, "reading the license"),
    ],
)
def test_refused_license_names_the_repository_and_writes_no_archive(
    source: Path, tmp_path: Path, fake_bin: FakeBin, task_set: str, gh: dict, reason: str
) -> None:
    # The source text claims a license; only GitHub's detection decides.
    text = (
        "Proprietary. We evaluated the Apache License, Version 2.0. Redistribution is prohibited."
    )
    base, head = pr(source, text, text)
    fake_bin.scenario("gh", **gh)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = out_dir / "t.bundle"
    out.write_bytes(b"previous archive")
    with pytest.raises(LicenseError, match=f"^{REPO}: ") as err:
        build_archive(REPO, str(source), base, head, out, task_set)
    assert reason in str(err.value)
    assert sorted(p.name for p in out_dir.iterdir()) == ["t.bundle"]
    assert out.read_bytes() == b"previous archive"


def test_hidden_tasks_skip_the_license_check(
    source: Path, tmp_path: Path, fake_bin: FakeBin
) -> None:
    base, head = pr(source, None, None)
    archive = build_archive(REPO, str(source), base, head, tmp_path / "t.bundle", "hidden")
    assert (archive.license, archive.license_file) == (None, None)
    assert fake_bin.calls() == []


def test_operator_git_config_and_environment_are_ignored(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, head = pr(source, MIT, MIT)
    home = tmp_path / "home"
    home.mkdir()
    # Read, this rewrite would send the fetch to a repository that does not exist.
    (home / ".gitconfig").write_text(f'[url "{tmp_path}/missing"]\n\tinsteadOf = {source}\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.setenv("GIT_DIR", str(source / ".git"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "operator")
    archive = build_archive(REPO, str(source), base, head, tmp_path / "t.bundle", "ours")
    repo = unpack(archive.path, tmp_path / "unpacked")
    assert git(repo, "log", "-1", "--format=%an", "reviewed") == "author"
    assert git(source, "branch", "--list", "base", "reviewed") == ""


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"base": "main"}, "base 'main' is not a full commit id"),
        ({"head": "abc"}, "head 'abc' is not a full commit id"),
        ({"source": "--upload-pack=touch pwned"}, "looks like an option"),
        ({"task_set": "secret"}, "unknown task set"),
        ({"head": "0" * 40}, "git fetch failed"),
    ],
)
def test_bad_inputs_fail_and_name_the_repository(
    source: Path, tmp_path: Path, change: dict, reason: str
) -> None:
    base, head = pr(source, MIT, MIT)
    args = {"source": str(source), "base": base, "head": head, "task_set": "ours"} | change
    with pytest.raises(ArchiveError, match=f"^{REPO}: ") as err:
        build_archive(REPO, out=tmp_path / "t.bundle", **args)
    assert reason in str(err.value)
    assert not (tmp_path / "t.bundle").exists()


def test_missing_gh_is_a_license_error_naming_the_repository(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, head = pr(source, MIT, MIT)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(LicenseError, match=f"^{REPO}: cannot run gh") as err:
        build_archive(REPO, str(source), base, head, tmp_path / "out" / "t.bundle", "ours")
    assert isinstance(err.value.__cause__, FileNotFoundError)
    assert not (tmp_path / "out").exists()
