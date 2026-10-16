"""The #2829 review-verdict merge gate — bind every merge to a recorded PASS at the head.

``execute_bound_merge`` is the single chokepoint BOTH autonomous merge paths
(the keystone CLEAR path and the solo-overlay bypass) converge on before the
forge squash PUT. ``assert_review_verdict_gate`` runs at its top and refuses any
merge that lacks a non-stale, INDEPENDENT ``merge_safe`` :class:`ReviewVerdict`
at the live head — every HOLD remains active until a newly reviewed head replaces it.

The keystone-path tests drive the REAL ``merge_ticket_pr`` (so the gate is the
production ``execute_bound_merge`` chokepoint, only the ``gh`` subprocess is
stubbed). The solo-path tests drive ``PrSweepScanner._evaluate_solo_overlay``
through ``scan()`` so the companion ``has_independent_cold_review`` predicate
(the defence-in-depth flag) is exercised on the same standing-hold rule.

Every "refuse" scenario merges on the pre-#2829 code (no gate) — they are the
RED-before-fix anti-vacuity proof: each asserts the PR is NOT merged AND the
ticket stays REVIEW_REQUESTED.
"""

import datetime as dt
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge import MergeOutcome, MergePreconditionError, merge_ticket_pr
from teatree.core.models import MergeAudit, MergeClear, Ticket
from teatree.core.models.review_verdict import HeadVerdictState, ReviewVerdict
from teatree.loop.scanners.pr_sweep import PrSummary, PrSweepScanner
from teatree.loop.scanners.pr_sweep_adapters import NullMergeNotifier
from teatree.loop.scanners.pr_sweep_decision import has_independent_cold_review
from tests._forge_stub import merge_path_stdout
from tests._pr_ledger import own_pr
from tests.factories import waive_rubric
from tests.teatree_core.conftest import record_merge_prerequisites_for_test
from tests.teatree_loop.test_pr_sweep_scanner import FakeKeystone, FakePrApiClient

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SLUG = "souliane/teatree"
_PR = 2829
_HEAD = "a" * 40
_OTHER = "b" * 40
_T0 = dt.datetime(2026, 6, 28, 12, 0, 0, tzinfo=dt.UTC)


def _record(verdict: str, *, sha: str = _HEAD, reviewer: str = "cold-reviewer", at: dt.datetime | None = None) -> None:
    """Record one verdict, optionally pinning ``recorded_at`` for ordering cases."""
    row = ReviewVerdict.record(
        pr_id=_PR,
        slug=_SLUG,
        reviewed_sha=sha,
        verdict=verdict,
        reviewer_identity=reviewer,
    )
    if at is not None:
        ReviewVerdict.objects.filter(pk=row.pk).update(recorded_at=at)


def _clear(*, ticket: Ticket | None = None) -> MergeClear:
    # The rubric done-gate runs at this chokepoint; the real path has an independent
    # verifier grade the rubric, so the audited bypass stands in (cf. _seed_sibling_verdict).
    if ticket is not None:
        waive_rubric(ticket)
        record_merge_prerequisites_for_test(ticket, _HEAD)
    """A green, cold-reviewer CLEAR bound to ``_HEAD`` — WITHOUT seeding a sibling verdict.

    The verdict each scenario controls itself, so this never auto-seeds (unlike
    the ``_clear`` helper in ``test_merge_execution``).
    """
    return MergeClear.objects.create(
        ticket=ticket,
        pr_id=_PR,
        slug=_SLUG,
        reviewed_sha=_HEAD,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.DOCS,
    )


#: The §17.4.3 read-only probe responses, keyed by an argv substring. A live head
#: == ``_HEAD``, green checks, not draft, empty required-context gate — so the
#: preconditions pass and the merge reaches ``execute_bound_merge`` (the gate).
_GH_PROBES: tuple[tuple[str, str], ...] = (
    ("headRefOid", _HEAD),
    ("isDraft", "false"),
    ("statusCheckRollup", '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'),
    ("baseRefName", "main"),
    ("required_status_checks", '{"contexts": []}'),
    ("state,mergeCommit", '{"state": "OPEN", "mergeCommit": null}'),
)


