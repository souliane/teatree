"""Sizing eligible artifacts inside the pass's cap and its time budget."""

import time
from pathlib import Path

import pytest

from teatree.core.cleanup.artifact_sizing import budget_spent, largest_first


@pytest.mark.parametrize(
    ("offset", "spent"),
    [(None, False), (-1.0, True), (60.0, False)],
    ids=["no-budget", "past", "future"],
)
def test_budget_spent_only_once_the_deadline_has_passed(offset: float | None, *, spent: bool) -> None:
    deadline = None if offset is None else time.monotonic() + offset

    assert budget_spent(deadline) is spent


def _artifact(root: Path, name: str, size: int) -> tuple[Path, Path]:
    checkout = root / name
    artifact = checkout / ".venv"
    artifact.mkdir(parents=True)
    (artifact / "blob").write_bytes(b"x" * size)
    return artifact, checkout


def test_the_largest_sized_artifact_comes_first(tmp_path: Path) -> None:
    small, large = _artifact(tmp_path, "a", 10), _artifact(tmp_path, "b", 1000)

    candidates, deferred = largest_first([small, large], deadline=None)

    assert [candidate.artifact for candidate in candidates] == [large[0], small[0]]
    assert deferred == []


def test_a_spent_budget_defers_every_unsized_artifact_by_name(tmp_path: Path) -> None:
    eligible = [_artifact(tmp_path, name, 10) for name in ("a", "b")]

    candidates, deferred = largest_first(eligible, deadline=time.monotonic() - 1)

    assert candidates == ()
    assert [line.split(":")[0] for line in deferred] == [str(artifact) for artifact, _ in eligible]
    assert all("time budget" in line for line in deferred)
