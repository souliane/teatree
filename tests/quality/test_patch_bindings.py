# test-path: cross-cutting — scans the whole tests/ + src/ tree; no single-module mirror.
"""A patch that RESOLVES can still reach a binding the subject under test never reads.

The sibling `test_patch_targets_resolve.py` proves every patch string resolves. That is
the check that was already green when the dogfood bug shipped:
`patch("teatree.core.overlay_loader.get_overlay")` resolves, and the module under test
held its own module-level `from ... import get_overlay`, so the patch replaced a name the
code never reads while the real call ran behind a bare `except`.

Two halves, mirroring the sibling. :class:`TestLiveTree` is the gate. The tmp-tree corpus
is the anti-vacuity + over-block proof: a real src package and a real mirrored test tree,
one file per shape, because the precision of this check lives entirely in the shapes it
declines to flag — a deferred import re-imports at call time and IS reached, an imported
name nothing reads cannot be missed, and a test file naming no single subject is out of
scope by construction.
"""

from pathlib import Path

import pytest

from teatree.quality.patch_bindings import PatchBindingFinding, mirrored_subject, module_index, scan_tree

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SRC = {
    "acme/__init__.py": "",
    "acme/definer.py": "def helper() -> int:\n    return 1\n",
    # Module-level import + a real read: the patch on `definer` cannot reach this binding.
    "acme/consumer.py": "from acme.definer import helper\n\n\ndef run() -> int:\n    return helper()\n",
    # Call-time import: the name is re-fetched from `definer`, so the patch DOES reach it.
    "acme/late.py": "def run() -> int:\n    from acme.definer import helper\n\n    return helper()\n",
    # Imported for re-export and never read: there is no second binding to miss.
    "acme/unused.py": 'from acme.definer import helper\n\n__all__ = ["helper"]\n',
    "acme/aliased.py": "from acme.definer import helper as _h\n\n\ndef run() -> int:\n    return _h()\n",
    "acme/consumer_both.py": "from acme.definer import helper\n\n\ndef run() -> int:\n    return helper()\n",
    "acme/declared.py": "from acme.definer import helper\n\n\ndef run() -> int:\n    return helper()\n",
    "acme/pkg/__init__.py": "",
    "acme/pkg/nested.py": "from acme.definer import helper\n\n\ndef run() -> int:\n    return helper()\n",
}

_PATCH = (
    "from unittest.mock import patch\n\n\n"
    'def test_it() -> None:\n    with patch("acme.definer.helper"):\n        pass\n'
)

_TESTS = {
    "test_consumer.py": _PATCH,
    "test_late.py": _PATCH,
    "test_unused.py": _PATCH,
    "test_aliased.py": _PATCH,
    "acme_pkg/test_nested.py": _PATCH,
    # Patches the subject's OWN binding as well, so nothing is left unreached.
    "test_consumer_both.py": (
        "from unittest.mock import patch\n\n\ndef test_it() -> None:\n"
        '    with patch("acme.definer.helper"), patch("acme.consumer_both.helper"):\n        pass\n'
    ),
    # A deliberate defining-module patch declares itself.
    "test_declared.py": (
        "from unittest.mock import patch\n\n\ndef test_it() -> None:\n"
        '    with patch("acme.definer.helper"):  # patch-binding: defining-module\n        pass\n'
    ),
    # No mirrored subject: out of scope by construction.
    "conformance/test_whatever.py": _PATCH,
}


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, Path]:
    src, tests = tmp_path / "src", tmp_path / "tests"
    for tree, files in ((src, _SRC), (tests, _TESTS)):
        for relative, body in files.items():
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
    return tests, src


def _flagged(corpus: tuple[Path, Path]) -> set[str]:
    tests, src = corpus
    return {f.path.relative_to(tests).as_posix() for f in scan_tree(tests, src, package="acme")}


class TestLiveTree:
    def test_every_patch_reaches_its_subjects_binding(self) -> None:
        findings = scan_tree(_REPO_ROOT / "tests", _REPO_ROOT / "src")
        assert not findings, "patch(es) that miss the binding the module under test reads:\n" + "\n".join(
            f"  {f.path.relative_to(_REPO_ROOT)}:{f.lineno}: patch({f.target!r}) never reaches "
            f"{f.unreached_binding}, which is what the subject reads. Patch that binding instead, or "
            f"declare the intent with a `# patch-binding: defining-module` comment on the call."
            for f in findings
        )

    def test_the_live_scan_reaches_a_meaningful_number_of_subjects(self) -> None:
        # Anti-vacuity: the gate above is green over an EMPTY scan too. Coverage is
        # partial by design (only a test path that mirrors one module names a subject),
        # so the floor is what makes "partial" different from "none".
        index = module_index(_REPO_ROOT / "src", "teatree")
        tests_root = _REPO_ROOT / "tests"
        subjects = [
            path for path in tests_root.rglob("test_*.py") if mirrored_subject(path, tests_root, index, "teatree")
        ]
        assert len(subjects) >= 500, len(subjects)


class TestTheCheckFires:
    def test_a_module_level_binding_the_subject_reads_is_flagged(self, corpus: tuple[Path, Path]) -> None:
        assert "test_consumer.py" in _flagged(corpus)

    def test_a_nested_mirror_is_flagged_too(self, corpus: tuple[Path, Path]) -> None:
        assert "acme_pkg/test_nested.py" in _flagged(corpus)

    def test_an_aliased_import_is_still_a_second_binding(self, corpus: tuple[Path, Path]) -> None:
        assert "test_aliased.py" in _flagged(corpus)

    def test_the_finding_names_the_binding_the_patch_never_reaches(self, corpus: tuple[Path, Path]) -> None:
        tests, src = corpus
        finding = next(f for f in scan_tree(tests, src, package="acme") if f.path.name == "test_consumer.py")
        assert isinstance(finding, PatchBindingFinding)
        assert finding.unreached_binding == "acme.consumer.helper"
        assert finding.lineno > 0


class TestTheCheckStaysOutOfTheWay:
    @pytest.mark.parametrize(
        "relative",
        [
            pytest.param("test_late.py", id="a_call_time_import_is_reached_by_the_patch"),
            pytest.param("test_unused.py", id="a_binding_nothing_reads_cannot_be_missed"),
            pytest.param("test_consumer_both.py", id="the_subjects_own_binding_is_patched_too"),
            pytest.param("test_declared.py", id="a_declared_defining_module_patch"),
            pytest.param("conformance/test_whatever.py", id="a_test_file_naming_no_single_subject"),
        ],
    )
    def test_a_reaching_patch_is_not_flagged(self, corpus: tuple[Path, Path], relative: str) -> None:
        assert relative not in _flagged(corpus)


class TestSubjectDerivation:
    def test_a_top_level_module_mirrors_the_tests_root(self, corpus: tuple[Path, Path]) -> None:
        tests, src = corpus
        index = module_index(src, "acme")
        assert mirrored_subject(tests / "test_consumer.py", tests, index, "acme") == "acme.consumer"

    def test_a_package_module_mirrors_the_prefixed_directory(self, corpus: tuple[Path, Path]) -> None:
        tests, src = corpus
        index = module_index(src, "acme")
        subject = mirrored_subject(tests / "acme_pkg" / "test_nested.py", tests, index, "acme")
        assert subject == "acme.pkg.nested"

    def test_a_path_naming_no_module_has_no_subject(self, corpus: tuple[Path, Path]) -> None:
        tests, src = corpus
        index = module_index(src, "acme")
        assert mirrored_subject(tests / "conformance" / "test_whatever.py", tests, index, "acme") is None
