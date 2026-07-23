"""Safety-biased incremental test selection (#113).

The pure core :func:`select` runs on synthetic dependents maps and test-import maps
so the fixture matrix is deterministic and needs no real tach/disk. The binding
invariant across every case: the selection OVER-selects (a superset of the mirror
path unioned with the direct/transitive importers) and NEVER under-selects — any
change the classifier cannot prove local degrades to a whole-tree FULL run.
"""

from pathlib import Path
from typing import ClassVar

import pytest

from teatree.quality import affected_tests as mod
from teatree.quality.affected_tests import (
    FLOOR_DIRS,
    Selection,
    SelectionSources,
    TachUnavailableError,
    build_selection,
    classify_selection,
    module_of,
    select,
)
from teatree.quality.changed_set import ChangedSet, ChangedSetError, ChangeEntry

_TACH_DOWN = "tach not found"
_MERGE_BASE_DOWN = "empty merge-base"
_TACH_MUST_NOT_RUN = "tach must not run once the verdict is already FULL"


def _changed(*entries: tuple[str, str]) -> ChangedSet:
    return ChangedSet(entries=tuple(ChangeEntry(status=s, path=p) for s, p in entries), base_ref="origin/main")


def _select(
    changed: ChangedSet,
    *,
    dependents: dict[str, list[str]] | None = None,
    test_imports: dict[str, tuple[str, ...]] | None = None,
    mirror: dict[str, str] | None = None,
    doc_readers: dict[str, tuple[str, ...]] | None = None,
) -> Selection:
    readers = doc_readers or {}

    def doc_reader_lookup(tokens: frozenset[str]) -> tuple[str, ...]:
        return tuple(sorted({test for token in tokens for test in readers.get(token, ())}))

    return select(
        changed=changed,
        sources=SelectionSources(
            dependents_map=dependents or {},
            test_imports=test_imports or {},
            mirror_lookup=(mirror or {}).get,
            doc_reader_lookup=doc_reader_lookup,
        ),
        floor_dirs=FLOOR_DIRS,
    )


class TestModuleOf:
    def test_module_file_maps_to_dotted_module(self) -> None:
        assert module_of("src/teatree/foo/bar.py") == "teatree.foo.bar"

    def test_package_init_maps_to_package_module(self) -> None:
        assert module_of("src/teatree/foo/__init__.py") == "teatree.foo"

    def test_non_src_or_non_python_path_maps_to_none(self) -> None:
        assert module_of("tests/teatree_foo/test_bar.py") is None
        assert module_of("src/teatree/foo/data.yaml") is None


class TestOverSelectsNeverUnder:
    def test_changed_src_selects_superset_of_mirror_and_importers(self) -> None:
        changed = _changed(("M", "src/teatree/foo/bar.py"))
        dependents = {
            "src/teatree/foo/bar.py": ["src/teatree/baz.py"],  # baz transitively depends on bar
            "src/teatree/baz.py": [],
        }
        test_imports = {
            "tests/teatree_foo/test_bar.py": ("teatree.foo.bar",),  # direct importer of the change
            "tests/teatree_baz/test_baz.py": ("teatree.baz",),  # importer of the transitive dependent
            "tests/teatree_other/test_other.py": ("teatree.other",),  # unrelated
        }
        mirror = {"teatree.foo.bar": "tests/teatree_foo/test_bar.py"}
        sel = _select(changed, dependents=dependents, test_imports=test_imports, mirror=mirror)

        assert not sel.full
        assert "tests/teatree_foo/test_bar.py" in sel.test_files
        assert "tests/teatree_baz/test_baz.py" in sel.test_files
        # Scoping is meaningful — an unrelated test is not swept in when scoping is provable.
        assert "tests/teatree_other/test_other.py" not in sel.test_files

    def test_ancestor_import_of_changed_module_still_matches(self) -> None:
        # ``from teatree.foo import bar`` records the module as ``teatree.foo`` (the
        # AST-granularity gap). The change is ``teatree.foo.bar`` — the test must still
        # be selected (over-select on the hierarchy, never miss).
        changed = _changed(("M", "src/teatree/foo/bar.py"))
        test_imports = {"tests/teatree_foo/test_bar.py": ("teatree.foo",)}
        sel = _select(changed, test_imports=test_imports)
        assert "tests/teatree_foo/test_bar.py" in sel.test_files

    def test_floor_dirs_present_in_every_scoped_run(self) -> None:
        sel = _select(_changed(("M", "src/teatree/foo/bar.py")))
        assert not sel.full
        assert sel.floor_dirs == FLOOR_DIRS
        for floor in FLOOR_DIRS:
            assert floor in sel.pytest_args()

    def test_mirror_file_included_even_when_import_scan_misses_it(self) -> None:
        changed = _changed(("M", "src/teatree/foo/bar.py"))
        # No test imports the module; only the mirror-path cross-check finds it.
        mirror = {"teatree.foo.bar": "tests/teatree_foo/test_bar.py"}
        sel = _select(changed, test_imports={}, mirror=mirror)
        assert "tests/teatree_foo/test_bar.py" in sel.test_files
        assert sel.warnings  # belt-and-braces inclusion is surfaced

    def test_floor_dir_test_is_not_double_listed(self) -> None:
        changed = _changed(("M", "src/teatree/foo/bar.py"))
        test_imports = {"tests/quality/test_bar_contract.py": ("teatree.foo.bar",)}
        sel = _select(changed, test_imports=test_imports)
        # Already covered by the floor dir — never listed as an individual file arg.
        assert "tests/quality/test_bar_contract.py" not in sel.test_files


