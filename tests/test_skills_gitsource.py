"""git source: hardened argv, ref/URL validation, size cap (runner injected)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from mcprouter.skills.gitsource import GitSourceError, clone_argv, fetch

SID = "0f0e0d0c-0000-4000-8000-000000000001"


def test_argv_is_hardened(tmp_path: Path) -> None:
    argv = clone_argv("https://example.com/r.git", "main", tmp_path / "d")
    joined = " ".join(argv)
    for frag in (
        "core.hooksPath=/dev/null",
        "protocol.allow=never",
        "protocol.https.allow=always",
        "--recurse-submodules=no",
        "--no-tags",
        "--depth 1",
        "http.followRedirects=false",  # a validated https URL must not 302 elsewhere
    ):
        assert frag in joined
    assert "file.allow" not in joined
    assert argv[argv.index("--") + 1] == "https://example.com/r.git"
    assert argv.index("--branch") < argv.index("--")


@pytest.mark.parametrize("ref", ["--upload-pack=touch /tmp/x", "-b", "a..b", "x y", "", "r.lock"])
def test_bad_refs(ref: str, tmp_path: Path) -> None:
    with pytest.raises(GitSourceError):
        clone_argv("https://example.com/r.git", ref, tmp_path)


@pytest.mark.parametrize(
    "url", ["http://example.com/r", "file:///etc", "ssh://git@h/r", "https://169.254.169.254/r"]
)
def test_bad_urls(url: str, tmp_path: Path) -> None:
    with pytest.raises(GitSourceError):
        fetch(SID, url, "main", tmp_path, runner=lambda a: None)


def _fake(size: int):  # noqa: ANN202
    def run(argv: Sequence[str]) -> None:
        dest = Path(argv[-1])
        (dest / ".git").mkdir(parents=True)
        (dest / ".git" / "HEAD").write_text("a" * 40 + "\n")
        (dest / "blob").write_bytes(b"x" * size)

    return run


def test_fetch_and_size_cap(tmp_path: Path) -> None:
    d, commit = fetch(SID, "https://example.com/r.git", "v1", tmp_path, runner=_fake(10))
    assert d == tmp_path / SID and commit == "a" * 40
    with pytest.raises(GitSourceError, match="size cap"):
        fetch(SID, "https://example.com/r.git", "v1", tmp_path, runner=_fake(500), max_bytes=100)
    assert (tmp_path / SID / "blob").stat().st_size == 10  # previous checkout kept


@pytest.mark.parametrize("failure", ["runner-raises", "not-a-repo", "too-large"])
def test_failed_fetch_leaves_no_tmp_dir(tmp_path: Path, failure: str) -> None:
    """P-110: a failed clone never leaves .<id>.tmp behind."""
    from mcprouter.skills.gitsource import GitCloneFailedError

    def runner(argv: Sequence[str]) -> None:
        dest = Path(argv[-1])
        dest.mkdir(parents=True)
        (dest / "partial.bin").write_bytes(b"x" * 64)
        if failure == "runner-raises":
            raise GitCloneFailedError("git clone failed")
        if failure == "too-large":
            (dest / ".git").mkdir()

    with pytest.raises(GitSourceError):
        cap = 8 if failure == "too-large" else 10**6
        fetch("abc123", "https://git.example/r.git", "main", tmp_path, runner=runner, max_bytes=cap)
    assert not (tmp_path / ".abc123.tmp").exists()
    assert not (tmp_path / "abc123").exists()
