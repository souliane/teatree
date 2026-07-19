# test-path: cross-cutting — drives deploy/entrypoint.sh clone/offline branch (no src mirror).
"""Integration tests for the deploy entrypoint's baked-snapshot boot logic (#3451).

The self-contained image bakes a pinned source clone into the ``teatree_src``
volume, so ``deploy/entrypoint.sh`` must choose its boot mode at runtime:

* **online** — fast-forward the runtime clone from origin (self-update stays in-loop);
* **offline** — run the BAKED snapshot as-is, with zero fetches (deterministic boot).

``network_up`` is the switch (``TEATREE_FORCE_OFFLINE`` forces the baked path);
``ensure_clone`` branches on it. Per the Test-Writing Doctrine these run the REAL
shell functions (extracted verbatim from the entrypoint) in a bash subprocess
against a REAL local git origin under ``tmp_path`` — nothing about the shell logic
is reimplemented, and no network is touched. This mirrors the sibling
entrypoint tests (``test_deploy_entrypoint_disable_loops.py``,
``test_deploy_entrypoint_token_preflight.py``).
"""

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="needs bash + git (both present in the deploy image and CI)",
)

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"
_BASH = shutil.which("bash") or "bash"
_GIT = shutil.which("git") or "git"

# A deterministic identity for the fixture commits — never the operator's.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _extract_shell_function(name: str) -> str:
    """Return the verbatim source of shell function *name* from the entrypoint."""
    body: list[str] = []
    capturing = False
    for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}() {{"):
            capturing = True
        if capturing:
            body.append(line)
            if line == "}":
                return "\n".join(body)
    not_found = f"function {name!r} not found in {ENTRYPOINT}"
    raise AssertionError(not_found)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [_GIT, "-C", str(cwd), *args],
        check=True,
        env={**_GIT_ENV, "HOME": str(cwd)},
        capture_output=True,
        text=True,
    )


def _head(cwd: Path) -> str:
    return subprocess.run(
        [_GIT, "-C", str(cwd), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass(frozen=True, slots=True)
class _Origin:
    """A local git origin plus a runtime clone deliberately left one commit behind."""

    url: Path  # the bare origin (stands in for TEATREE_REPO_URL)
    clone: Path  # the runtime clone (stands in for TEATREE_CLONE_DIR), at commit1
    commit1: str  # the clone's baked HEAD
    commit2: str  # origin's tip — where an online fast-forward lands


def _make_origin_and_clone(tmp_path: Path) -> _Origin:
    """A bare origin at commit2 and a clone pinned at commit1 (one behind)."""
    origin = tmp_path / "origin.git"
    subprocess.run([_GIT, "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)

    seed = tmp_path / "seed"
    subprocess.run([_GIT, "clone", str(origin), str(seed)], check=True, capture_output=True)
    (seed / "a.txt").write_text("one\n", encoding="utf-8")
    _git(seed, "add", "a.txt")
    _git(seed, "commit", "-m", "commit1")
    _git(seed, "push", "origin", "main")

    clone = tmp_path / "clone"
    subprocess.run([_GIT, "clone", str(origin), str(clone)], check=True, capture_output=True)
    commit1 = _head(clone)

    # Advance origin so the clone is one fast-forwardable commit behind.
    (seed / "b.txt").write_text("two\n", encoding="utf-8")
    _git(seed, "add", "b.txt")
    _git(seed, "commit", "-m", "commit2")
    _git(seed, "push", "origin", "main")
    commit2 = _head(seed)

    return _Origin(url=origin, clone=clone, commit1=commit1, commit2=commit2)


def _run_ensure_clone(
    tmp_path: Path, *, clone_dir: Path, repo_url: Path, force_offline: bool
) -> subprocess.CompletedProcess[str]:
    """Run the extracted ``network_up`` + ``ensure_clone`` against a local origin."""
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -euo pipefail\n"
        f'CLONE_DIR="{clone_dir}"\n'
        f'REPO_URL="{repo_url}"\n'
        f"{_extract_shell_function('network_up')}\n"
        f"{_extract_shell_function('ensure_clone')}\n"
        "ensure_clone\n",
        encoding="utf-8",
    )
    env = {**_GIT_ENV, "HOME": str(tmp_path), "PATH": Path(_GIT).parent.as_posix() + ":/usr/bin:/bin"}
    if force_offline:
        env["TEATREE_FORCE_OFFLINE"] = "1"
    return subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=False, env=env)


class TestEnsureCloneOnline:
    def test_existing_clone_fast_forwards_from_origin(self, tmp_path: Path) -> None:
        origin = _make_origin_and_clone(tmp_path)
        result = _run_ensure_clone(tmp_path, clone_dir=origin.clone, repo_url=origin.url, force_offline=False)
        assert result.returncode == 0, result.stderr
        # Online: the runtime clone advanced to origin's tip.
        assert _head(origin.clone) == origin.commit2

    def test_missing_clone_is_cloned_when_online(self, tmp_path: Path) -> None:
        origin = _make_origin_and_clone(tmp_path)
        fresh = tmp_path / "fresh"
        result = _run_ensure_clone(tmp_path, clone_dir=fresh, repo_url=origin.url, force_offline=False)
        assert result.returncode == 0, result.stderr
        assert (fresh / ".git").exists()


class TestEnsureCloneOffline:
    def test_existing_clone_runs_baked_snapshot_without_fetching(self, tmp_path: Path) -> None:
        origin = _make_origin_and_clone(tmp_path)
        result = _run_ensure_clone(tmp_path, clone_dir=origin.clone, repo_url=origin.url, force_offline=True)
        assert result.returncode == 0, result.stderr
        # Offline: the clone stayed on its BAKED commit — no fast-forward happened.
        assert _head(origin.clone) == origin.commit1
        assert "BAKED snapshot" in result.stderr

    def test_missing_clone_fails_loud_when_offline(self, tmp_path: Path) -> None:
        origin = _make_origin_and_clone(tmp_path)
        fresh = tmp_path / "fresh"
        result = _run_ensure_clone(tmp_path, clone_dir=fresh, repo_url=origin.url, force_offline=True)
        assert result.returncode != 0
        assert not (fresh / ".git").exists()
        assert "cannot bootstrap the source offline" in result.stderr
