"""The merge-quality critic gate (north-star PR-4): clean+tested-enough as a real MERGE gate.

The two ``transition="merge"`` LLM items (``test_value`` + ``cleanliness``) are
judged by the async critic (mocked here as a recorded ``CriticVerdict``, exactly
like the mark_delivered semantic-item tests) and the gate is the DETERMINISTIC
row-check over that verdict. Anti-vacuity, both directions, one line each:

- a VACUOUS test -> ``test_value`` FAIL verdict -> refuse + finding.
- a genuinely-valuable non-redundant test -> ``test_value`` PASS -> merges.
- a BLOATED redundant test set -> ``test_value`` FAIL -> refuses.
- a non-clean change -> ``cleanliness`` FAIL -> refuses + finding.
- ``execute_bound_merge`` REFUSES a directive keystone with no clean verdict at the
    shipped head (RED-before: on pre-PR-4 code the same keystone merged).
- the merge items are keyed to ``merge`` — they never fire at ``mark_delivered``.

Ordinary tickets are provably unaffected unless ``require_merge_quality_verdict``
is on; the verdict is armed when absent, so the gate is satisfiable, not suppression.
"""

import contextlib
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.gates import merge_quality_gate
from teatree.core.gates.merge_quality_gate import (
    MergeQualityVerdictError,
    build_merge_quality_contract,
    check_merge_quality_verdict,
    is_directive_ticket,
    linked_directive,
    merge_quality_enforced,
    ratified_test_strategy,
)
from teatree.core.merge import MergePreconditionError, merge_ticket_pr
from teatree.core.models import (
    CriticDispatch,
    CriticFinding,
    CriticVerdict,
    Directive,
    MergeAudit,
    MergeClear,
    PlanArtifact,
    PullRequest,
    Ticket,
)
from teatree.core.models.mechanism_sketch import MechanismSketch
from teatree.core.models.plan_adequacy import all_negated_adequacy
from teatree.core.models.review_verdict import ReviewVerdict

_FORTY_HEX = "a" * 40
_OTHER_HEX = "b" * 40


def _sketch() -> MechanismSketch:
    return MechanismSketch(
        kind="setting_policy_gate",
        setting_key="max_open_prs_per_repo_per_ticket",
        setting_type="int",
        neutral_default=0,
        policy_chokepoint="src/teatree/core/gates/pr_budget_gate.py::check_pr_budget",
        activation_scope="example-overlay",
        activation_value=1,
        rejected_alternatives=("an overlay-local hook — fails the N=2 litmus",),
        acceptance_tests=("tests/teatree_core/gates/test_pr_budget_gate.py::TestBudget::test_refused",),
    )


def _directive_ticket(*, with_sketch: bool = True, link: str = "fk") -> Ticket:
    """A ticket implementing a directive — linked by the reverse FK or ``extra['directive_id']``."""
    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
    directive = Directive.objects.capture("max 1 open PR per repo per ticket", source=Directive.Source.CLI)
    if with_sketch:
        directive.mechanism_sketch = _sketch().to_dict()
        directive.save(update_fields=["mechanism_sketch"])
    if link == "fk":
        directive.ticket = ticket
        directive.save(update_fields=["ticket"])
    else:
        ticket.extra = {"directive_id": directive.pk}
        ticket.save(update_fields=["extra"])
    return ticket


def _ordinary_ticket() -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)


def _record_merge_verdict(ticket: Ticket, *, items: list[dict], head_sha: str = _FORTY_HEX) -> CriticVerdict:
    return CriticVerdict.record_from_envelope(
        ticket=ticket,
        transition="merge",
        head_sha=head_sha,
        envelope={"grader_identity": "critic-agent-7", "items": items},
    )


def _pass(slug: str) -> dict:
    return {"slug": slug, "status": "pass", "citation": f"inspected {slug}, clean at test_x.py::t"}


def _fail(slug: str, citation: str) -> dict:
    return {"slug": slug, "status": "fail", "citation": citation}


def _clean_verdict_items() -> list[dict]:
    return [_pass("test_value"), _pass("cleanliness")]


@contextlib.contextmanager
def _ordinary_enforcement(*, on: bool) -> Iterator[None]:
    with patch.object(
        merge_quality_gate,
        "get_effective_settings",
        return_value=UserSettings(require_merge_quality_verdict=on),
    ):
        yield


