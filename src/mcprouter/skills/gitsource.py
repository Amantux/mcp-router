"""Shallow-clone https git skill sources into skills_cache_dir/<source id>.

Hardening: URL validated by urlcheck (https only, no localhost), refs
restricted to a conservative charset that cannot start with '-' (blocks
`--upload-pack=` style option injection) and positional args follow `--`;
hooks disabled, every protocol but https denied, no submodules, no tags,
120s timeout, post-clone size cap. Tests inject `runner` instead of enabling
file:// transport, so production argv never contains a file-protocol allowance.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from mcprouter.inference.urlcheck import InvalidEndpointError, validate_outbound_url

GIT_TIMEOUT_S = 120
CLONE_MAX_BYTES = 200 * 1024 * 1024
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SAFE_CFG = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "submodule.recurse=false",
)

Runner = Callable[[Sequence[str]], None]


class GitSourceError(RuntimeError):
    """Curated (URL/path-free) git failure."""


class GitCloneFailedError(GitSourceError):
    """Clone exited non-zero or could not start."""


class GitTimeoutError(GitSourceError):
    """Clone exceeded the time budget."""


class GitTooLargeError(GitSourceError):
    """Checkout exceeded the size cap."""


class GitInvalidRefError(GitSourceError):
    """Ref failed validation."""


class GitNotRepositoryError(GitSourceError):
    """Clone produced no git repository."""


def _default_runner(argv: Sequence[str]) -> None:
    env = {"PATH": os.environ.get("PATH", ""), "GIT_TERMINAL_PROMPT": "0", "HOME": "/nonexistent"}
    try:
        subprocess.run(  # noqa: S603 - argv list, validated inputs
            list(argv), check=True, capture_output=True, timeout=GIT_TIMEOUT_S, env=env
        )
    except subprocess.TimeoutExpired as exc:
        raise GitTimeoutError("git clone timed out") from exc
    except (subprocess.CalledProcessError, OSError) as exc:
        raise GitCloneFailedError("git clone failed") from exc


def validate_ref(ref: str) -> str:
    if not _REF_RE.fullmatch(ref) or ".." in ref or ref.endswith((".lock", "/")):
        raise GitInvalidRefError("invalid git ref")
    return ref


def clone_argv(url: str, ref: str, dest: Path) -> list[str]:
    return [
        "git",
        *_SAFE_CFG,
        "clone",
        "--depth",
        "1",
        "--no-tags",
        "--recurse-submodules=no",
        "--single-branch",
        "--branch",
        validate_ref(ref),
        "--",
        url,
        str(dest),
    ]


def _tree_size(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root, followlinks=False):
        for f in files:
            p = Path(dirpath) / f
            if not p.is_symlink():
                total += p.stat().st_size
    return total


def _head_commit(dest: Path) -> str | None:
    head = dest / ".git" / "HEAD"
    try:
        text = head.read_text().strip()
        if text.startswith("ref: "):
            text = (dest / ".git" / text[5:]).read_text().strip()
    except OSError:
        return None
    return text if re.fullmatch(r"[0-9a-f]{40,64}", text) else None


def fetch(
    source_id: str,
    url: str,
    ref: str | None,
    cache_dir: str | Path,
    *,
    runner: Runner = _default_runner,
    max_bytes: int = CLONE_MAX_BYTES,
) -> tuple[Path, str | None]:
    """Fresh shallow clone (replace-on-success). Returns (checkout dir, commit)."""
    if not url.lower().startswith("https://"):
        raise GitSourceError("git sources must use https")
    try:
        validate_outbound_url(url, allow_http_localhost=False)
    except InvalidEndpointError as exc:
        raise GitSourceError("git source URL is not allowed") from exc
    if not re.fullmatch(r"[0-9a-f-]{1,36}", source_id):
        raise GitSourceError("invalid source id")
    base = Path(cache_dir)
    base.mkdir(parents=True, exist_ok=True)
    final, tmp = base / source_id, base / f".{source_id}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    runner(clone_argv(url, ref or "main", tmp))
    if _tree_size(tmp) > max_bytes:
        shutil.rmtree(tmp, ignore_errors=True)
        raise GitTooLargeError("git source exceeds the size cap")
    if not (tmp / ".git").is_dir():
        shutil.rmtree(tmp, ignore_errors=True)
        raise GitNotRepositoryError("not a git repository")
    shutil.rmtree(final, ignore_errors=True)
    tmp.rename(final)
    return final, _head_commit(final)
