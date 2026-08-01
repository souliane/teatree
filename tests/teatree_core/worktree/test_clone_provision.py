"""Tests for teatree.core.worktree.clone_provision — materialising an absent source clone.

Real git throughout (a local path IS a clonable remote), so the clone that
provisioning would consume is the clone these tests assert on.
"""

from pathlib import Path

import pytest

from teatree.core.worktree.clone_provision import ensure_clone
from tests._git_repo import make_git_repo, run_git


class _StubProvisioning:
    def __init__(self, urls: dict[str, str]) -> None:
        self._urls = urls

    def repo_clone_url(self, repo_name: str) -> str:
        return self._urls.get(repo_name, "")


class _StubOverlay:
    """Minimal stand-in for the one hook ``ensure_clone`` reads off an overlay."""

    def __init__(self, urls: dict[str, str]) -> None:
        self.provisioning = _StubProvisioning(urls)


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    origin = make_git_repo(tmp_path / "remote" / "backend")
    (origin / "README.md").write_text("backend\n", encoding="utf-8")
    run_git(origin, "add", "README.md")
    run_git(origin, "commit", "-q", "-m", "readme")
    return origin


class TestEnsureCloneWithNoLocalClone:
    def test_clones_from_the_overlay_declared_remote(self, tmp_path: Path, remote: Path) -> None:
        clone_root = tmp_path / "workspace"
        clone_root.mkdir()

        result = ensure_clone(clone_root, "backend", _StubOverlay({"backend": str(remote)}))

        assert result == clone_root / "backend"
        assert (result / ".git").is_dir()
        assert (result / "README.md").read_text(encoding="utf-8") == "backend\n"

    def test_clones_into_a_not_yet_existing_clone_root(self, tmp_path: Path, remote: Path) -> None:
        """A fresh runtime's clone root is an empty volume that nothing created yet."""
        clone_root = tmp_path / "never-created"

        result = ensure_clone(clone_root, "backend", _StubOverlay({"backend": str(remote)}))

        assert result == clone_root / "backend"
        assert (result / ".git").is_dir()

    def test_slug_repo_name_lands_at_the_namespaced_path(self, tmp_path: Path, remote: Path) -> None:
        clone_root = tmp_path / "workspace"

        result = ensure_clone(clone_root, "acme/backend", _StubOverlay({"acme/backend": str(remote)}))

        assert result == clone_root / "acme" / "backend"
        assert (result / ".git").is_dir()

    def test_returns_none_when_the_overlay_declares_no_remote(self, tmp_path: Path) -> None:
        clone_root = tmp_path / "workspace"
        clone_root.mkdir()

        assert ensure_clone(clone_root, "backend", _StubOverlay({})) is None

    def test_returns_none_with_no_overlay_at_all(self, tmp_path: Path) -> None:
        clone_root = tmp_path / "workspace"
        clone_root.mkdir()

        assert ensure_clone(clone_root, "backend", None) is None

    def test_returns_none_when_the_clone_fails(self, tmp_path: Path) -> None:
        clone_root = tmp_path / "workspace"
        clone_root.mkdir()

        result = ensure_clone(clone_root, "backend", _StubOverlay({"backend": str(tmp_path / "nowhere")}))

        assert result is None

    def test_refuses_to_clone_over_an_existing_non_checkout(self, tmp_path: Path, remote: Path) -> None:
        """A partial tree from an interrupted clone is reported, never silently removed."""
        clone_root = tmp_path / "workspace"
        (clone_root / "backend").mkdir(parents=True)
        (clone_root / "backend" / "leftover").write_text("x", encoding="utf-8")

        assert ensure_clone(clone_root, "backend", _StubOverlay({"backend": str(remote)})) is None
        assert (clone_root / "backend" / "leftover").is_file()


class TestEnsureCloneWithAnExistingClone:
    def test_returns_the_existing_clone_without_cloning(self, tmp_path: Path) -> None:
        clone_root = tmp_path / "workspace"
        existing = make_git_repo(clone_root / "backend")
        marker = existing / "local-only.txt"
        marker.write_text("untouched", encoding="utf-8")

        overlay = _StubOverlay({"backend": "https://example.invalid/backend.git"})
        result = ensure_clone(clone_root, "backend", overlay)

        assert result == existing
        assert marker.read_text(encoding="utf-8") == "untouched"

    def test_finds_a_namespaced_clone_by_basename(self, tmp_path: Path) -> None:
        clone_root = tmp_path / "workspace"
        existing = make_git_repo(clone_root / "acme" / "backend")

        overlay = _StubOverlay({"backend": "https://example.invalid/backend.git"})

        assert ensure_clone(clone_root, "backend", overlay) == existing
