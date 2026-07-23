"""Documentation-impact classification (#3645).

The binding invariant: a docs-only path is proven to have NO executable semantics,
so it must not escalate the selection to the whole tree — but every test that
genuinely READS the changed doc must still be selected. Under-selecting a
doc-consistency test is the failure this module must never produce.
"""

from pathlib import Path

import pytest

from teatree.quality.doc_impact import disk_doc_reader_lookup, is_doc_path, reference_tokens


class TestIsDocPath:
    @pytest.mark.parametrize(
        "path",
        [
            "BLUEPRINT.md",
            "README.md",
            "docs/blueprint/appendix.md",
            "docs/generated/antipattern-catalog.md",
            "docs/dashboard.png",
            "skills/rules/SKILL.md",
            "mkdocs.yml",
        ],
    )
    def test_documentation_paths_are_doc_paths(self, path: str) -> None:
        assert is_doc_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "src/teatree/quality/affected_tests.py",
            "src/teatree/quality/antipatterns.yaml",
            "src/teatree/core/README.md",
            "tests/fixtures/corpus.md",
            "tests/conftest.py",
            "docs/conf.py",
            "pyproject.toml",
            "dev/ci-parity.sh",
            "scripts/ci/changed_lanes.py",
        ],
    )
    def test_executable_or_in_root_paths_are_not_doc_paths(self, path: str) -> None:
        assert not is_doc_path(path)


class TestReferenceTokens:
    def test_root_doc_yields_its_own_name(self) -> None:
        assert reference_tokens(["BLUEPRINT.md"]) == frozenset({"BLUEPRINT.md"})

    def test_nested_doc_yields_path_basename_and_every_directory_prefix(self) -> None:
        assert reference_tokens(["docs/generated/catalog.md"]) == frozenset(
            {"docs/generated/catalog.md", "catalog.md", "docs/generated/", "docs/"}
        )

    def test_tokens_union_across_several_docs(self) -> None:
        tokens = reference_tokens(["BLUEPRINT.md", "skills/rules/SKILL.md"])
        assert {"BLUEPRINT.md", "SKILL.md", "skills/", "skills/rules/"} <= tokens


class TestDiskDocReaderLookup:
    def _tree(self, root: Path) -> None:
        (root / "tests" / "teatree_quality").mkdir(parents=True)
        (root / "tests" / "teatree_quality" / "test_blueprint_sync.py").write_text(
            'BLUEPRINT = Path(__file__).parents[2] / "BLUEPRINT.md"\n', encoding="utf-8"
        )
        (root / "tests" / "teatree_quality" / "test_unrelated.py").write_text("assert True\n", encoding="utf-8")

    def test_selects_the_test_that_names_the_changed_doc(self, tmp_path: Path) -> None:
        self._tree(tmp_path)
        lookup = disk_doc_reader_lookup(tmp_path)
        assert lookup(reference_tokens(["BLUEPRINT.md"])) == ("tests/teatree_quality/test_blueprint_sync.py",)

    def test_leaves_out_a_test_that_never_names_the_doc(self, tmp_path: Path) -> None:
        self._tree(tmp_path)
        lookup = disk_doc_reader_lookup(tmp_path)
        assert "tests/teatree_quality/test_unrelated.py" not in lookup(reference_tokens(["BLUEPRINT.md"]))

    def test_no_changed_doc_selects_nothing(self, tmp_path: Path) -> None:
        self._tree(tmp_path)
        assert disk_doc_reader_lookup(tmp_path)(frozenset()) == ()

    def test_an_unreadable_candidate_is_skipped_not_crashed(self, tmp_path: Path) -> None:
        # A path whose name matches the *.py glob but cannot be read (here: it is a
        # directory) must be swallowed so the scan still selects the genuine reader.
        self._tree(tmp_path)
        (tmp_path / "tests" / "teatree_quality" / "bogus.py").mkdir()
        lookup = disk_doc_reader_lookup(tmp_path)
        assert lookup(reference_tokens(["BLUEPRINT.md"])) == ("tests/teatree_quality/test_blueprint_sync.py",)


class TestRealRepoDocConsistencyTestsAreMapped:
    """The mapping is not vacuous.

    This repo HAS doc-consistency tests, and a ``BLUEPRINT.md`` edit must still
    select them.
    """

    def test_blueprint_edit_selects_the_real_blueprint_tests(self) -> None:
        root = Path(__file__).resolve().parents[2]
        selected = disk_doc_reader_lookup(root)(reference_tokens(["BLUEPRINT.md"]))
        assert "tests/test_blueprint_readme_pr_sync.py" in selected
        assert "tests/test_check_blueprint_sync_hook.py" in selected
