"""Fitness function: every teatree-shaped reference a skill makes resolves against the tree.

A skill's worked example that names a plausible-but-absent teatree module, path,
or symbol is indistinguishable from a work item to an agent skimming the skill —
and unlike a stale import or a stale patch target, nothing mechanical catches it.

:class:`TestLiveTree` is the gate: it resolves every teatree-shaped reference in
``skills/**/*.md`` and asserts zero unresolved ones.

:class:`TestCharterDocs` extends the same walk to the documents every agent loads
BEFORE any skill — ``BLUEPRINT.md``, ``AGENTS.md``, ``CLAUDE.md`` and the
``docs/blueprint/`` appendices — which the skills-only scan could not see at all.
Their currently-unresolved references are pinned in
:data:`_KNOWN_UNRESOLVED_CHARTER_REFS`, a shrink-only ratchet asserted in BOTH
directions: the listed ones are visible instead of invisible, a NEW stale citation
reds, and a listed one that got FIXED reds until its entry is deleted.

:class:`TestGoldenCorpus` proves the scanner is neither vacuous nor
over-blocking against a committed ``*.md.txt`` corpus — a must-FLAG set (absent
path, absent module, absent attribute, absent imported name, an absent bare
module-local symbol beside a RESOLVING path on the same line, an absent symbol
in a repo script, an absent repo path outside ``src/teatree/``, a path-qualified
symbol its live module does not carry, and one cited under a directory that does
not carry it) and a symmetric must-NOT-FLAG set (live path, live dotted name,
live import, a live bare module-local symbol, a live path-qualified symbol, the
pragma in both its line and block scopes, a config-section header, a filename
tail, a glob, and the third-party / attribute-access tokens the module-local
widening must never sweep in).
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
    resolve_path_qualified_symbol,
    resolve_repo_path,
    scan_file,
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


#: The charter documents an agent reads before any skill. A stale symbol here is
#: read as a work item exactly as one in a skill is — more so, since these load first.
_CHARTER_DOCS: list[Path] = [
    _REPO_ROOT / "BLUEPRINT.md",
    _REPO_ROOT / "AGENTS.md",
    _REPO_ROOT / "CLAUDE.md",
    *sorted((_REPO_ROOT / "docs" / "blueprint").glob("*.md")),
]

#: ``(document, reference)`` pairs that do not resolve today. Some are genuinely
#: stale citations, some are tokens the scanner cannot yet tell apart from an
#: importable name (an entry-point group, a config-section header) — the remedy for
#: the latter is a ``skill-symbol-ref:`` pragma on the citing line. Either way the
#: set may only ever SHRINK: fixing an entry means deleting its line here, and a NEW
#: unresolved reference fails the ratchet.
_KNOWN_UNRESOLVED_CHARTER_REFS: frozenset[tuple[str, str]] = frozenset(
    {
        ("AGENTS.md", "teatree.overlays"),
        ("BLUEPRINT.md", "teatree.harnesses"),
        ("BLUEPRINT.md", "teatree.overlays"),
        ("BLUEPRINT.md", "teatree.teams"),
        ("BLUEPRINT.md", "teatree.utils.django_db.refresh_reference_snapshot"),
        ("CLAUDE.md", "teatree.overlays"),
        ("docs/blueprint/configuration.md", "teatree.Gates.Quality"),
        ("docs/blueprint/configuration.md", "teatree.log"),
        ("docs/blueprint/loop-topology.md", "teatree.loops.master.build_loop_table_jobs"),
    },
)


def _unresolved_charter_refs() -> set[tuple[str, str]]:
    """Every ``(charter doc, unresolved reference)`` pair the scanner reports right now."""
    return {
        (str(doc.relative_to(_REPO_ROOT)), finding.ref)
        for doc in _CHARTER_DOCS
        if doc.is_file()
        for finding in _unresolved(scan_file(doc, _REPO_ROOT))
    }


class TestCharterDocs:
    def test_no_new_charter_document_reference_is_unresolved(self) -> None:
        new = _unresolved_charter_refs() - _KNOWN_UNRESOLVED_CHARTER_REFS
        assert new == set(), (
            "charter document(s) naming a symbol the tree does not have — an agent reads "
            f"these before any skill, so a stale citation reads as a work item: {sorted(new)}"
        )

    def test_no_known_charter_reference_is_stale(self) -> None:
        """The other direction: an entry the scanner no longer reports must be deleted.

        A lingering entry re-widens the ratchet — it would grandfather a future stale
        citation of the same symbol in the same doc. It also proves each recorded pair
        still resolves to a real scan result rather than exempting nothing.
        """
        stale = _KNOWN_UNRESOLVED_CHARTER_REFS - _unresolved_charter_refs()
        assert stale == set(), (
            "Pinned charter reference(s) the scanner no longer reports as unresolved — "
            f"delete them from _KNOWN_UNRESOLVED_CHARTER_REFS so the ratchet stays tight: {sorted(stale)}"
        )

    def test_the_charter_documents_are_actually_walked(self) -> None:
        # The ratchet above is satisfied by scanning nothing, so pin that the walk
        # reaches real content: BLUEPRINT.md alone cites dozens of live symbols.
        resolved = [f for f in scan_file(_REPO_ROOT / "BLUEPRINT.md", _REPO_ROOT) if f.reason is None]
        assert len(resolved) > 10, "BLUEPRINT.md yielded almost no teatree-shaped references — the walk is broken"


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

    def test_path_qualified_symbol_resolves(self) -> None:
        ref = "hooks/scripts/main_clone_guard.handle_block_main_clone_mutation"
        assert resolve_path_qualified_symbol(ref, build_repo_index(_REPO_ROOT)) is None

    def test_path_qualified_symbol_resolves_under_a_src_rooted_qualifier(self) -> None:
        ref = "src/teatree/agents/sdk_tool_map.CAPABILITY_TO_SDK_TOOLS"
        assert resolve_path_qualified_symbol(ref, build_repo_index(_REPO_ROOT)) is None

    def test_path_qualified_symbol_the_module_lacks_is_unresolved(self) -> None:
        ref = "agents/model_tiering.no_such_tier_helper"
        assert resolve_path_qualified_symbol(ref, build_repo_index(_REPO_ROOT)) is not None

    def test_a_directory_that_does_not_carry_the_module_cannot_vouch_for_it(self) -> None:
        # `main_clone_guard` is a basename two directories ship; only the hook leaf
        # carries the handler, so the qualifier has to discriminate rather than be stripped.
        index = build_repo_index(_REPO_ROOT)
        ref = "core/gates/main_clone_guard.handle_block_main_clone_mutation"
        assert resolve_path_qualified_symbol(ref, index) is not None
        assert resolve_module_local("main_clone_guard.handle_block_main_clone_mutation", index) is None

    def test_a_file_path_names_no_symbol_reading(self) -> None:
        # The module is live, so attempting the symbol reading would report a missing
        # `py` attribute on it — the tail names a file, and only the path reading applies.
        reason = resolve_path_qualified_symbol("agents/harness.py", build_repo_index(_REPO_ROOT))
        assert reason is not None
        assert "teatree.agents.harness" not in reason

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

    def test_a_failed_path_reference_reports_both_readings_it_admits(self) -> None:
        fixture = _FIXTURES / "must_flag" / "path_qualified_absent_symbol.md.txt"
        (finding,) = _unresolved(scan_source(fixture.read_text(encoding="utf-8"), fixture, _REPO_ROOT))
        assert finding.reason is not None
        assert "no such path in the tree" in finding.reason
        assert "no_such_tier_helper" in finding.reason

    def test_a_plain_file_path_reports_only_the_path_reading(self) -> None:
        fixture = _FIXTURES / "must_flag" / "absent_repo_path.md.txt"
        (finding,) = _unresolved(scan_source(fixture.read_text(encoding="utf-8"), fixture, _REPO_ROOT))
        assert finding.reason == "no such path in the tree"

    @pytest.mark.parametrize("fixture", _MUST_NOT_FLAG, ids=lambda p: p.name)
    def test_must_not_flag(self, fixture: Path) -> None:
        findings = scan_source(fixture.read_text(encoding="utf-8"), fixture, _REPO_ROOT)
        unresolved = _unresolved(findings)
        assert not unresolved, f"{fixture.name} must not be flagged: {[f.ref for f in unresolved]}"
