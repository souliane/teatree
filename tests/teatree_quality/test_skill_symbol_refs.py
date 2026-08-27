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

:class:`TestPythonProse` extends it again to Python docstrings and ``#:``
comments under ``src/teatree`` and ``hooks`` — the surface a `:func:` citation of
a function that MOVED modules shipped through, because the markdown walk cannot
read a ``.py`` file at all. Its ratchet is
:data:`_KNOWN_UNRESOLVED_PYTHON_PROSE_REFS`, two-sided for the same reason.

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

import importlib
import sys
from collections.abc import Iterator
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

import pytest

import teatree
from teatree.quality.python_prose_refs import prose_lines, scan_python_source, scan_python_tree
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


#: The retired ``teams`` pane, whose leftover directory made BLUEPRINT's citation of it resolve.
_PHANTOM_REF = "teatree.teams"


@pytest.fixture
def phantom_subpackage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Plant the observed shape: a retired module's leftover ``__pycache__``-only directory.

    Python reads any bare directory on a package's ``__path__`` as an implicit
    namespace package, so this is enough for ``import teatree.teams`` to succeed
    with no source behind it.
    """
    leaf = _PHANTOM_REF.rpartition(".")[2]
    (tmp_path / leaf / "__pycache__").mkdir(parents=True)
    monkeypatch.setattr(teatree, "__path__", [*teatree.__path__, str(tmp_path)])
    monkeypatch.delitem(sys.modules, _PHANTOM_REF, raising=False)
    importlib.invalidate_caches()
    yield
    sys.modules.pop(_PHANTOM_REF, None)
    # import binds the child onto the parent package too; popping sys.modules alone
    # leaves that attribute, which later resolves the phantom for every other test.
    if hasattr(teatree, leaf):
        delattr(teatree, leaf)


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


#: The docstring shape that shipped to ``main``: ``_retire_superseded`` moved into
#: ``transient_requeue_disposal`` and this citation stayed on the old home.
_MOVED_FUNCTION_DOCSTRING = '''"""Requeue a transient failure.

