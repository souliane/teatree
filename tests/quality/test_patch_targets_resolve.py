"""Fitness function: every string-based ``patch`` target resolves against live modules.

The rename-sweep blind spot (souliane/teatree#2048, from the #2046 cycle-break): a
``patch("old.dotted.path")`` or ``patch.object(module_alias, "attr")`` left after a
module move applies to a DEAD name, so the test passes VACUOUSLY — the §5c
import-based rename sweep cannot see a string target, and CI's ``--exitfirst``
masks the second offending file behind the first. This converts the whole
stale-patch-target class from review-luck to a deterministic gate.

Two halves:

:class:`TestLiveTree` is the gate itself: it resolves every resolvable patch
string target in ``tests/`` and ``src/`` against the live module tree
(``importlib`` + ``getattr``, mirroring :func:`unittest.mock._get_target`) and
asserts zero unresolved targets. A regression that resurrects a dead string
target turns it red.

:class:`TestGoldenCorpus` proves the scanner is neither vacuous nor
over-blocking against a committed ``*.py.txt`` corpus: a must-FLAG set (dead
dotted target, dead ``patch.object`` attribute) and a symmetric must-NOT-FLAG
set (live target, ``create=True``, a non-module ``patch.object`` first arg, an
allowlist pragma) — the must-NOT-FLAG set is what proves the gate cannot
false-positive on legitimate dynamic targets.
"""

from pathlib import Path

import pytest

from teatree.quality.patch_targets import PatchTargetFinding, resolve_patch_target, scan_file, scan_source, scan_tree

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOTS = (_REPO_ROOT / "tests", _REPO_ROOT / "src")

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "patch_targets"
_MUST_FLAG = sorted((_FIXTURES / "must_flag").glob("*.py.txt"))
_MUST_NOT_FLAG = sorted((_FIXTURES / "must_not_flag").glob("*.py.txt"))


class TestLiveTree:
    def test_every_patch_string_target_resolves(self) -> None:
        findings = scan_tree(_SCAN_ROOTS)
        unresolved = [f for f in findings if f.reason is not None]
        assert not unresolved, "stale string-based patch target(s):\n" + "\n".join(
            f"  {f.path.relative_to(_REPO_ROOT)}:{f.lineno}: patch({f.target!r}) — {f.reason}" for f in unresolved
        )


class TestResolver:
    def test_live_dotted_target_resolves(self) -> None:
        assert resolve_patch_target("teatree.config.load_config") is None

    def test_live_module_only_target_resolves(self) -> None:
        assert resolve_patch_target("teatree.config") is None

    def test_dead_attribute_on_live_module_is_unresolved(self) -> None:
        reason = resolve_patch_target("teatree.config.this_attr_does_not_exist")
        assert reason is not None

    def test_dead_module_is_unresolved(self) -> None:
        reason = resolve_patch_target("teatree.no_such_module.attr")
        assert reason is not None

    def test_stdlib_target_resolves(self) -> None:
        assert resolve_patch_target("importlib.metadata.entry_points") is None

    def test_bare_module_target_resolves(self) -> None:
        assert resolve_patch_target("sys") is None

    def test_bare_dead_module_is_unresolved(self) -> None:
        assert resolve_patch_target("no_such_top_level_module") is not None


class TestGoldenCorpus:
    def test_corpus_has_both_dimensions(self) -> None:
        assert _MUST_FLAG, "must-FLAG corpus is empty"
        assert _MUST_NOT_FLAG, "must-NOT-FLAG corpus is empty (over-block dimension missing)"

    @pytest.mark.parametrize("fixture", _MUST_FLAG, ids=[p.stem for p in _MUST_FLAG])
    def test_must_flag_fixture_has_unresolved_finding(self, fixture: Path) -> None:
        findings = scan_file(fixture)
        unresolved = [f for f in findings if f.reason is not None]
        assert unresolved, f"{fixture.name} should produce an unresolved finding but did not"

    @pytest.mark.parametrize("fixture", _MUST_NOT_FLAG, ids=[p.stem for p in _MUST_NOT_FLAG])
    def test_must_not_flag_fixture_has_no_unresolved_finding(self, fixture: Path) -> None:
        findings = scan_file(fixture)
        unresolved = [f for f in findings if f.reason is not None]
        assert not unresolved, f"{fixture.name} wrongly flagged: " + ", ".join(
            f"patch({f.target!r}) — {f.reason}" for f in unresolved
        )


class TestFormCoverage:
    def test_finding_carries_location_and_target(self) -> None:
        findings = scan_file(_FIXTURES / "must_flag" / "dead_dotted_target.py.txt")
        assert findings
        finding = findings[0]
        assert isinstance(finding, PatchTargetFinding)
        assert finding.lineno > 0
        assert finding.target

    def test_patch_object_with_single_arg_is_skipped(self) -> None:
        findings = scan_source("from unittest.mock import patch\npatch.object(config)\n", Path("x.py"))
        assert findings == []

    def test_scan_tree_skips_nonexistent_root(self) -> None:
        assert scan_tree([_REPO_ROOT / "does_not_exist"]) == []