class TestChangedTestFileSelectsItself:
    def test_changed_test_file_selects_itself_scoped(self) -> None:
        sel = _select(_changed(("M", "tests/teatree_foo/test_bar.py")))
        assert not sel.full
        assert "tests/teatree_foo/test_bar.py" in sel.test_files
        assert sel.floor_dirs == FLOOR_DIRS


class TestUnclassifiableForcesFull:
    @pytest.mark.parametrize(
        "entry",
        [
            ("M", "src/teatree/foo/corpus.yaml"),  # non-.py data file under src
            ("M", "tests/teatree_foo/fixture.json"),  # non-.py data file under tests
            ("M", "scripts/ci/thing.py"),  # python outside the modelled roots
            ("M", "hooks/scripts/gate.py"),  # hook python outside the graph
            ("M", "e2e/spec/flow.py"),  # e2e python outside the modelled roots
            ("M", "src/teatree/foo/notes.md"),  # markdown under a code root is fixture data
        ],
    )
    def test_unclassifiable_change_forces_full(self, entry: tuple[str, str]) -> None:
        sel = _select(_changed(entry))
        assert sel.full
        assert sel.reason

    def test_deletion_forces_full(self) -> None:
        sel = _select(_changed(("D", "src/teatree/foo/bar.py")))
        assert sel.full
        assert "delete" in sel.reason.lower() or "rename" in sel.reason.lower()

    def test_rename_forces_full(self) -> None:
        assert _select(_changed(("R", "src/teatree/foo/renamed.py"))).full

    def test_conftest_forces_full(self) -> None:
        assert _select(_changed(("M", "tests/teatree_foo/conftest.py"))).full

    def test_factories_forces_full(self) -> None:
        sel = _select(_changed(("M", "tests/factories.py")))
        assert sel.full
        assert "factories" in sel.reason.lower()

    def test_django_settings_forces_full(self) -> None:
        sel = _select(_changed(("M", "tests/django_settings.py")))
        assert sel.full
        assert "settings" in sel.reason.lower()

    def test_tests_config_dir_forces_full(self) -> None:
        assert _select(_changed(("M", "tests/config/test_autonomy.py"))).full

    def test_pyproject_config_forces_full(self) -> None:
        assert _select(_changed(("M", "pyproject.toml"))).full

    def test_migration_forces_full_and_create_db(self) -> None:
        sel = _select(_changed(("M", "src/teatree/core/migrations/0002_thing.py")))
        assert sel.full
        assert sel.create_db
        assert "--create-db" in sel.pytest_args()

    def test_any_full_trigger_in_a_mixed_diff_forces_full(self) -> None:
        # A scopable src change plus one unclassifiable file ⇒ the whole diff is FULL.
        sel = _select(_changed(("M", "src/teatree/foo/bar.py"), ("M", "tests/conftest.py")))
        assert sel.full


class TestExplainChain:
    def test_explain_traces_changed_seed_through_to_the_test(self) -> None:
        changed = _changed(("M", "src/teatree/foo/bar.py"))
        dependents = {"src/teatree/foo/bar.py": ["src/teatree/baz.py"], "src/teatree/baz.py": []}
        test_imports = {"tests/teatree_baz/test_baz.py": ("teatree.baz",)}
        sel = _select(changed, dependents=dependents, test_imports=test_imports)
        chain = next(r.chain for r in sel.reasons if r.test == "tests/teatree_baz/test_baz.py")
        joined = " ".join(chain)
        assert "src/teatree/foo/bar.py" in joined  # the changed seed
        assert "src/teatree/baz.py" in joined  # the transitive dependent
        assert "teatree.baz" in joined  # the import that selected the test

    def test_self_changed_test_reason_is_recorded(self) -> None:
        sel = _select(_changed(("M", "tests/teatree_foo/test_bar.py")))
        reason = next(r for r in sel.reasons if r.test == "tests/teatree_foo/test_bar.py")
        assert reason.kind == "self-changed"

    def test_explain_for_unselected_test_reports_not_selected(self) -> None:
        sel = _select(_changed(("M", "tests/teatree_foo/test_bar.py")))
        assert sel.explain("tests/teatree_other/test_nope.py") == [
            "tests/teatree_other/test_nope.py: not selected by this diff"
        ]


