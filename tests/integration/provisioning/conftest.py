"""Fixtures for the real-provisioning integration package.

The ``provisioning_root`` fixture hands the test a clean tmp directory and a
``register_finalizer`` callback. Subclasses register a per-worktree teardown
through it at creation time (before any start attempt), so every spawned
server is reaped even when a later worktree fails to start.
"""

from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def provisioning_root(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> tuple[Path, Callable[[Callable[[], None]], None]]:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)

    def register_finalizer(fn: Callable[[], None]) -> None:
        request.addfinalizer(fn)

    return root, register_finalizer
