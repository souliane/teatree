# test-path: cross-cutting
"""The headless image links the runtime clone into the ORG-PREFIXED clone root.

The runtime clone lives on the ``teatree_src`` volume at ``/home/teatree/teatree``,
but ``clone_root()`` resolves source clones under ``~/workspace``. Provisioning a
worktree calls ``find_clone_path(clone_root, "souliane/teatree")``, which matches
the slug's literal path ``~/workspace/souliane/teatree`` — a bare
``~/workspace/teatree`` link (no owner segment) is invisible to that lookup, so
``workspace ticket`` fails at worktree provisioning ("No git clone found for
souliane/teatree") and the issue-implementer intake is silently disabled.

These tests pin the image to the org-prefixed discovery link and cross-check the
same layout resolves through the real ``find_clone_path`` (a RED-before-fix guard:
the old bare ``workspace/teatree`` link fails both).
"""

import re
from pathlib import Path

import pytest

from teatree.core.worktree.clone_paths import find_clone_path

DOCKERFILE = Path(__file__).resolve().parents[1] / "deploy" / "Dockerfile"

# The container's fixed layout the image must construct.
CLONE_DIR = "/home/teatree/teatree"
CLONE_ROOT = "/home/teatree/workspace"
REPO_SLUG = "souliane/teatree"
DISCOVERY_LINK = f"{CLONE_ROOT}/{REPO_SLUG}"


def _dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


class TestDockerfileDiscoveryLink:
    def test_org_prefixed_symlink_points_the_clone_root_at_the_runtime_clone(self) -> None:
        # `ln -s <CLONE_DIR> <CLONE_ROOT>/souliane/teatree` — the slug's literal
        # path, so find_clone_path(clone_root, "souliane/teatree") resolves.
        text = _dockerfile_text()
        assert re.search(rf"ln -s {re.escape(CLONE_DIR)} {re.escape(DISCOVERY_LINK)}\b", text), (
            "Dockerfile must symlink the runtime clone into the ORG-PREFIXED clone root "
            f"({DISCOVERY_LINK} -> {CLONE_DIR})"
        )

    def test_org_parent_dir_is_created_before_the_link(self) -> None:
        # The owner dir must exist for `ln` to place the link inside it.
        assert re.search(rf"mkdir -p {re.escape(CLONE_ROOT)}/souliane\b", _dockerfile_text())

    def test_no_bare_non_org_discovery_link(self) -> None:
        # A bare `workspace/teatree` link is invisible to the slug lookup — the
        # exact regression this fix removes. (t3-workspaces is a different path.)
        assert not re.search(rf"ln -s \S+ {re.escape(CLONE_ROOT)}/teatree\b", _dockerfile_text())


class TestFindClonePathResolvesContainerLayout:
    """The Dockerfile layout, reconstructed under tmp_path, resolves the slug.

    Mirrors the container exactly: the real clone lives OUTSIDE the clone root,
    reached only through the org-prefixed symlink.
    """

    def test_slug_resolves_through_org_prefixed_symlink_to_external_clone(self, tmp_path: Path) -> None:
        clone = tmp_path / "teatree"  # stands in for /home/teatree/teatree
        (clone / ".git").mkdir(parents=True)
        clone_root = tmp_path / "workspace"
        (clone_root / "souliane").mkdir(parents=True)
        (clone_root / "souliane" / "teatree").symlink_to(clone)

        resolved = find_clone_path(clone_root, REPO_SLUG)
        assert resolved == clone_root / "souliane" / "teatree"
        assert (resolved / ".git").is_dir()

    def test_bare_non_org_symlink_does_not_resolve_the_slug(self, tmp_path: Path) -> None:
        # The old container layout: link at workspace/teatree (no owner) — the
        # slug lookup misses it, reproducing the provisioning failure.
        clone = tmp_path / "teatree"
        (clone / ".git").mkdir(parents=True)
        clone_root = tmp_path / "workspace"
        clone_root.mkdir()
        (clone_root / "teatree").symlink_to(clone)

        assert find_clone_path(clone_root, REPO_SLUG) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
