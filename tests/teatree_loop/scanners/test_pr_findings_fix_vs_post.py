"""The fix-vs-post rule for a review finding, keyed on the merge request's AUTHOR.

The defect these pin: the factory reviewed its own merge requests and answered
every finding by REPORTING it — an inline note, a statusline row, an aged-skip DM —
including on merge requests it authored itself. On its own work a comment merely
registers a defect against itself, so a real, fixable finding sat until a human
read the DM (observed on a green, conflict-free own MR carrying one unresolved
inline note naming a concrete defect).

The rule is the author and nothing else:

* our own identity, or a bot we declared as ourselves -> FIX (dispatch the fix
    agent, which implements on the MR's own branch and pushes);
* anyone else, or an author we could not resolve      -> POST (surface only; never
    write to somebody else's branch).

Each of those four cases is asserted here, at the two seams that together decide
it: the scanner (which signal kind it emits) and the routing table (where that kind
goes). The asymmetry — a positive identity match is required to reach FIX, never
merely the absence of a negative — is the safety property, so the unresolved-author
case is a first-class test rather than an edge note.
"""

import pytest

from teatree.core.models.red_mr_fix_attempt import RedMrFixAttempt
from teatree.core.models.review_verdict import HeadVerdictState
from teatree.loop.dispatch_tables import AGENT_BY_KIND, DUAL_DISPATCH, STATUSLINE_ZONE_BY_KIND
from teatree.loop.persistence import _FIX_REASON_BY_KIND
from teatree.loop.scanners.my_prs import MyPrsScanner
from teatree.loop.scanners.pr_findings import FindingDisposition, disposition_for_author, unaddressed_review_findings
from tests.teatree_loop.test_scanners import FakeCodeHost

# Neutral stand-ins on purpose: the rule is driven by the CONFIGURED identity set,
# never by a literal handle, so the deployment's real operator and bot names have no
# business in core. ``test_the_identity_set_is_configuration_not_a_literal`` is what
# pins that property.
OWNER = "the-operator"
BOT = "the-operators-bot"
COLLEAGUE = "some-colleague"
IDENTITIES = (OWNER, BOT)

FIX_KIND = "my_pr.findings_to_fix"
POST_KIND = "my_pr.draft_notes"


def _mr(*, author: str | None, notes: int = 1, iid: int = 130) -> dict[str, object]:
    """A green, conflict-free own-repo MR carrying *notes* unresolved notes."""
    payload: dict[str, object] = {
        "iid": iid,
        "title": "fix(loop): resolve the self-update CI verdict",
        "web_url": f"https://gitlab.com/group/repo/-/merge_requests/{iid}",
        "sha": f"{iid:040d}",
        "head_pipeline": {"status": "success"},
        "user_notes_count": notes,
    }
    if author is not None:
        payload["author"] = {"username": author}
    return payload


def _kinds(mr: dict[str, object], *, identities: tuple[str, ...] = IDENTITIES) -> list[str]:
    host = FakeCodeHost(user=identities[0] if identities else "", my_prs=[mr])
    return [signal.kind for signal in MyPrsScanner(host=host, identities=identities).scan()]


