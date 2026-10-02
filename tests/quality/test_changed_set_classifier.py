# test-path: cross-cutting
"""FULL-trigger classifier + changed_lanes parity for ``teatree.quality.changed_set`` (#122).

``changed_set`` is the single source of truth both the push gate (#122) and the
CI-lane classifier (``scripts/ci/changed_lanes.py``) consume, so lane routing and
push routing never drift (architecture check #8 — one normalizer). These tests
pin the ordered FULL-trigger table, the fail-safe-unknown default, the scopable
cases, and that ``changed_lanes`` imports the SAME hoisted config sets.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.ci import changed_lanes
from teatree.quality import changed_set
from teatree.quality.changed_set import (
    ChangedSet,
    ChangedSetError,
    ChangeEntry,
    _parse_name_status,
    changed_paths,
    classify,
)


def _changed(*entries: tuple[str, str]) -> ChangedSet:
    return ChangedSet(entries=tuple(ChangeEntry(status=s, path=p) for s, p in entries), base_ref="origin/main")


class TestFullTriggerTable:
    """Every §2.4 category forces a whole-tree FULL run, err-to-FULL by design."""

    def test_astgrep_rule_edit_is_full(self) -> None:
        trigger = classify(_changed(("M", ".ast-grep/blocking/except-swallow-to-empty.yml")))
        assert trigger.full
        assert ".ast-grep" in trigger.reason

    def test_regression_manifest_yaml_is_full(self) -> None:
        trigger = classify(_changed(("M", "src/teatree/quality/regression_rules.yaml")))
        assert trigger.full
        assert "manifest" in trigger.reason

    def test_regression_catalog_py_is_full(self) -> None:
        trigger = classify(_changed(("M", "src/teatree/quality/regression_catalog.py")))
        assert trigger.full
        assert "manifest" in trigger.reason

    def test_conftest_is_full(self) -> None:
        trigger = classify(_changed(("M", "tests/conftest.py")))
        assert trigger.full
        assert "conftest" in trigger.reason

    def test_nested_conftest_is_full(self) -> None:
        assert classify(_changed(("M", "tests/quality/conftest.py"))).full

    def test_pyproject_is_full(self) -> None:
        trigger = classify(_changed(("M", "pyproject.toml")))
        assert trigger.full
        assert "config" in trigger.reason

    def test_migration_is_full(self) -> None:
        trigger = classify(_changed(("A", "src/teatree/core/migrations/0099_thing.py")))
        assert trigger.full
        assert "migration" in trigger.reason

    def test_max_migration_txt_is_full(self) -> None:
        assert classify(_changed(("M", "src/teatree/core/migrations/max_migration.txt"))).full

    def test_config_exact_is_full(self) -> None:
        assert classify(_changed(("M", "uv.lock"))).full
        assert classify(_changed(("M", "manage.py"))).full
        assert classify(_changed(("M", ".pre-commit-config.yaml"))).full

    def test_config_prefix_is_full(self) -> None:
        assert classify(_changed(("M", ".github/workflows/ci.yml"))).full
        assert classify(_changed(("M", "dev/push-gate.sh"))).full

    def test_config_suffix_is_full(self) -> None:
        assert classify(_changed(("M", "some/thing.cfg"))).full
        assert classify(_changed(("M", "some/thing.ini"))).full

    def test_non_python_under_src_is_full(self) -> None:
        trigger = classify(_changed(("M", "src/teatree/eval/corpus/foo.yaml")))
        assert trigger.full
        assert "non-python" in trigger.reason

    def test_non_python_under_tests_is_full(self) -> None:
        assert classify(_changed(("M", "tests/fixtures/foo.json"))).full

    def test_python_outside_modelled_roots_is_full(self) -> None:
        # ast-grep blocking rules scan hooks/scripts (the hook_router fixture), so a
        # .py change there could introduce an out-of-scope finding — FULL, not scoped.
        trigger = classify(_changed(("M", "hooks/scripts/hook_router.py")))
        assert trigger.full
        assert "outside" in trigger.reason
        assert classify(_changed(("M", "scripts/ci/changed_lanes.py"))).full

    def test_delete_is_full(self) -> None:
        trigger = classify(_changed(("D", "src/teatree/core/gone.py")))
        assert trigger.full
        assert "delete" in trigger.reason or "rename" in trigger.reason

    def test_rename_is_full(self) -> None:
        assert classify(_changed(("R", "src/teatree/core/renamed.py"))).full

    def test_unclassifiable_path_is_full(self) -> None:
        trigger = classify(_changed(("M", "some/weird/artifact.xyz")))
        assert trigger.full
        assert "fail-safe" in trigger.reason or "unclassifiable" in trigger.reason


class TestScopableCases:
    """A provably-local diff scopes; a scoped run is the exception, not the default."""

    def test_src_python_scopes_to_itself(self) -> None:
        trigger = classify(_changed(("M", "src/teatree/core/session.py")))
        assert not trigger.full
        assert Path("src/teatree/core/session.py") in trigger.scoped_src
        assert trigger.scoped_tests == ()

    def test_test_python_scopes_to_itself(self) -> None:
        trigger = classify(_changed(("M", "tests/teatree_core/test_session.py")))
        assert not trigger.full
        assert Path("tests/teatree_core/test_session.py") in trigger.scoped_tests
        assert trigger.scoped_src == ()

    def test_markdown_outside_code_dirs_is_ignored_not_full(self) -> None:
        # A .md file cannot affect a src doctest or a python ast-grep rule, so a
        # docs-only touch alongside a src .py must NOT force FULL — else the common
        # "src change + BLUEPRINT/README update" PR would never scope.
        trigger = classify(_changed(("M", "src/teatree/core/session.py"), ("M", "BLUEPRINT.md"), ("M", "docs/foo.md")))
        assert not trigger.full
        assert Path("src/teatree/core/session.py") in trigger.scoped_src

    def test_added_src_python_scopes(self) -> None:
        trigger = classify(_changed(("A", "src/teatree/quality/changed_set.py")))
        assert not trigger.full
        assert Path("src/teatree/quality/changed_set.py") in trigger.scoped_src


class TestChangedLanesParity:
    """The FULL-trigger config sets are hoisted here; changed_lanes imports them back."""

    def test_config_sets_are_the_same_objects(self) -> None:
        assert changed_lanes._CONFIG_EXACT is changed_set.CONFIG_EXACT
        assert changed_lanes._CONFIG_PREFIXES is changed_set.CONFIG_PREFIXES
        assert changed_lanes._CONFIG_SUFFIXES is changed_set.CONFIG_SUFFIXES

    def test_both_modules_agree_config_forces_full(self) -> None:
        # Every config path changed_lanes routes to all=True, changed_set routes to FULL.
        for path in ("pyproject.toml", "uv.lock", ".github/workflows/ci.yml", ".ast-grep/blocking/x.yml"):
            assert changed_lanes.classify([path]).all, path
            assert classify(_changed(("M", path))).full, path


class TestChangedPathsGather:
    """``changed_paths`` unions merge-base diff + staged + unstaged + untracked, with D/R status."""

    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)  # noqa: S607 — git on PATH

    def _init_repo(self, root: Path) -> None:
        self._git(root, "init", "-q", "-b", "main")
        self._git(root, "config", "user.email", "t@example.com")
        self._git(root, "config", "user.name", "t")
        (root / "base.py").write_text("x = 1\n", encoding="utf-8")
        self._git(root, "add", "-A")
        self._git(root, "commit", "-qm", "base")
        self._git(root, "branch", "origin/main")

    def test_gathers_committed_staged_unstaged_untracked(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path)
        (tmp_path / "committed.py").write_text("y = 2\n", encoding="utf-8")
        self._git(tmp_path, "add", "committed.py")
        self._git(tmp_path, "commit", "-qm", "committed change")
        (tmp_path / "staged.py").write_text("z = 3\n", encoding="utf-8")
        self._git(tmp_path, "add", "staged.py")
        (tmp_path / "base.py").write_text("x = 99\n", encoding="utf-8")  # unstaged
        (tmp_path / "untracked.py").write_text("w = 4\n", encoding="utf-8")

        changed = changed_paths(base_ref="origin/main", cwd=tmp_path)
        paths = set(changed.paths)
        assert {"committed.py", "staged.py", "base.py", "untracked.py"} <= paths

    def test_delete_is_captured_as_delete_status(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path)
        self._git(tmp_path, "rm", "-q", "base.py")
        self._git(tmp_path, "commit", "-qm", "remove base")
        changed = changed_paths(base_ref="origin/main", cwd=tmp_path)
        assert changed.has_delete_or_rename
        assert classify(changed).full


class TestChangedPathsFailSafe:
    """A git failure (R7 dirty/shallow merge-base) raises, so the caller forces FULL."""

    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)  # noqa: S607 — git on PATH

    def test_unresolvable_base_ref_raises(self, tmp_path: Path) -> None:
        self._git(tmp_path, "init", "-q", "-b", "main")
        self._git(tmp_path, "config", "user.email", "t@example.com")
        self._git(tmp_path, "config", "user.name", "t")
        (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
        self._git(tmp_path, "add", "-A")
        self._git(tmp_path, "commit", "-qm", "c")
        with pytest.raises(ChangedSetError, match="failed"):
            changed_paths(base_ref="no-such-ref", cwd=tmp_path)

    def test_empty_merge_base_raises(self, tmp_path: Path) -> None:
        empty = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(changed_set, "run_allowed_to_fail", return_value=empty),
            pytest.raises(ChangedSetError, match="empty merge-base"),
        ):
            changed_paths(base_ref="origin/main", cwd=tmp_path)

    def test_parse_name_status_skips_blank_lines(self) -> None:
        entries: set[ChangeEntry] = set()
        _parse_name_status("M\ta.py\n\nA\tb.py\n", entries)
        assert entries == {ChangeEntry("M", "a.py"), ChangeEntry("A", "b.py")}