class TestDirectiveDetection(TestCase):
    def test_detected_via_reverse_fk(self) -> None:
        ticket = _directive_ticket(link="fk")
        assert is_directive_ticket(ticket)
        assert linked_directive(ticket) is not None

    def test_detected_via_extra_directive_id(self) -> None:
        ticket = _directive_ticket(link="extra")
        assert is_directive_ticket(ticket)

    def test_ordinary_ticket_is_not_directive(self) -> None:
        assert not is_directive_ticket(_ordinary_ticket())


class TestEnforcementScope(TestCase):
    def test_directive_ticket_is_always_enforced(self) -> None:
        with _ordinary_enforcement(on=False):  # flag off must NOT relax a directive ticket
            assert merge_quality_enforced(_directive_ticket())

    def test_ordinary_ticket_gated_only_under_the_flag(self) -> None:
        ticket = _ordinary_ticket()
        with _ordinary_enforcement(on=False):
            assert not merge_quality_enforced(ticket)
        with _ordinary_enforcement(on=True):
            assert merge_quality_enforced(ticket)


class TestVerdictGate(TestCase):
    """The fail-closed row-check — no verdict / any unmet item refuses; a clean verdict passes."""

    def test_no_verdict_refuses_a_directive_keystone(self) -> None:
        ticket = _directive_ticket()
        with pytest.raises(MergeQualityVerdictError, match="no recorded merge-quality CriticVerdict"):
            check_merge_quality_verdict(ticket, _FORTY_HEX)

    def test_clean_verdict_passes(self) -> None:
        ticket = _directive_ticket()
        _record_merge_verdict(ticket, items=_clean_verdict_items())
        check_merge_quality_verdict(ticket, _FORTY_HEX)  # no raise

    def test_vacuous_test_value_fail_refuses_and_records_a_finding(self) -> None:
        # Anti-vacuity (a): a vacuous test → test_value FAIL → refuse + finding.
        ticket = _directive_ticket()
        _record_merge_verdict(
            ticket, items=[_fail("test_value", "test_x asserts True — vacuous"), _pass("cleanliness")]
        )
        with pytest.raises(MergeQualityVerdictError, match="test_value"):
            check_merge_quality_verdict(ticket, _FORTY_HEX)
        assert CriticFinding.objects.filter(ticket=ticket, transition="merge", rubric_item="test_value").exists()

    def test_test_value_both_directions(self) -> None:
        # Anti-vacuity (b): a bloated/redundant set FAILS, a genuine non-redundant test PASSES.
        bloated = _directive_ticket()
        _record_merge_verdict(
            bloated,
            items=[_fail("test_value", "3 tests assert the same branch — redundant bloat"), _pass("cleanliness")],
        )
        with pytest.raises(MergeQualityVerdictError, match="test_value"):
            check_merge_quality_verdict(bloated, _FORTY_HEX)

        genuine = _directive_ticket()
        _record_merge_verdict(genuine, items=_clean_verdict_items())
        check_merge_quality_verdict(genuine, _FORTY_HEX)  # no raise — the both-directions proof

    def test_non_clean_cleanliness_fail_refuses_and_records_a_finding(self) -> None:
        # Anti-vacuity (c): a non-clean change (unjustified suppression / poor typing) → cleanliness FAIL.
        ticket = _directive_ticket()
        _record_merge_verdict(
            ticket,
            items=[_pass("test_value"), _fail("cleanliness", "src/x.py:3 smuggles `Any`; a # noqa hides the cause")],
        )
        with pytest.raises(MergeQualityVerdictError, match="cleanliness"):
            check_merge_quality_verdict(ticket, _FORTY_HEX)
        assert CriticFinding.objects.filter(ticket=ticket, transition="merge", rubric_item="cleanliness").exists()

    def test_omitted_item_is_unmet_and_refuses(self) -> None:
        # The anti-vacuity floor: a verdict silent on cleanliness cannot wave the merge through.
        ticket = _directive_ticket()
        _record_merge_verdict(ticket, items=[_pass("test_value")])
        with pytest.raises(MergeQualityVerdictError, match="cleanliness"):
            check_merge_quality_verdict(ticket, _FORTY_HEX)

    def test_uncited_pass_is_unmet_and_refuses(self) -> None:
        # Anti-theater: an uncited pass is downgraded to instrumentation_gap → unmet → refuse.
        ticket = _directive_ticket()
        _record_merge_verdict(
            ticket, items=[{"slug": "test_value", "status": "pass", "citation": ""}, _pass("cleanliness")]
        )
        with pytest.raises(MergeQualityVerdictError, match="test_value"):
            check_merge_quality_verdict(ticket, _FORTY_HEX)

    def test_verdict_at_a_different_head_does_not_cover(self) -> None:
        # A push after judging moves the head → the old verdict no longer covers it → refuse.
        ticket = _directive_ticket()
        _record_merge_verdict(ticket, items=_clean_verdict_items(), head_sha=_OTHER_HEX)
        with pytest.raises(MergeQualityVerdictError, match="no recorded merge-quality CriticVerdict"):
            check_merge_quality_verdict(ticket, _FORTY_HEX)

    def test_ordinary_ticket_is_a_noop_when_the_flag_is_off(self) -> None:
        ticket = _ordinary_ticket()
        with _ordinary_enforcement(on=False):
            check_merge_quality_verdict(ticket, _FORTY_HEX)  # no verdict, no raise — ordinary work unaffected

    def test_ordinary_ticket_is_gated_when_the_flag_is_on(self) -> None:
        ticket = _ordinary_ticket()
        with _ordinary_enforcement(on=True), pytest.raises(MergeQualityVerdictError):
            check_merge_quality_verdict(ticket, _FORTY_HEX)


