"""``get_overlay_for_repo`` owns a repo on segment boundaries, not by raw substring.

The URL side of repo ownership has been boundary-safe since #1120
(``_full_slug_owns`` / ``_bare_name_owns``); the cwd-repo side still asked
``repo_slug in slug``, so ``acme/widget`` owned ``acme/widget-extra`` and the
wrong overlay was selected for the checkout. These pin the converged behaviour,
including the two-tier order the bundled bare-name overlay depends on.
"""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay_for_repo

_GIT = shutil.which("git") or "git"


def _repo_with_origin(path: Path, origin_url: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run([_GIT, "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run([_GIT, "remote", "add", "origin", origin_url], cwd=path, check=True)
    return path


class _RepoOverlay(OverlayBase):
    """Concrete overlay exposing a fixed workspace-repo token list."""

    def __init__(self, repos: list[str]) -> None:
        self._repos = repos

    def get_repos(self) -> list[str]:
        return self._repos

    def get_provision_steps(self, worktree: object) -> list:
        return []


class TestOwnershipIsSegmentBounded:
    def test_a_prefix_slug_does_not_own_a_longer_repo_name(self, tmp_path: Path) -> None:
        repo = _repo_with_origin(tmp_path / "widget-extra", "git@github.com:acme/widget-extra.git")
        overlays = {"a": _RepoOverlay(["acme/widget"])}
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            assert get_overlay_for_repo(str(repo)) is None

    def test_an_exact_slug_still_owns(self, tmp_path: Path) -> None:
        repo = _repo_with_origin(tmp_path / "widgets", "git@github.com:acme/widgets.git")
        overlays = {"a": _RepoOverlay(["acme/widgets"]), "b": _RepoOverlay(["other/repo"])}
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            assert get_overlay_for_repo(str(repo)) is overlays["a"]


class TestBareNameIsTheWeakTier:
    def test_a_full_slug_owner_outranks_a_bare_name_claimant(self, tmp_path: Path) -> None:
        repo = _repo_with_origin(tmp_path / "widgets", "git@github.com:acme/widgets.git")
        overlays = {"bare": _RepoOverlay(["widgets"]), "full": _RepoOverlay(["acme/widgets"])}
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            assert get_overlay_for_repo(str(repo)) is overlays["full"]

    def test_a_bare_name_still_owns_when_no_full_slug_claims_the_repo(self, tmp_path: Path) -> None:
        repo = _repo_with_origin(tmp_path / "teatree", "git@github.com:souliane/teatree.git")
        overlays = {"a": _RepoOverlay(["teatree"])}
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            assert get_overlay_for_repo(str(repo)) is overlays["a"]
