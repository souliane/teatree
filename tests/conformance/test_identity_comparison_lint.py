"""The identity-comparison guard: no raw phase/overlay comparison survives in src.

Conformance gate for the identity-normalization ('slug bug') family (teatree
integration audit #20/#24). The live gate asserts ``src/teatree`` carries no raw
comparison; the synthetic lanes prove the guard is anti-vacuous — it goes RED on
the exact bug shape and GREEN on the normalized form — so a future raw
comparison fails CI instead of silently mis-firing a phase branch.
"""

from pathlib import Path

from teatree.core.modelkit.phases import CANONICAL_PHASES, SUBAGENT_BY_PHASE
from teatree.quality.identity_comparison_lint import (
    IDENTITY_COMPARISON_FAMILIES,
    PHASE_MEMBERS,
    FamilyKind,
    IdentityFamily,
    scan_source,
    scan_tree,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "teatree"

_PHASE_FAMILY = next(f for f in IDENTITY_COMPARISON_FAMILIES if f.name == "phase")


def _scan(source: str) -> list[str]:
    return [v.family for v in scan_source(source, Path("<test>"))]


class TestLiveTreeIsClean:
    def test_src_has_no_raw_identity_comparison(self) -> None:
        violations = scan_tree([_SRC])
        rendered = "\n".join(
            f"  {v.path.relative_to(_REPO_ROOT)}:{v.lineno} [{v.family}] {v.snippet}" for v in violations
        )
        assert not violations, f"raw identity comparison(s) — route through the family normalizer:\n{rendered}"


class TestPhaseFamilyAntiVacuity:
    """The guard must catch the exact #20 shape and clear the normalized form."""

    def test_raw_phase_literal_compare_is_flagged(self) -> None:
        # The pre-fix attempt_recorder shape: an un-normalized phase vs a canonical literal.
        assert _scan('if effective_phase != "planning":\n    pass\n') == ["phase"]

    def test_raw_attribute_phase_compare_is_flagged(self) -> None:
        # The pre-fix prompt.py shape: a raw ``.phase`` attribute vs a canonical literal.
        assert _scan('if task.phase == "coding":\n    pass\n') == ["phase"]

    def test_direct_normalize_phase_call_is_clean(self) -> None:
        assert _scan('if normalize_phase(task.phase) == "coding":\n    pass\n') == []

    def test_name_assigned_from_normalizer_is_clean(self) -> None:
        # The task.py / lifecycle.py shape: compare a name proven normalized in-file.
        assert _scan('phase = normalize_phase(self.phase)\nif phase == "reviewing":\n    pass\n') == []

    def test_walrus_normalized_name_is_clean(self) -> None:
        assert _scan('if (phase := normalize_phase(raw)) == "shipping":\n    pass\n') == []

    def test_two_literals_are_not_flagged(self) -> None:
        assert _scan('if "coding" == "planning":\n    pass\n') == []

    def test_short_verb_literal_is_not_a_phase_member(self) -> None:
        # A short verb (a lifecycle-skill name, a do-step name) is not a phase.
        assert _scan('if lifecycle_skill == "review":\n    pass\n') == []

    def test_is_operator_against_phase_literal_is_flagged(self) -> None:
        assert _scan('if effective_phase is not "planning":\n    pass\n') == ["phase"]

    def test_membership_test_is_out_of_scope(self) -> None:
        assert _scan('if phase in ("coding", "testing"):\n    pass\n') == []


class TestOverlayFamilyAntiVacuity:
    """The overlay guard flags a bare ``.overlay``-vs-literal comparison only."""

    def test_raw_overlay_attr_vs_literal_is_flagged(self) -> None:
        assert _scan('if worktree.overlay == "t3-teatree":\n    pass\n') == ["overlay"]

    def test_overlay_vs_blank_sentinel_is_clean(self) -> None:
        # ``overlay == ""`` is the ambient-single-overlay default check, never a bug.
        assert _scan('if ticket.overlay == "":\n    pass\n') == []

    def test_overlay_vs_another_stored_name_is_clean(self) -> None:
        assert _scan("if ticket.overlay == self.overlay_name:\n    pass\n") == []

    def test_overlay_vs_name_is_clean(self) -> None:
        assert _scan("if ticket.overlay == overlay:\n    pass\n") == []

    def test_canonicalized_overlay_vs_literal_is_clean(self) -> None:
        assert _scan('if resolve_overlay_name(ticket.overlay) == "t3-teatree":\n    pass\n') == []


class TestPragmaAndSelfExclusion:
    def test_pragma_line_is_exempt(self) -> None:
        assert _scan('if task.phase == "coding":  # identity-lint: ok\n    pass\n') == []

    def test_scan_tree_skips_the_guard_module(self) -> None:
        # The guard names phase members in its own registry; scanning src must not flag itself.
        assert not [v for v in scan_tree([_SRC]) if v.path.name == "identity_comparison_lint.py"]

    def test_scan_tree_skips_nonexistent_root(self) -> None:
        assert scan_tree([_REPO_ROOT / "does_not_exist"]) == []


class TestRegistryIsAdditiveAndWellFormed:
    def test_families_are_registered(self) -> None:
        names = {f.name for f in IDENTITY_COMPARISON_FAMILIES}
        assert {"phase", "overlay"} <= names

    def test_phase_family_members_meet_cardinality_floor(self) -> None:
        # Anti-vacuity: a refactor that empties the member set cannot silence the guard.
        assert len(_PHASE_FAMILY.literal_members) >= 10
        assert {"planning", "coding", "reviewing", "shipping", "testing"} <= _PHASE_FAMILY.literal_members
        assert "normalize_phase" in _PHASE_FAMILY.normalizer_calls

    def test_short_verbs_are_excluded_from_members(self) -> None:
        assert not ({"code", "test", "review", "ship", "plan"} & _PHASE_FAMILY.literal_members)

    def test_a_new_family_plugs_in_without_a_checker_edit(self) -> None:
        # The singleton-literal guard shape: no normalizer, so any comparison is flagged.
        singleton = IdentityFamily(
            name="singleton",
            kind=FamilyKind.LITERAL_MEMBER,
            normalizer_calls=frozenset(),
            literal_members=frozenset({"worker", "teatree-worker"}),
        )
        flagged = [v.family for v in scan_source('if name == "teatree-worker":\n    pass\n', Path("<x>"), [singleton])]
        assert flagged == ["singleton"]


class TestPhaseMembersMatchTheSsot:
    """The vendored PHASE_MEMBERS cannot silently drift from the live phase vocabulary.

    ``teatree.quality`` is a foundation layer and must not import the
    ``teatree.core.modelkit`` domain (tach), so the lint vendors the phase
    member set as a literal. This test — which lives in ``tests`` and may import
    the domain freely — is the drift detector: a new canonical or dispatchable
    phase fails here until the vendored copy is updated.
    """

    def test_vendored_members_equal_the_live_vocabulary(self) -> None:
        live = frozenset(CANONICAL_PHASES) | {phase for _role, phase in SUBAGENT_BY_PHASE}
        assert live == PHASE_MEMBERS
