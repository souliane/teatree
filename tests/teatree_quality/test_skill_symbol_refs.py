"""Fitness function: every teatree-shaped reference a skill makes resolves against the tree.

A skill's worked example that names a plausible-but-absent teatree module, path,
or symbol is indistinguishable from a work item to an agent skimming the skill —
and unlike a stale import or a stale patch target, nothing mechanical catches it.

:class:`TestLiveTree` is the gate: it resolves every teatree-shaped reference in
``skills/**/*.md`` and asserts zero unresolved ones.

:class:`TestGoldenCorpus` proves the scanner is neither vacuous nor
over-blocking against a committed ``*.md.txt`` corpus — a must-FLAG set (absent
path, absent module, absent attribute, absent imported name, an absent bare
module-local symbol beside a RESOLVING path on the same line, an absent symbol
in a repo script, an absent repo path outside ``src/teatree/``) and a symmetric
must-NOT-FLAG set (live path, live dotted name, live import, a live bare
module-local symbol, the pragma in both its line and block scopes, a
config-section header, a filename tail, a glob, and the third-party /
attribute-access tokens the module-local widening must never sweep in).
"""

from dataclasses import replace
from pathlib import Path

import pytest

from teatree.quality.skill_symbol_refs import (
    RepoIndex,
    SymbolRefFinding,
    build_repo_index,
    resolve_dotted,
    resolve_module_local,
    resolve_repo_path,
    scan_source,
    scan_tree,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_ROOT = _REPO_ROOT / "skills"


_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "skill_symbol_refs"
_MUST_FLAG = sorted((_FIXTURES / "must_flag").glob("*.md.txt"))
_MUST_NOT_FLAG = sorted((_FIXTURES / "must_not_flag").glob("*.md.txt"))


def _unresolved(findings: list[SymbolRefFinding]) -> list[SymbolRefFinding]:
    return [finding for finding in findings if finding.reason is not None]


def _ambiguous_index() -> RepoIndex:
    """One basename mapping to two modules — the shape ~150 real basenames have."""
    return replace(
        build_repo_index(_REPO_ROOT),
        modules={"probe": ("teatree.core.modelkit.phases", "teatree.quality.skill_symbol_refs")},
    )


class TestLiveTree:
    def test_every_skill_symbol_reference_resolves(self) -> None:
        unresolved = _unresolved(scan_tree(_SKILLS_ROOT, _REPO_ROOT))
        assert not unresolved, "skill reference(s) naming a symbol the tree does not have:\n" + "\n".join(
            f"  {f.path.relative_to(_REPO_ROOT)}:{f.lineno}: {f.ref} — {f.reason}" for f in unresolved
        )


class TestResolver:
    def test_live_path_resolves(self) -> None:
        assert resolve_repo_path("src/teatree/quality/skill_symbol_refs.py", _REPO_ROOT) is None

    def test_absent_path_is_unresolved(self) -> None:
        assert resolve_repo_path("src/teatree/core/session.py", _REPO_ROOT) is not None

    def test_live_module_resolves(self) -> None:
        assert resolve_dotted("teatree.quality.skill_symbol_refs") is None

    def test_live_attribute_chain_resolves(self) -> None:
        assert resolve_dotted("teatree.quality.skill_symbol_refs.SymbolRefFinding.path") is None

    def test_absent_module_is_unresolved(self) -> None:
        assert resolve_dotted("teatree.notify") is not None

    def test_absent_attribute_is_unresolved(self) -> None:
        assert resolve_dotted("teatree.quality.skill_symbol_refs.no_such_helper") is not None

    def test_no_importable_prefix_is_unresolved(self) -> None:
        assert resolve_dotted("teatreeish_package_that_does_not_exist.thing") is not None

    def test_live_module_local_symbol_resolves(self) -> None:
        assert resolve_module_local("skill_symbol_refs.PRAGMA", build_repo_index(_REPO_ROOT)) is None

    def test_absent_module_local_symbol_is_unresolved(self) -> None:
        assert resolve_module_local("skill_symbol_refs.NO_SUCH", build_repo_index(_REPO_ROOT)) is not None

    def test_module_local_symbol_in_a_repo_script_resolves(self) -> None:
        assert resolve_module_local("hook_router._teatree_engaged", build_repo_index(_REPO_ROOT)) is None

    def test_head_the_tree_does_not_ship_is_unresolved(self) -> None:
        assert resolve_module_local("typer.Exit", build_repo_index(_REPO_ROOT)) == "no module named 'typer' in the tree"

    def test_ambiguous_basename_resolves_when_any_module_carries_it(self) -> None:
        assert resolve_module_local("probe.PRAGMA", _ambiguous_index()) is None

    def test_ambiguous_basename_reports_every_reading(self) -> None:
        reason = resolve_module_local("probe.NO_SUCH", _ambiguous_index())
        assert reason is not None
        assert "teatree.core.modelkit.phases" in reason
        assert "teatree.quality.skill_symbol_refs" in reason

    def test_module_raising_on_import_reports_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "skill_ref_probe_boom.py").write_text('raise RuntimeError("boom")\n', encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        assert resolve_dotted("skill_ref_probe_boom") == "RuntimeError: boom"


class TestGoldenCorpus:
    @pytest.mark.parametrize("fixture", _MUST_FLAG, ids=lambda p: p.name)
    def test_must_flag(self, fixture: Path) -> None:
        findings = scan_source(fixture.read_text(encoding="utf-8"), fixture, _REPO_ROOT)
        assert _unresolved(findings), f"{fixture.name} must be flagged but was not"

    def test_a_resolving_reference_does_not_vouch_for_a_broken_one_beside_it(self) -> None:
        fixture = _FIXTURES / "must_flag" / "bare_module_local.md.txt"
        findings = scan_source(fixture.read_text(encoding="utf-8"), fixture, _REPO_ROOT)
        assert len({finding.lineno for finding in findings}) == 1
        assert {finding.ref: finding.reason is None for finding in findings} == {
            "src/teatree/core/modelkit/phase_tools.py": True,
            "phase_tools.PHASE_TOOLS": False,
        }

    @pytest.mark.parametrize("fixture", _MUST_NOT_FLAG, ids=lambda p: p.name)
    def test_must_not_flag(self, fixture: Path) -> None:
        findings = scan_source(fixture.read_text(encoding="utf-8"), fixture, _REPO_ROOT)
        unresolved = _unresolved(findings)
        assert not unresolved, f"{fixture.name} must not be flagged: {[f.ref for f in unresolved]}"
