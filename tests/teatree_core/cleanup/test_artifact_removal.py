"""Removal anchored to the identities that authorised it."""

import os
from pathlib import Path
from unittest.mock import patch

from teatree.core.cleanup import artifact_removal
from teatree.core.cleanup.artifact_removal import AnchoredArtifact, remove_anchored_artifact


def _identity(path: Path) -> tuple[int, int, int]:
    status = path.lstat()
    return status.st_dev, status.st_ino, status.st_mode


def _target(tmp_path: Path) -> AnchoredArtifact:
    checkout = tmp_path / "checkout"
    artifact = checkout / ".nx"
    artifact.mkdir(parents=True)
    (artifact / "cache.bin").write_bytes(b"x")
    return AnchoredArtifact(artifact, checkout, _identity(artifact), _identity(checkout), None)


def test_an_authorised_artifact_is_removed_whole(tmp_path: Path) -> None:
    target = _target(tmp_path)

    assert remove_anchored_artifact(target) == ""
    assert list(target.checkout.iterdir()) == []


def test_a_directory_swapped_in_during_the_rename_is_put_back_untouched(tmp_path: Path) -> None:
    target = _target(tmp_path)
    real_rename = os.rename
    renames: list[tuple[str, str]] = []

    def _rename_after_a_swap(src: str, dst: str, **kwargs: int) -> None:
        if not renames:
            displaced = target.checkout / "displaced"
            real_rename(target.artifact, displaced)
            target.artifact.mkdir()
            (target.artifact / "replacement.txt").write_text("new", encoding="utf-8")
        renames.append((src, dst))
        real_rename(src, dst, **kwargs)

    with patch.object(artifact_removal.os, "rename", side_effect=_rename_after_a_swap):
        reason = remove_anchored_artifact(target)

    assert "put back" in reason
    assert (target.artifact / "replacement.txt").read_text(encoding="utf-8") == "new"
    assert len(renames) == 2, "moved aside, then back"