class TestTheAuthorDecidesFixOrPost:
    """All four cases of the owner's rule, at the scanner that emits the decision."""

    def test_owner_authored_findings_are_dispatched_for_a_fix(self) -> None:
        assert _kinds(_mr(author=OWNER)) == [FIX_KIND]

    def test_bot_authored_findings_are_dispatched_for_a_fix(self) -> None:
        assert _kinds(_mr(author=BOT)) == [FIX_KIND]

    def test_a_colleagues_findings_are_only_surfaced(self) -> None:
        assert _kinds(_mr(author=COLLEAGUE)) == [POST_KIND]

    def test_an_unresolvable_author_is_only_surfaced(self) -> None:
        # Fails SAFE: a branch is never written to on an identity we cannot resolve.
        assert _kinds(_mr(author=None)) == [POST_KIND]

    def test_an_unreadable_identity_set_is_only_surfaced(self) -> None:
        # The identity set is the other half of the resolution; losing it must not
        # promote every author to "ours". Asserted on the predicate because the
        # scanner cannot reach it — with no identity at all it lists no PRs, so its
        # own answer to this case is "emit nothing", which is safe for a different
        # reason and would not prove this one.
        assert disposition_for_author(OWNER, self_identities=()) is FindingDisposition.POST

    @pytest.mark.parametrize(
        ("author", "expected"),
        [
            (OWNER, FindingDisposition.FIX),
            (BOT, FindingDisposition.FIX),
            (COLLEAGUE, FindingDisposition.POST),
            ("", FindingDisposition.POST),
        ],
    )
    def test_the_predicate_itself(self, author: str, expected: FindingDisposition) -> None:
        assert disposition_for_author(author, self_identities=IDENTITIES) is expected

    def test_the_identity_set_is_configuration_not_a_literal(self) -> None:
        # The bot handle has changed before: a differently-configured deployment
        # must reach FIX for ITS bot and POST for ours.
        assert disposition_for_author("other-bot", self_identities=("someone", "other-bot")) is FindingDisposition.FIX
        assert disposition_for_author(BOT, self_identities=("someone", "other-bot")) is FindingDisposition.POST


class TestAGreenHeadStillOwesItsFindings:
    """A green pipeline was what suppressed the signal entirely — the observed MR's shape."""

    def test_a_green_own_mr_with_an_unresolved_note_still_fires(self) -> None:
        assert _kinds(_mr(author=BOT)) == [FIX_KIND]

    def test_a_clean_own_mr_with_no_notes_stays_an_ordinary_open_pr(self) -> None:
        assert _kinds(_mr(author=BOT, notes=0)) == ["my_pr.open"]

    def test_a_recorded_hold_fires_even_with_no_note_on_the_forge(self) -> None:
        # The headless reviewer is Bash-denied and RETURNS its findings, so a HOLD
        # frequently posts nothing: the note count alone would miss it entirely.
        assert unaddressed_review_findings(verdict_state=HeadVerdictState.HOLD, notes_count=0)

    def test_a_vouched_head_is_not_re_dispatched_over_its_notes(self) -> None:
        assert not unaddressed_review_findings(verdict_state=HeadVerdictState.MERGE_SAFE, notes_count=3)

    def test_an_unjudged_head_falls_back_to_the_forge_note_count(self) -> None:
        assert unaddressed_review_findings(verdict_state=None, notes_count=1)
        assert not unaddressed_review_findings(verdict_state=HeadVerdictState.NO_MERGE_SAFE, notes_count=0)


class TestTheRoutingTableCarriesTheSameRule:
    """The kind the scanner picked must actually reach (or not reach) an agent."""

    def test_the_fix_kind_reaches_the_fix_agent(self) -> None:
        assert AGENT_BY_KIND[FIX_KIND] == "t3:debug"

    def test_the_post_kind_reaches_no_agent(self) -> None:
        assert POST_KIND not in AGENT_BY_KIND

    def test_both_stay_visible_to_the_operator(self) -> None:
        assert STATUSLINE_ZONE_BY_KIND[FIX_KIND] == "action_needed"
        assert STATUSLINE_ZONE_BY_KIND[POST_KIND] == "action_needed"
        assert FIX_KIND in DUAL_DISPATCH


class TestTheFixDispatchGetsItsOwnLedgerSlotAndRemedy:
    """Sharing the CI-red slot would let one fix dedup away the other at the same head."""

    def test_the_ledger_has_a_review_findings_slot(self) -> None:
        assert RedMrFixAttempt.Kind.REVIEW_FINDINGS.value == "review_findings"

    def test_the_scanner_stamps_that_slot_on_the_fix_payload(self) -> None:
        host = FakeCodeHost(user=OWNER, my_prs=[_mr(author=BOT)])
        signal = MyPrsScanner(host=host, identities=IDENTITIES).scan()[0]
        assert signal.payload["fix_kind"] == RedMrFixAttempt.Kind.REVIEW_FINDINGS

    def test_the_remedy_says_implement_and_push_never_comment(self) -> None:
        remedy = _FIX_REASON_BY_KIND[RedMrFixAttempt.Kind.REVIEW_FINDINGS]
        assert "IMPLEMENT" in remedy
        assert "push" in remedy
        assert "another comment" in remedy