def _gh_green(argv: list[str]) -> tuple[int, str, str]:
    """A forge whose live head == ``_HEAD``, green checks, not draft — preconditions pass."""
    joined = " ".join(argv)
    for needle, out in _GH_PROBES:
        if needle in joined:
            return (0, out, "")
    if "pulls" in joined and "merge" in joined:
        return (0, '{"sha": "merged0deadbeef"}', "")
    return (0, merge_path_stdout(joined), "")


@pytest.fixture(autouse=True)
def _skip_author_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # The #1773 public-repo author gate is orthogonal; neutralise it so these
    # tests isolate the #2829 review-verdict gate.
    monkeypatch.setattr("teatree.core.merge.execution.assert_merge_provenance_trusted", lambda **_: None)


def _merge(clear: MergeClear) -> MergeOutcome:
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_green):
        return merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")


class TestKeystoneMergeVerdictGate(TestCase):
    """The real ``execute_bound_merge`` chokepoint, driven through ``merge_ticket_pr``."""

    def test_1_no_verdict_at_head_refuses(self) -> None:
        # RED before #2829: with no recorded verdict the keystone merged today.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        clear = _clear(ticket=ticket)
        with pytest.raises(MergePreconditionError, match="no recorded merge_safe ReviewVerdict at the live head"):
            _merge(clear)
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED
        assert clear.consumed_at is None
        assert not MergeAudit.objects.filter(clear=clear).exists()

    def test_2_hold_only_at_head_refuses(self) -> None:
        # RED before #2829: a recorded HOLD did not stop the merge path.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        clear = _clear(ticket=ticket)
        _record("hold", at=_T0)
        with pytest.raises(MergePreconditionError, match="standing HOLD verdict"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED
        assert not MergeAudit.objects.filter(clear=clear).exists()

    def test_3_hold_then_later_merge_safe_refuses(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        clear = _clear(ticket=ticket)
        _record("hold", reviewer="reviewer-a", at=_T0)
        hold = ReviewVerdict.objects.get(reviewer_identity="reviewer-a")
        _record("merge_safe", reviewer="reviewer-b", at=_T0 + dt.timedelta(seconds=1))
        assert ReviewVerdict.objects.for_pr(_SLUG, _PR).count() == 2
        with pytest.raises(MergePreconditionError, match=rf"HOLD.*#{hold.pk}.*push a fix"):
            _merge(clear)
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED
        assert clear.consumed_at is None

    def test_hold_at_older_head_and_pass_at_live_head_merges(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        clear = _clear(ticket=ticket)
        _record("hold", sha=_OTHER, reviewer="reviewer-a")
        _record("merge_safe", sha=_HEAD, reviewer="reviewer-b")
        outcome = _merge(clear)
        assert outcome.merged_sha
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_4_merge_safe_then_later_hold_refuses(self) -> None:
        # A HOLD blocks this head regardless of when the pass was recorded.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        clear = _clear(ticket=ticket)
        _record("merge_safe", reviewer="reviewer-a", at=_T0)
        _record("hold", reviewer="reviewer-b", at=_T0 + dt.timedelta(seconds=1))
        with pytest.raises(MergePreconditionError, match="an independent reviewer recorded a HOLD at this head"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED
        assert not MergeAudit.objects.filter(clear=clear).exists()

    def test_4b_same_timestamp_hold_and_merge_safe_refuses(self) -> None:
        # Low finding: a HOLD recorded in the SAME instant as a PASS resolves to
        # HOLD (the safe direction), never silently overridden to merge-safe.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        clear = _clear(ticket=ticket)
        _record("merge_safe", reviewer="reviewer-a", at=_T0)
        _record("hold", reviewer="reviewer-b", at=_T0)
        with pytest.raises(MergePreconditionError, match="an independent reviewer recorded a HOLD at this head"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED
        assert not MergeAudit.objects.filter(clear=clear).exists()

    def test_5_stale_merge_safe_refuses(self) -> None:
        # SHA binding: a merge_safe reviewed against a different tree is stale at the
        # live head, so it cannot vouch for it. RED before #2829.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        clear = _clear(ticket=ticket)
        _record("merge_safe", sha=_OTHER, at=_T0)
        with pytest.raises(MergePreconditionError, match="no recorded merge_safe ReviewVerdict at the live head"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED

    def test_6_happy_path_non_stale_merge_safe_merges(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        clear = _clear(ticket=ticket)
        _record("merge_safe", at=_T0)
        outcome = _merge(clear)
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert outcome.merged_sha
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None
        assert MergeAudit.objects.filter(clear=clear).exists()

    def test_self_attested_verdict_can_never_satisfy_the_gate(self) -> None:
        # Anti-vacuity: ``ReviewVerdict.record`` refuses a maker/loop reviewer, so a
        # maker can never seed a row that satisfies the gate — the merge stays refused.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        clear = _clear(ticket=ticket)
        from teatree.core.models.review_verdict import ReviewVerdictError  # noqa: PLC0415

        with pytest.raises(ReviewVerdictError, match="maker/coding-agent/loop"):
            _record("merge_safe", reviewer="merge-loop", at=_T0)
        with pytest.raises(MergePreconditionError, match="no recorded merge_safe ReviewVerdict at the live head"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED


def _solo_scanner(prs: list[PrSummary]) -> tuple[PrSweepScanner, FakePrApiClient, FakeKeystone]:
    api = FakePrApiClient(prs_by_slug={_SLUG: prs})
    keystone = FakeKeystone()
    scanner = PrSweepScanner(
        repos=(_SLUG,),
        api=api,
        keystone=keystone,
        notifier=NullMergeNotifier(),
        overlay="teatree",
        solo_overlay=True,
        self_identities=("souliane",),
    )
    return scanner, api, keystone


def _solo_pr() -> PrSummary:
    own_pr(_SLUG, _PR)
    return PrSummary(
        slug=_SLUG,
        number=_PR,
        head_sha=_HEAD,
        is_draft=False,
        has_changes_requested=False,
        rollup=(
            {
                "__typename": "CheckRun",
                "name": "test (3.13)",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-06-19T10:00:00Z",
                "completedAt": "2026-06-19T10:05:00Z",
            },
        ),
        url=f"https://github.com/{_SLUG}/pull/{_PR}",
        title=f"PR {_PR}",
        author="souliane",
    )


class TestSoloOverlayPathHonoursStandingHolds(TestCase):
    """The #2829 gate and #4380 sweep both refuse a head with any standing HOLD."""

    @pytest.fixture(autouse=True)
    def _internal_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.review.author_trust.repo_is_internal", lambda *a, **k: True)
        monkeypatch.setattr(
            "teatree.core.merge.ci_rollup.CodeHostQuery.pr_changed_paths",
            lambda *a, **k: ["src/teatree/loop/scanners/pr_sweep.py"],
        )
        # #12: the sweep's CI gate reads the branch-protection required set; the
        # ``_solo_pr`` rollup carries a green ``test (3.13)`` so scope to just it.
        monkeypatch.setattr(
            "teatree.core.merge.ci_rollup.CodeHostQuery.required_context_names",
            lambda *a, **k: {"test (3.13)"},
        )

    def test_no_verdict_flags_not_merges(self) -> None:
        scanner, api, keystone = _solo_scanner([_solo_pr()])
        signals = scanner.scan()
        assert api.merge_pr_calls == []
        assert keystone.calls == []
        assert signals[0].kind == "pr_sweep.flag_no_review"

    def test_hold_wins_flags_not_merges(self) -> None:
        _record("merge_safe", reviewer="reviewer-a", at=_T0)
        _record("hold", reviewer="reviewer-b", at=_T0 + dt.timedelta(seconds=1))
        assert ReviewVerdict.objects.for_pr(_SLUG, _PR).count() == 2
        scanner, api, _keystone = _solo_scanner([_solo_pr()])
        signals = scanner.scan()
        assert api.merge_pr_calls == []
        # #4380: a held head is flagged as HELD, not as "no independent review" —
        # a reviewer looked and said no. Two reviewers really do disagree here, and
        # the HOLD landing second must not downgrade that to a lone hold.
        assert signals[0].kind == "pr_sweep.flag_held"
        assert signals[0].payload["reason"] == "contested_hold_at_head"

    def test_the_holding_reviewer_pass_does_not_lift_their_hold(self) -> None:
        _record("hold", at=_T0)
        _record("merge_safe", at=_T0 + dt.timedelta(seconds=1))
        assert ReviewVerdict.objects.for_pr(_SLUG, _PR).count() == 2
        scanner, api, _keystone = _solo_scanner([_solo_pr()])
        signals = scanner.scan()
        assert api.merge_pr_calls == []
        assert signals[0].kind == "pr_sweep.flag_held"


class TestHasIndependentColdReviewStandingHolds(TestCase):
    """Scenario 7: the companion predicate returns False when any verdict is a HOLD."""

    def test_hold_beside_pass_returns_false(self) -> None:
        _record("merge_safe", reviewer="reviewer-a", at=_T0)
        _record("hold", reviewer="reviewer-b", at=_T0 + dt.timedelta(seconds=1))
        assert ReviewVerdict.objects.for_pr(_SLUG, _PR).count() == 2
        assert has_independent_cold_review(slug=_SLUG, pr_id=_PR, head_sha=_HEAD) is False

    def test_later_merge_safe_does_not_override_hold(self) -> None:
        _record("hold", reviewer="reviewer-a", at=_T0)
        _record("merge_safe", reviewer="reviewer-b", at=_T0 + dt.timedelta(seconds=1))
        assert ReviewVerdict.objects.for_pr(_SLUG, _PR).count() == 2
        assert has_independent_cold_review(slug=_SLUG, pr_id=_PR, head_sha=_HEAD) is False

    def test_no_merge_safe_returns_false(self) -> None:
        _record("hold", at=_T0)
        assert has_independent_cold_review(slug=_SLUG, pr_id=_PR, head_sha=_HEAD) is False

    def test_stale_merge_safe_returns_false(self) -> None:
        _record("merge_safe", sha=_OTHER, at=_T0)
        assert has_independent_cold_review(slug=_SLUG, pr_id=_PR, head_sha=_HEAD) is False


class TestEffectiveStateAtHeldHead(TestCase):
    """A same-timestamp PASS and HOLD still resolve to HOLD."""

    def test_same_timestamp_pass_and_hold_holds(self) -> None:
        # Low finding: an independent reviewer's HOLD recorded in the same instant
        # as another's PASS must NOT be silently overridden — the tie resolves to
        # HOLD. Distinct reviewer identities make the two judgments explicit.
        _record("hold", reviewer="reviewer-a", at=_T0)
        _record("merge_safe", reviewer="reviewer-b", at=_T0)
        state = ReviewVerdict.objects.effective_state_at(slug=_SLUG, pr_id=_PR, head_sha=_HEAD)
        assert state is HeadVerdictState.HOLD

    def test_head_sha_is_normalised(self) -> None:
        _record("merge_safe", at=_T0)
        state = ReviewVerdict.objects.effective_state_at(slug=_SLUG, pr_id=_PR, head_sha=f"  {_HEAD.upper()}  ")
        assert state is HeadVerdictState.MERGE_SAFE