class TestFindingsFeedBack(TestCase):
    def test_a_now_clean_head_clears_the_stale_finding(self) -> None:
        ticket = _directive_ticket()
        _record_merge_verdict(ticket, items=[_fail("test_value", "vacuous"), _pass("cleanliness")])
        with pytest.raises(MergeQualityVerdictError):
            check_merge_quality_verdict(ticket, _FORTY_HEX)
        assert CriticFinding.objects.filter(ticket=ticket, transition="merge", rubric_item="test_value").exists()

        _record_merge_verdict(ticket, items=_clean_verdict_items())  # re-judged clean at the same head
        check_merge_quality_verdict(ticket, _FORTY_HEX)
        assert not CriticFinding.objects.filter(ticket=ticket, transition="merge").exists()


class TestRatifiedTestStrategyAnchor(TestCase):
    def test_directive_anchor_is_the_sketch_acceptance_tests(self) -> None:
        ticket = _directive_ticket(with_sketch=True)
        anchor = ratified_test_strategy(ticket)
        assert "test_pr_budget_gate.py::TestBudget::test_refused" in anchor
        assert anchor in build_merge_quality_contract(ticket, _FORTY_HEX)

    def test_ordinary_anchor_is_the_plan_test_strategy_section(self) -> None:
        ticket = _ordinary_ticket()
        PlanArtifact.objects.create(
            ticket=ticket,
            plan_text="plan body",
            recorded_by="planner",
            base_sha=_FORTY_HEX,
            adequacy=dict(all_negated_adequacy("cover the budget-count branch and the 0-neutral branch")),
        )
        anchor = ratified_test_strategy(ticket)
        assert "budget-count branch" in anchor


class TestArming(TestCase):
    def test_no_verdict_arms_the_async_critic_at_the_head(self) -> None:
        ticket = _directive_ticket()
        with contextlib.suppress(MergeQualityVerdictError):
            check_merge_quality_verdict(ticket, _FORTY_HEX)
        dispatch = CriticDispatch.objects.filter(ticket=ticket, transition="merge", head_sha=_FORTY_HEX).first()
        assert dispatch is not None
        assert dispatch.task is not None
        assert dispatch.task.phase == "critic_reviewing"

    def test_a_clean_verdict_arms_nothing(self) -> None:
        ticket = _directive_ticket()
        _record_merge_verdict(ticket, items=_clean_verdict_items())
        check_merge_quality_verdict(ticket, _FORTY_HEX)
        assert not CriticDispatch.objects.filter(ticket=ticket, transition="merge").exists()


class TestTransitionScoping(TestCase):
    """The merge items are keyed to ``merge`` — a mark_delivered verdict never satisfies the merge gate."""

    def test_a_mark_delivered_verdict_does_not_cover_the_merge_gate(self) -> None:
        ticket = _directive_ticket()
        CriticVerdict.record_from_envelope(
            ticket=ticket,
            transition="mark_delivered",
            head_sha=_FORTY_HEX,
            envelope={"grader_identity": "critic-agent-7", "items": _clean_verdict_items()},
        )
        with pytest.raises(MergeQualityVerdictError, match="no recorded merge-quality CriticVerdict"):
            check_merge_quality_verdict(ticket, _FORTY_HEX)

    def test_a_merge_finding_is_recorded_under_the_merge_transition(self) -> None:
        ticket = _directive_ticket()
        _record_merge_verdict(ticket, items=[_fail("test_value", "vacuous"), _pass("cleanliness")])
        with pytest.raises(MergeQualityVerdictError):
            check_merge_quality_verdict(ticket, _FORTY_HEX)
        assert CriticFinding.objects.filter(ticket=ticket, transition="merge").exists()
        assert not CriticFinding.objects.filter(ticket=ticket, transition="mark_delivered").exists()


