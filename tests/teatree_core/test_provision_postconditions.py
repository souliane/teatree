"""The aggregate provision post-condition probe (PR-27, souliane/teatree#1385)."""

import tempfile
from pathlib import Path
from types import SimpleNamespace

from teatree.core.overlay import OverlayBase
from teatree.core.provision_postconditions import aggregate_provision_post_conditions
from teatree.core.worktree.worktree_env import CACHE_DIRNAME, CACHE_FILENAME
from teatree.types import ProvisionStep


class _StubOverlay(OverlayBase):
    def __init__(self, *, steps: tuple = (), db_strategy: dict | None = None) -> None:
        self._steps = list(steps)
        self._db_strategy = db_strategy

    def get_repos(self) -> list[str]:
        return ["repo"]

    def get_provision_steps(self, worktree):
        return self._steps

    def get_db_import_strategy(self, worktree):
        return self._db_strategy


def _worktree(wt_dir: Path, *, db_name: str = "") -> SimpleNamespace:
    return SimpleNamespace(worktree_path=str(wt_dir), db_name=db_name, extra={"worktree_path": str(wt_dir)})


def _provisioned_layout(root: Path) -> tuple[Path, Path]:
    """Return ``(wt_dir, env_cache)`` for a well-formed provisioned worktree."""
    wt_dir = root / "repo"
    wt_dir.mkdir()
    cache = root / CACHE_DIRNAME / CACHE_FILENAME
    cache.parent.mkdir()
    cache.write_text("WT_DB_NAME=x\n", encoding="utf-8")
    return wt_dir, cache


def _run(overlay: OverlayBase, worktree: SimpleNamespace) -> dict[str, bool]:
    return {p.check().name: p.check().passed for p in aggregate_provision_post_conditions(overlay, worktree)}


def test_all_core_post_conditions_hold_for_a_healthy_worktree():
    with tempfile.TemporaryDirectory() as tmp:
        wt_dir, _cache = _provisioned_layout(Path(tmp))
        outcomes = _run(_StubOverlay(), _worktree(wt_dir))
    assert outcomes == {"worktree-dir": True, "env-cache": True}


def test_deleting_env_cache_fails_the_env_cache_post_condition():
    with tempfile.TemporaryDirectory() as tmp:
        wt_dir, cache = _provisioned_layout(Path(tmp))
        cache.unlink()  # the falsification: env cache deleted out from under a provisioned worktree
        outcomes = _run(_StubOverlay(), _worktree(wt_dir))
    assert outcomes["worktree-dir"] is True
    assert outcomes["env-cache"] is False


def test_deleting_worktree_dir_fails_the_worktree_dir_post_condition():
    with tempfile.TemporaryDirectory() as tmp:
        wt_dir, _cache = _provisioned_layout(Path(tmp))
        outcomes = _run(_StubOverlay(), _worktree(wt_dir / "gone"))
    assert outcomes["worktree-dir"] is False


def test_db_post_condition_included_only_when_overlay_imports_a_db():
    with tempfile.TemporaryDirectory() as tmp:
        wt_dir, _cache = _provisioned_layout(Path(tmp))
        no_db = aggregate_provision_post_conditions(_StubOverlay(), _worktree(wt_dir, db_name="wt_db"))
        with_db = aggregate_provision_post_conditions(
            _StubOverlay(db_strategy={"kind": "dslr"}), _worktree(wt_dir, db_name="wt_db")
        )
    assert "app-db" not in {p.name for p in no_db}
    assert "app-db" in {p.name for p in with_db}


def test_step_post_condition_is_included_and_evaluated():
    step = ProvisionStep(name="import-db", callable=lambda: None, post_condition=lambda: False)
    with tempfile.TemporaryDirectory() as tmp:
        wt_dir, _cache = _provisioned_layout(Path(tmp))
        outcomes = _run(_StubOverlay(steps=(step,)), _worktree(wt_dir))
    assert outcomes["step:import-db"] is False


def test_no_probes_when_worktree_has_no_on_disk_path():
    unmaterialised = SimpleNamespace(worktree_path="", db_name="", extra={})
    probes = aggregate_provision_post_conditions(_StubOverlay(), unmaterialised)
    assert probes == []