class TestDocsOnlyChangeIsScopedNotFull:
    """#3645: a docs-only path no longer escalates to the whole tree.

    Replaces the old blanket ``BLUEPRINT.md``/``SKILL.md`` FULL cases. Coverage is
    preserved, not dropped: the doc's own readers are selected instead, which is a
    STRICTLY better guard than a whole-tree run nobody could afford to wait for.
    """

    _BLUEPRINT_READERS: ClassVar[dict[str, tuple[str, ...]]] = {"BLUEPRINT.md": ("tests/test_blueprint_sync.py",)}

    def test_blueprint_edit_alone_does_not_force_full(self) -> None:
        sel = _select(_changed(("M", "BLUEPRINT.md")), doc_readers=self._BLUEPRINT_READERS)
        assert not sel.full

    def test_blueprint_edit_selects_the_tests_that_read_it(self) -> None:
        sel = _select(_changed(("M", "BLUEPRINT.md")), doc_readers=self._BLUEPRINT_READERS)
        assert "tests/test_blueprint_sync.py" in sel.test_files

    def test_doc_reader_selection_records_its_reason(self) -> None:
        sel = _select(_changed(("M", "BLUEPRINT.md")), doc_readers=self._BLUEPRINT_READERS)
        assert [r.kind for r in sel.reasons] == ["doc-read"]

    def test_a_doc_with_no_reader_selects_only_the_floor(self) -> None:
        sel = _select(_changed(("M", "docs/blueprint/appendix.md")))
        assert not sel.full
        assert sel.test_files == ()
        assert sel.pytest_args() == list(FLOOR_DIRS)

    def test_docs_tree_and_mkdocs_config_are_scoped_too(self) -> None:
        assert not _select(_changed(("M", "docs/dashboard.md"), ("M", "mkdocs.yml"))).full

    def test_skill_markdown_is_scoped(self) -> None:
        assert not _select(_changed(("M", "skills/rules/SKILL.md"))).full

    def test_changed_docs_are_reported_on_the_selection(self) -> None:
        sel = _select(_changed(("M", "BLUEPRINT.md")), doc_readers=self._BLUEPRINT_READERS)
        assert sel.changed_docs == ("BLUEPRINT.md",)

    def test_src_change_plus_required_blueprint_edit_stays_scoped(self) -> None:
        # The vicious interaction the ticket names: the blueprint-sync gate COMPELS
        # the doc edit, and that edit used to compel the whole suite.
        sel = _select(
            _changed(("M", "src/teatree/foo/bar.py"), ("M", "BLUEPRINT.md")),
            test_imports={"tests/teatree_foo/test_bar.py": ("teatree.foo.bar",)},
            doc_readers=self._BLUEPRINT_READERS,
        )
        assert not sel.full
        assert {"tests/teatree_foo/test_bar.py", "tests/test_blueprint_sync.py"} <= set(sel.test_files)
        assert sel.doctest_targets == ("src/teatree/foo/bar.py",)

    def test_a_real_full_trigger_beside_a_doc_still_forces_full(self) -> None:
        assert _select(_changed(("M", "BLUEPRINT.md"), ("M", "tests/conftest.py"))).full

    def test_a_deleted_doc_is_still_scoped_and_selects_its_readers(self) -> None:
        sel = _select(_changed(("D", "BLUEPRINT.md")), doc_readers=self._BLUEPRINT_READERS)
        assert not sel.full
        assert "tests/test_blueprint_sync.py" in sel.test_files

    def test_a_deleted_source_file_beside_a_doc_still_forces_full(self) -> None:
        assert _select(_changed(("D", "src/teatree/foo/bar.py"), ("M", "BLUEPRINT.md"))).full


class TestClassifySelection:
    def test_scoped_src_change_is_not_full(self) -> None:
        verdict = classify_selection(_changed(("M", "src/teatree/foo/bar.py")))
        assert not verdict.full
        assert [str(p) for p in verdict.scoped_src] == ["src/teatree/foo/bar.py"]