# --------------------------------------------------------------------------- #
# End-to-end: the gate fires at the REAL execute_bound_merge chokepoint.
# --------------------------------------------------------------------------- #
_SLUG = "souliane/teatree"
_PR = 4040

_GH_PROBES: tuple[tuple[str, str], ...] = (
    ("headRefOid", _FORTY_HEX),
    ("isDraft", "false"),
    ("statusCheckRollup", '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'),
    ("baseRefName", "main"),
    ("required_status_checks", '{"contexts": []}'),
    ("state,mergeCommit", '{"state": "OPEN", "mergeCommit": null}'),
)


def _gh_green(argv: list[str]) -> tuple[int, str, str]:
    """A forge whose live head == ``_FORTY_HEX``, green checks, not draft — every merge precondition passes."""
    joined = " ".join(argv)
    for needle, out in _GH_PROBES:
        if needle in joined:
            return (0, out, "")
    if "pulls" in joined and "merge" in joined:
        return (0, '{"sha": "merged0deadbeef"}', "")
    return (0, "", "")


def _keystone_fixtures(ticket: Ticket) -> MergeClear:
    """A cold-review verdict + PR row + green CLEAR bound to ``_FORTY_HEX`` for *ticket*."""
    ReviewVerdict.record(
        pr_id=_PR, slug=_SLUG, reviewed_sha=_FORTY_HEX, verdict="merge_safe", reviewer_identity="cold-reviewer"
    )
    PullRequest.objects.create(
        ticket=ticket, overlay="t3-teatree", repo=_SLUG, iid=str(_PR), url=f"https://github.com/{_SLUG}/pull/{_PR}"
    )
    return MergeClear.objects.create(
        ticket=ticket,
        pr_id=_PR,
        slug=_SLUG,
        reviewed_sha=_FORTY_HEX,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.DOCS,
    )


class TestKeystoneWiring(TestCase):
    """``execute_bound_merge`` refuses a directive keystone with no clean verdict; the ordinary twin merges.

    The ordinary twin — identical inputs, no merge verdict — MERGES, which is
    exactly the pre-PR-4 (no-gate) behavior: the RED-before control proving the
    gate is load-bearing, not a blanket refuse.
    """

    @pytest.fixture(autouse=True)
    def _skip_author_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.merge.execution.assert_merge_provenance_trusted", lambda **_: None)

    def _merge(self, clear: MergeClear) -> object:
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_green):
            return merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

    def test_directive_keystone_refused_without_a_clean_verdict(self) -> None:
        ticket = _directive_ticket()
        clear = _keystone_fixtures(ticket)
        with pytest.raises(MergePreconditionError, match="no recorded merge-quality CriticVerdict"):
            self._merge(clear)
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert clear.consumed_at is None
        assert not MergeAudit.objects.filter(clear=clear).exists()
        # satisfiable, not suppression: the async merge critic was armed at the head.
        assert CriticDispatch.objects.filter(ticket=ticket, transition="merge", head_sha=_FORTY_HEX).exists()

    def test_directive_keystone_merges_with_a_clean_verdict(self) -> None:
        ticket = _directive_ticket()
        clear = _keystone_fixtures(ticket)
        _record_merge_verdict(ticket, items=_clean_verdict_items())
        outcome = self._merge(clear)
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert outcome.merged_sha
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None

    def test_ordinary_keystone_merges_without_a_merge_verdict(self) -> None:
        # The RED-before control: identical setup, ordinary ticket + flag off → merges
        # (the pre-PR-4 no-gate behavior). Only the directive twin is refused.
        ticket = _ordinary_ticket()
        clear = _keystone_fixtures(ticket)
        outcome = self._merge(clear)
        ticket.refresh_from_db()
        assert outcome.merged_sha
        assert ticket.state == Ticket.State.MERGED
