"""The force-keep layer over the tach pytest plugin (#3672).

The layer re-adds our escalations (floor dirs, doc-readers, mirrors, changed tests) over
the plugin's deselection, in the SAME session. Three surfaces are pinned: the item
partition (which collected items are protected), the ``force_keep_for`` chain over real
git under ``tmp_path`` (with its fail-safe), and the collection hook wrapper itself —
driven directly to prove a protected item survives an inner deselection.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from teatree.quality import force_keep_plugin as plugin
from teatree.quality.affected_tests import FLOOR_DIRS, ForceKeep
from teatree.quality.force_keep_plugin import (
    PROTECT_ALL,
    force_keep_for,
    protected_items,
    pytest_collection_modifyitems,
)
from tests._git_repo import make_git_repo, run_git


def _item(root: Path, rel: str) -> SimpleNamespace:
    return SimpleNamespace(path=root / rel, nodeid=rel)


class TestProtectedItems:
    _ROOT = Path("/repo")

    def test_only_force_kept_items_are_protected(self) -> None:
        items = [
            _item(self._ROOT, "tests/quality/test_a.py"),  # under a floor dir
            _item(self._ROOT, "tests/test_blueprint_sync.py"),  # exact force-kept file
            _item(self._ROOT, "tests/teatree_other/test_z.py"),  # neither
        ]
        keep = ForceKeep(paths=("tests/quality", "tests/test_blueprint_sync.py"))
        kept = protected_items(items, self._ROOT, keep)
        assert {i.nodeid for i in kept} == {"tests/quality/test_a.py", "tests/test_blueprint_sync.py"}

    def test_protect_all_keeps_every_item(self) -> None:
        items = [_item(self._ROOT, "tests/a/test_a.py"), _item(self._ROOT, "tests/b/test_b.py")]
        assert protected_items(items, self._ROOT, PROTECT_ALL) == items

    def test_an_item_outside_the_root_is_not_protected(self) -> None:
        outside = _item(Path("/elsewhere"), "tests/quality/test_a.py")
        kept = protected_items([outside], self._ROOT, ForceKeep(paths=("tests/quality",)))
        assert kept == []


class TestForceKeepForOverRealGit:
    def test_a_changed_test_file_is_force_kept(self, tmp_path: Path) -> None:
        make_git_repo(tmp_path)
        # teatree writes a hermetic-config cache into the repo root at runtime; keep it
        # untracked-invisible so it does not read as a diff and escalate the verdict.
        (tmp_path / ".gitignore").write_text("t3-hermetic-config/\n")
        run_git(tmp_path, "add", ".gitignore")
        run_git(tmp_path, "commit", "-qm", "ignore runtime droppings")
        run_git(tmp_path, "checkout", "-q", "-b", "work")
        (tmp_path / "tests/teatree_work").mkdir(parents=True)
        (tmp_path / "tests/teatree_work/test_thing.py").write_text("def test_x() -> None: ...\n")
        run_git(tmp_path, "add", "-A")
        run_git(tmp_path, "commit", "-qm", "add test")

        keep = force_keep_for(tmp_path, "main")
        assert keep is not PROTECT_ALL
        assert set(FLOOR_DIRS) <= set(keep.paths)
        assert "tests/teatree_work/test_thing.py" in keep.paths

    def test_a_full_verdict_force_keeps_nothing(self, tmp_path: Path) -> None:
        make_git_repo(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests/conftest.py").write_text("# base\n")
        run_git(tmp_path, "add", "-A")
        run_git(tmp_path, "commit", "-qm", "base")
        run_git(tmp_path, "checkout", "-q", "-b", "work")
        (tmp_path / "tests/conftest.py").write_text("# changed tree-wide\n")
        run_git(tmp_path, "add", "-A")
        run_git(tmp_path, "commit", "-qm", "conftest")

        keep = force_keep_for(tmp_path, "main")
        assert keep is not PROTECT_ALL
        assert keep.paths == ()

    def test_a_non_git_dir_fails_safe_to_protect_all(self, tmp_path: Path) -> None:
        assert force_keep_for(tmp_path, "origin/main") is PROTECT_ALL


class TestResolveBase:
    def test_reads_the_tach_base_option(self) -> None:
        config = SimpleNamespace(getoption=lambda _name: "origin/dev")
        assert plugin._resolve_base(config) == "origin/dev"

    def test_falls_back_to_default_when_absent(self) -> None:
        def _missing(_name: str) -> str:
            raise ValueError

        assert plugin._resolve_base(SimpleNamespace(getoption=_missing)) == "origin/main"

    def test_falls_back_to_default_when_empty(self) -> None:
        assert plugin._resolve_base(SimpleNamespace(getoption=lambda _name: None)) == "origin/main"


class TestCollectionHookWrapperForceKeeps:
    """The wrapper must survive an inner deselection of a protected item (single session)."""

    def _drive(self, items: list[SimpleNamespace], deselect: object, config: SimpleNamespace) -> None:
        gen = pytest_collection_modifyitems(config, items)
        next(gen)  # advance to the yield — protected items are now hidden from inner hooks
        items[:] = [i for i in items if i is not deselect]  # simulate the tach plugin deselecting
        with pytest.raises(StopIteration):
            gen.send(None)  # resume — the finally re-inserts the protected items

    def test_a_protected_item_the_inner_hook_dropped_is_re_added(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        protected = _item(tmp_path, "tests/quality/test_floor.py")
        unrelated = _item(tmp_path, "tests/teatree_other/test_z.py")
        items = [protected, unrelated]
        monkeypatch.setattr(plugin, "force_keep_for", lambda _root, _base: ForceKeep(paths=("tests/quality",)))
        config = SimpleNamespace(rootpath=tmp_path, getoption=lambda _name: "origin/main")

        # The inner hook (tach) tries to drop the protected floor-dir item.
        self._drive(items, deselect=protected, config=config)

        nodeids = {i.nodeid for i in items}
        assert "tests/quality/test_floor.py" in nodeids  # force-kept despite the inner drop
        assert "tests/teatree_other/test_z.py" in nodeids  # inner-kept item stays

    def test_an_inner_drop_of_an_unprotected_item_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        protected = _item(tmp_path, "tests/quality/test_floor.py")
        unrelated = _item(tmp_path, "tests/teatree_other/test_z.py")
        items = [protected, unrelated]
        monkeypatch.setattr(plugin, "force_keep_for", lambda _root, _base: ForceKeep(paths=("tests/quality",)))
        config = SimpleNamespace(rootpath=tmp_path, getoption=lambda _name: "origin/main")

        # The inner hook drops the UNPROTECTED item — it must stay dropped.
        self._drive(items, deselect=unrelated, config=config)

        nodeids = {i.nodeid for i in items}
        assert "tests/teatree_other/test_z.py" not in nodeids
        assert "tests/quality/test_floor.py" in nodeids