class TestPytestArgs:
    def test_full_no_migration_emits_no_positional_args(self) -> None:
        assert _select(_changed(("M", "pyproject.toml"))).pytest_args() == []

    def test_scoped_emits_files_then_floor_last(self) -> None:
        changed = _changed(("M", "src/teatree/foo/bar.py"))
        test_imports = {"tests/teatree_foo/test_bar.py": ("teatree.foo.bar",)}
        sel = _select(changed, test_imports=test_imports)
        args = sel.pytest_args()
        assert "tests/teatree_foo/test_bar.py" in args
        assert args[-len(FLOOR_DIRS) :] == list(FLOOR_DIRS)

    def test_scoped_run_carries_doctest_parity_for_changed_modules(self) -> None:
        # Match the CI shard flags: the changed src modules' doctests run locally.
        changed = _changed(("M", "src/teatree/foo/bar.py"))
        test_imports = {"tests/teatree_foo/test_bar.py": ("teatree.foo.bar",)}
        args = _select(changed, test_imports=test_imports).pytest_args()
        assert "--doctest-modules" in args
        assert "src/teatree/foo/bar.py" in args
        # The doctest flag precedes its target module path.
        assert args.index("--doctest-modules") < args.index("src/teatree/foo/bar.py")

    def test_cloned_test_db_swaps_create_db_for_reuse_db(self) -> None:
        # souliane/teatree#3326: once the opt-in template clone has refreshed the
        # test DB, a migration diff must NOT replay (--create-db would wipe the
        # fresh clone) — it reuses the already-current DB instead.
        sel = _select(_changed(("M", "src/teatree/core/migrations/0002_thing.py")))
        assert sel.create_db
        assert "--create-db" in sel.pytest_args()
        cloned = sel.pytest_args(test_db_cloned=True)
        assert "--reuse-db" in cloned
        assert "--create-db" not in cloned

    def test_cloned_flag_is_inert_without_create_db(self) -> None:
        # No migration ⇒ create_db is False ⇒ the clone flag adds no DB arg either way.
        sel = _select(_changed(("M", "src/teatree/foo/bar.py")))
        assert not sel.create_db
        assert "--reuse-db" not in sel.pytest_args(test_db_cloned=True)
        assert "--create-db" not in sel.pytest_args(test_db_cloned=True)


class TestBuildSelectionFailSafe:
    def test_tach_unavailable_forces_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "changed_paths", lambda base_ref, cwd: _changed(("M", "src/teatree/foo/bar.py")))

        def _boom(root: Path) -> dict[str, list[str]]:
            raise TachUnavailableError(_TACH_DOWN)

        monkeypatch.setattr(mod, "run_tach_dependents_map", _boom)
        sel = build_selection(Path("/nonexistent"), base_ref="origin/main")
        assert sel.full
        assert "tach" in sel.reason.lower()

    def test_dirty_merge_base_forces_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(base_ref: str, cwd: Path) -> ChangedSet:
            raise ChangedSetError(_MERGE_BASE_DOWN)

        monkeypatch.setattr(mod, "changed_paths", _boom)
        sel = build_selection(Path("/nonexistent"), base_ref="origin/main")
        assert sel.full
        assert "changed set" in sel.reason.lower()

    def test_full_verdict_skips_tach_entirely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "changed_paths", lambda base_ref, cwd: _changed(("M", "pyproject.toml")))

        def _must_not_run(root: Path) -> dict[str, list[str]]:
            raise AssertionError(_TACH_MUST_NOT_RUN)

        monkeypatch.setattr(mod, "run_tach_dependents_map", _must_not_run)
        assert build_selection(Path("/nonexistent"), base_ref="origin/main").full


class TestRunTachDependentsMapResolution:
    """An absent ``tach`` binary is a TachUnavailableError (→ FULL), never a raw crash.

    A globally-installed ``t3`` whose venv predates the ``tach`` dependency has no
    ``tach`` on PATH nor beside its interpreter. The fail-safe contract says an
    unavailable tach map degrades to a whole-tree FULL run; a raw ``FileNotFoundError``
    escaping the selector crashes ``dev/test-affected.sh`` (and thus the mandated
    ``dev/ci-parity-fast.sh``) instead.
    """

    def test_missing_tach_degrades_to_tach_unavailable(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # No tach on PATH, and none beside the interpreter.
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setattr(mod.sys, "executable", str(tmp_path / "python"))
        with pytest.raises(TachUnavailableError):
            mod.run_tach_dependents_map(tmp_path)

    def test_tach_beside_the_interpreter_is_used_when_off_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The uv-tool-installed t3 keeps tach in its venv bin (beside python) but not
        # on PATH; the resolver must find it there so the selection can still scope.
        interpreter = tmp_path / "python"
        interpreter.touch()
        adjacent_tach = tmp_path / "tach"
        adjacent_tach.write_text("#!/bin/sh\necho '{}'\n")
        adjacent_tach.chmod(0o755)
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(mod.sys, "executable", str(interpreter))
        assert mod.run_tach_dependents_map(tmp_path) == {}