The disposal half is :func:`teatree.loop.transient_requeue._retire_superseded`.
"""
'''

#: ``(module, reference)`` pairs that do not resolve today, seeded so the walk can
#: land without a tree-wide cleanup first. Some are genuinely stale citations, some
#: are illustrative stand-ins, some are tokens the scanner cannot tell apart from an
#: importable name (an entry-point group, a Django settings attribute) — the remedy
#: for the last is a ``skill-symbol-ref:`` pragma on the citing line. Either way the
#: set may only ever SHRINK, asserted in both directions below.
_KNOWN_UNRESOLVED_PYTHON_PROSE_REFS: frozenset[tuple[str, str]] = frozenset(
    {
        ("hooks/scripts/session_start_skills.py", "scripts/lib/skill_loader.suggest_skills"),
        ("hooks/scripts/worker_supervisor.py", "teatree.config.settings._parse_env_bool"),
        ("src/teatree/_overlay_api.py", "teatree.overlays"),
        ("src/teatree/agents/attempt_recorder.py", "prompt._REVIEW_VERDICT_RETURN_LINES"),
        ("src/teatree/agents/envelope_refusal.py", "agents/prompt.py"),
        ("src/teatree/agents/harness_registry.py", "teatree.harnesses"),
        ("src/teatree/agents/harness_registry.py", "teatree.overlays"),
        ("src/teatree/agents/pydantic_ai_session.py", "models._get_final_result_event"),
        ("src/teatree/agents/regulated_path.py", "teatree.config.UserSettings.regulated_path_model_allowlist"),
        ("src/teatree/agents/runner_usage.py", "agents/runner.py"),
        ("src/teatree/backends/forge_merge_rpc.py", "github._run_gh"),
        ("src/teatree/backends/gitlab/sync_terminal.py", "hooks/check_module_health.py"),
        ("src/teatree/cli/doctor/app.py", "teatree.cli.doctor._x"),
        ("src/teatree/cli/doctor/checks_environment.py", "teatree.overlays"),
        ("src/teatree/cli/enforcement_tools.py", "teatree.targetBranch"),
        ("src/teatree/cli/overlay_dev.py", "teatree.overlays"),
        ("src/teatree/cli/review/evidence_gate.py", "src/teatree/cli/foo.py"),
        ("src/teatree/cli/review/on_behalf.py", "hooks/scripts/check_module_health.py"),
        ("src/teatree/cli/update.py", "teatree.overlays"),
        ("src/teatree/config/agent_enums.py", "teatree.harnesses"),
        ("src/teatree/config/discovery.py", "teatree.overlays"),
        ("src/teatree/config/fleet_policy.py", "deploy/teatree.env"),
        ("src/teatree/config/fleet_policy.py", "teatree.env"),
        ("src/teatree/config/loader.py", "settings.T3_WORKSPACE_DIR"),
        ("src/teatree/config/loader.py", "teatree.log"),
        ("src/teatree/config/overlay_code_defaults.py", "teatree.overlays"),
        ("src/teatree/config/reviewer_identities.py", "teatree.core.models.merge_clear.REVIEWER_ROLE_COMPONENTS"),
        ("src/teatree/core/cleanup/cleanup_emit.py", "src/gate.py"),
        ("src/teatree/core/code_tokens.py", "teatree.core.tasks.claim"),
        ("src/teatree/core/gates/review_request_guard.py", "slack._iter_review_matches"),
        ("src/teatree/core/harness_todos.py", "hooks/scripts/hook_router._newest_task_agent_id"),
        ("src/teatree/core/management/commands/run.py", "src/foo.py"),
        ("src/teatree/core/management/commands/ticket_short_describe.py", "teatree.core.ticket_short_description"),
        (
            "src/teatree/core/merge/clear_reconcile.py",
            "teatree.core.models.pull_request.PullRequest.record_forge_merge",
        ),
        ("src/teatree/core/merge/merge_response.py", "execution._is_transient_merge_response"),
        ("src/teatree/core/modelkit/diff_scope.py", "src/other_paths.py"),
        ("src/teatree/core/modelkit/diff_scope.py", "src/paths.py"),
        ("src/teatree/core/models/merge_clear.py", "src/teatree/core/merger/"),
        ("src/teatree/core/models/mechanism_sketch.py", "src/teatree/../overlays/x.py"),
        ("src/teatree/core/models/review_verdict.py", "teatree.core.review.diff_scope_gate"),
        ("src/teatree/core/review/diff_scope_probe.py", "teatree.core.review.diff_scope_gate"),
        ("src/teatree/core/views/github_webhook.py", "settings.TEATREE_GITHUB_WEBHOOK_SECRET"),
        ("src/teatree/core/views/gitlab_webhook.py", "settings.TEATREE_GITLAB_WEBHOOK_TOKEN"),
        ("src/teatree/eval/corpus_models.py", "teatree.eval.scenarios"),
        ("src/teatree/eval/git_fixture.py", "src/teatree/util/money.py"),
        ("src/teatree/eval/git_fixture.py", "teatree.util.money"),
        ("src/teatree/eval/models.py", "teatree.eval.api_runner.resolve_max_turns_override"),
        ("src/teatree/eval/pydantic_ai_runner.py", "teatree.agents.harness._X_LANE_HEADER"),
        ("src/teatree/hooks/publish_destination.py", "public_visibility._destination_visibility"),
        ("src/teatree/hooks/quote_scanner.py", "hook_router._agent_prompt_skip_token"),
        ("src/teatree/loop/rendering.py", "teatree.loop.rendering.X"),
        ("src/teatree/loop/scanners/issue_disposition.py", "src/teatree/foo.py"),
        ("src/teatree/loop/scanners/self_update_ci.py", "teatree.loop.scanners.pr_sweep.GhPrApiClient"),
        ("src/teatree/loop/substrate_pinger.py", "teatree.loop.scanners.pr_sweep.SubstratePinger"),
        ("src/teatree/loop/transient_requeue.py", "teatree.loop.config_self_repair.SELF_REPAIR_STAMP"),
        ("src/teatree/loops/dream/recall.py", "hook_router._AMBIENT_CONTEXT_RE"),
        ("src/teatree/overlay_sdk/factory.py", "teatree.agents._"),
        ("src/teatree/quality/affected_tests.py", "src/teatree/foo/bar.py"),
        ("src/teatree/quality/affected_tests.py", "teatree.foo.bar"),
        ("src/teatree/quality/full_suite_invocation.py", "tests/foo.py"),
        ("src/teatree/quality/mutation_run.py", "src/teatree/x.py"),
        ("src/teatree/quality/mutation_run.py", "teatree.x"),
        ("src/teatree/quality/skill_symbol_refs.py", "phase_tools.PHASE_TOOLS"),
        ("src/teatree/quality/snapshot_baseline.py", "e2e/foo.spec.ts-snapshots/"),
        ("src/teatree/self_update.py", "teatree.overlays"),
        ("src/teatree/settings.py", "teatree.overlays"),
        ("src/teatree/utils/coverage_exclusions.py", "src/myvenv_helper.py"),
        ("src/teatree/utils/django_db/testdb_clone.py", "importer._copy_ref_to_ticket"),
        ("src/teatree/utils/editable_pth.py", "teatree.overlays"),
        ("src/teatree/utils/git_branch.py", "teatree.targetBranch"),
    },
)


@lru_cache(maxsize=1)
def _python_prose_findings() -> tuple[SymbolRefFinding, ...]:
    """The whole-tree walk, run once per worker rather than once per test that reads it."""
    return tuple(scan_python_tree(_REPO_ROOT))


def _unresolved_python_prose_refs() -> set[tuple[str, str]]:
    """Every ``(module, unresolved reference)`` pair the Python-prose walk reports now."""
    return {
        (str(finding.path.relative_to(_REPO_ROOT)), finding.ref)
        for finding in _python_prose_findings()
        if finding.reason is not None
    }


class TestPythonProse:
    def test_no_new_python_prose_reference_is_unresolved(self) -> None:
        new = _unresolved_python_prose_refs() - _KNOWN_UNRESOLVED_PYTHON_PROSE_REFS
        assert new == set(), (
            "Python docstring/#: reference(s) naming a symbol the tree does not have — a reader "
            f"following one finds nothing and cannot tell renamed from moved from deleted: {sorted(new)}"
        )

    def test_no_known_python_prose_reference_is_stale(self) -> None:
        stale = _KNOWN_UNRESOLVED_PYTHON_PROSE_REFS - _unresolved_python_prose_refs()
        assert stale == set(), (
            "Pinned Python-prose reference(s) the scanner no longer reports as unresolved — "
            f"delete them so the ratchet stays tight: {sorted(stale)}"
        )

    def test_the_python_tree_is_actually_walked(self) -> None:
        # Both ratchet directions are satisfied by scanning nothing, so pin that the
        # walk reaches real content: the tree cites hundreds of live symbols in prose.
        resolved = [f for f in _python_prose_findings() if f.reason is None]
        assert len(resolved) > 100, "the Python tree yielded almost no teatree-shaped references — the walk is broken"

    def test_a_moved_function_reference_in_a_docstring_is_flagged(self) -> None:
        findings = scan_python_source(_MOVED_FUNCTION_DOCSTRING, Path("probe.py"), _REPO_ROOT)
        (finding,) = _unresolved(findings)
        assert finding.ref == "teatree.loop.transient_requeue._retire_superseded"

    def test_the_markdown_walk_cannot_see_a_python_docstring(self, tmp_path: Path) -> None:
        # The gap the guard had: its walk is `rglob("*.md")`, so the stale citation
        # above is invisible to it however the tree is arranged.
        (tmp_path / "probe.py").write_text(_MOVED_FUNCTION_DOCSTRING, encoding="utf-8")
        assert scan_tree(tmp_path, _REPO_ROOT) == []

    def test_a_module_local_symbol_the_module_lacks_is_flagged(self) -> None:
        source = '"""The claim is settled by ``skill_symbol_refs._NO_SUCH_HELPER``."""\n'
        (finding,) = _unresolved(scan_python_source(source, Path("probe.py"), _REPO_ROOT))
        assert finding.ref == "skill_symbol_refs._NO_SUCH_HELPER"

    def test_a_hash_colon_comment_is_walked(self) -> None:
        source = "#: Keyed off ``skill_symbol_refs._NO_SUCH_HELPER``.\nVALUE = 1\n"
        (finding,) = _unresolved(scan_python_source(source, Path("probe.py"), _REPO_ROOT))
        assert (finding.lineno, finding.ref) == (1, "skill_symbol_refs._NO_SUCH_HELPER")

    @pytest.mark.usefixtures("phantom_subpackage")
    def test_a_stray_directory_cannot_hide_a_stale_docstring_citation(self) -> None:
        source = f'"""The retired teams pane lived in ``{_PHANTOM_REF}``."""\n'
        (finding,) = _unresolved(scan_python_source(source, Path("probe.py"), _REPO_ROOT))
        assert finding.ref == _PHANTOM_REF

    def test_a_plain_comment_and_a_non_docstring_literal_are_not_walked(self) -> None:
        source = (
            "# Keyed off skill_symbol_refs._NO_SUCH_HELPER.\n"
            'PROMPT = "see skill_symbol_refs._NO_SUCH_HELPER"\n'
            "CALL = skill_symbol_refs._NO_SUCH_HELPER\n"
        )
        assert scan_python_source(source, Path("probe.py"), _REPO_ROOT) == []

    def test_an_unparsable_module_yields_no_prose(self) -> None:
        assert prose_lines("def broken(:\n") == frozenset()

    def test_a_leading_non_string_expression_is_not_a_docstring(self) -> None:
        assert prose_lines("42\nVALUE = 1\n") == frozenset()

    def test_a_class_and_function_docstring_are_both_prose(self) -> None:
        source = (
            '"""Module."""\n\n\nclass Probe:\n    """Class."""\n\n    def run(self) -> None:\n        """Method."""\n'
        )
        assert prose_lines(source) == frozenset({1, 5, 8})

    def test_the_pragma_exempts_a_docstring_line(self) -> None:
        source = '"""Registered under ``teatree.overlays``.  skill-symbol-ref: entry-point group."""\n'
        assert _unresolved(scan_python_source(source, Path("probe.py"), _REPO_ROOT)) == []


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

    @pytest.mark.usefixtures("phantom_subpackage")
    def test_a_source_less_namespace_package_is_not_evidence(self) -> None:
        assert resolve_dotted(_PHANTOM_REF) is not None

    @pytest.mark.usefixtures("phantom_subpackage")
    def test_a_stray_directory_cannot_hide_a_stale_charter_citation(self) -> None:
        source = f"The retired teams pane lived in ``{_PHANTOM_REF}``.\n"
        findings = scan_source(source, Path("BLUEPRINT.md"), _REPO_ROOT)
        assert [finding.ref for finding in _unresolved(findings)] == [_PHANTOM_REF]

    def test_a_package_carrying_source_still_resolves(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        package = tmp_path / "skill_ref_probe_live"
        package.mkdir()
        (package / "__init__.py").write_text("MARKER = 1\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        importlib.invalidate_caches()
        assert resolve_dotted("skill_ref_probe_live.MARKER") is None


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
