"""A checkout under a temp root can never discharge a pull-request obligation (#4577).

The 16 leaked obligations were all registered from ad-hoc clones under ``/tmp`` and
``/var/tmp`` by the git pre-push hook — a venue with no teatree caller to pass a
"this one is disposable" flag, so the path is the only signal available.
"""

import tempfile
from pathlib import Path

import pytest

from teatree.utils.disposable_checkout import disposable_roots, is_disposable_checkout

#: The paths the issue measured, verbatim.
_LEAKED = (
    "/tmp/mt-4413",
    "/tmp/mt4412",
    "/tmp/rv4510/repo",
    "/var/tmp/rev-4521",
)
_REAL = (
    "/srv/workspace/t3-workspaces/t3-teatree/4560-a-ticket/teatree",
    "/srv/workspace/teatree",
    "/srv/.local/share/teatree-worktrees/4194-handover-single-row",
)


@pytest.fixture(autouse=True)
def _production_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the suite-wide sentinel so these assertions run the shipped defaults."""
    monkeypatch.delenv("TEATREE_DISPOSABLE_CHECKOUT_ROOTS", raising=False)


@pytest.mark.parametrize("path", _LEAKED)
def test_a_measured_leak_path_is_disposable(path: str) -> None:
    assert is_disposable_checkout(path)


@pytest.mark.parametrize("path", _REAL)
def test_a_real_worktree_is_not_disposable(path: str) -> None:
    assert not is_disposable_checkout(path)


def test_the_temp_root_itself_is_not_a_checkout() -> None:
    assert not is_disposable_checkout("/tmp")


def test_a_sibling_sharing_the_root_prefix_is_not_disposable() -> None:
    """``is_relative_to`` compares components, so ``/tmpfoo`` must not match ``/tmp``."""
    assert not is_disposable_checkout("/tmpfoo/clone")


def test_the_system_temp_dir_is_covered_even_when_it_is_neither_hardcoded_root() -> None:
    assert is_disposable_checkout(Path(tempfile.gettempdir()) / "some-clone")


def test_the_env_override_replaces_the_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TEATREE_DISPOSABLE_CHECKOUT_ROOTS", f"{tmp_path}:/opt/scratch")

    assert disposable_roots() == (tmp_path.resolve(), Path("/opt/scratch"))
    assert is_disposable_checkout(tmp_path / "clone")
    assert is_disposable_checkout("/opt/scratch/clone")


def test_a_blank_override_entry_is_dropped_rather_than_matching_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty segment resolves to the cwd, which would make every path disposable."""
    monkeypatch.setenv("TEATREE_DISPOSABLE_CHECKOUT_ROOTS", "::/opt/scratch:")

    assert disposable_roots() == (Path("/opt/scratch"),)
