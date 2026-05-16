"""Repo-mode auto-detection (solo vs collaborative) — issue #550 item 4.

Integration-first per the Test-Writing Doctrine: a real git repo under
``tmp_path`` with scripted authorship history. The only mocked externals
are the clock-bound 7-day cache TTL and ``teatree.config.CONFIG_PATH``
(filesystem-isolated TOML fixture), mirroring ``test_config.py``.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.repo_mode import RepoMode, detect_repo_mode, resolve_repo_mode

_GIT = shutil.which("git") or "/usr/bin/git"


def _git(*args: str, cwd: Path, author: str | None = None) -> None:
    env = None
    if author is not None:
        env = {
            "GIT_AUTHOR_NAME": author,
            "GIT_AUTHOR_EMAIL": f"{author}@example.com",
            "GIT_COMMITTER_NAME": author,
            "GIT_COMMITTER_EMAIL": f"{author}@example.com",
            "PATH": "/usr/bin:/bin",
            "HOME": str(cwd),
        }
    subprocess.run([_GIT, "-C", str(cwd), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "seed@example.com", cwd=root)
    _git("config", "user.name", "seed", cwd=root)
    _git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=root)
    return root


def _commit(repo: Path, author: str, n: int = 1) -> None:
    for _ in range(n):
        _git("commit", "--allow-empty", "-q", "-m", f"by {author}", cwd=repo, author=author)


class TestDetectRepoMode:
    def test_single_author_history_is_solo(self, repo: Path) -> None:
        _commit(repo, "alice", n=10)
        assert detect_repo_mode(str(repo)) is RepoMode.SOLO

    def test_dominant_author_above_threshold_is_solo(self, repo: Path) -> None:
        _commit(repo, "alice", n=9)
        _commit(repo, "bob", n=1)
        assert detect_repo_mode(str(repo)) is RepoMode.SOLO

    def test_shared_authorship_is_collaborative(self, repo: Path) -> None:
        _commit(repo, "alice", n=6)
        _commit(repo, "bob", n=4)
        assert detect_repo_mode(str(repo)) is RepoMode.COLLABORATIVE

    def test_exactly_at_threshold_is_solo(self, repo: Path) -> None:
        _commit(repo, "alice", n=8)
        _commit(repo, "bob", n=2)
        assert detect_repo_mode(str(repo)) is RepoMode.SOLO

    def test_empty_window_defaults_collaborative(self, repo: Path) -> None:
        # No commits inside the window → unknown → conservative (don't fix proactively).
        assert detect_repo_mode(str(repo), since_days=90) is RepoMode.COLLABORATIVE

    def test_custom_threshold_changes_verdict(self, repo: Path) -> None:
        _commit(repo, "alice", n=7)
        _commit(repo, "bob", n=3)
        assert detect_repo_mode(str(repo), solo_threshold=0.6) is RepoMode.SOLO
        assert detect_repo_mode(str(repo), solo_threshold=0.8) is RepoMode.COLLABORATIVE


class TestResolveRepoMode:
    def test_config_override_solo_wins_over_detection(self, repo: Path, tmp_path: Path) -> None:
        _commit(repo, "alice", n=5)
        _commit(repo, "bob", n=5)  # detection would say COLLABORATIVE
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text('[teatree]\nrepo_mode = "solo"\n', encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            assert resolve_repo_mode(str(repo)) is RepoMode.SOLO

    def test_config_override_collaborative_wins_over_detection(self, repo: Path, tmp_path: Path) -> None:
        _commit(repo, "alice", n=10)  # detection would say SOLO
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text('[teatree]\nrepo_mode = "collaborative"\n', encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            assert resolve_repo_mode(str(repo)) is RepoMode.COLLABORATIVE

    def test_result_is_cached_and_reused(self, repo: Path, tmp_path: Path) -> None:
        _commit(repo, "alice", n=10)
        cache_dir = tmp_path / "cache"
        with patch("teatree.repo_mode.DATA_DIR", cache_dir):
            first = resolve_repo_mode(str(repo))
            assert first is RepoMode.SOLO
            cache_files = list((cache_dir / "repo-mode").glob("*.json"))
            assert len(cache_files) == 1
            cached = json.loads(cache_files[0].read_text(encoding="utf-8"))
            assert cached["mode"] == "solo"
            # Mutate history so a fresh detection would flip the verdict;
            # the still-fresh cache must keep returning the old answer.
            _commit(repo, "bob", n=20)
            assert resolve_repo_mode(str(repo)) is RepoMode.SOLO

    def test_stale_cache_is_refreshed(self, repo: Path, tmp_path: Path) -> None:
        _commit(repo, "alice", n=10)
        cache_dir = tmp_path / "cache"
        with patch("teatree.repo_mode.DATA_DIR", cache_dir):
            assert resolve_repo_mode(str(repo)) is RepoMode.SOLO
            cache_file = next((cache_dir / "repo-mode").glob("*.json"))
            stale = json.loads(cache_file.read_text(encoding="utf-8"))
            stale["ts"] = time.time() - (8 * 86_400)  # older than the 7-day TTL
            cache_file.write_text(json.dumps(stale), encoding="utf-8")
            _commit(repo, "bob", n=30)  # now COLLABORATIVE
            assert resolve_repo_mode(str(repo)) is RepoMode.COLLABORATIVE

    def test_refresh_flag_bypasses_fresh_cache(self, repo: Path, tmp_path: Path) -> None:
        _commit(repo, "alice", n=10)
        cache_dir = tmp_path / "cache"
        with patch("teatree.repo_mode.DATA_DIR", cache_dir):
            assert resolve_repo_mode(str(repo)) is RepoMode.SOLO
            _commit(repo, "bob", n=30)
            assert resolve_repo_mode(str(repo), refresh=True) is RepoMode.COLLABORATIVE
