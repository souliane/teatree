"""Tests for :class:`AutoReviewDispatch` — the auto-review-dispatch ledger + task factory (#68)."""

import datetime as dt
import json
from pathlib import Path
from typing import cast

import pytest
from django.utils import timezone

from teatree.agents.envelope_contract import envelope_example
from teatree.agents.phase_blocks import _REVIEW_VERDICT_RETURN_LINES
from teatree.agents.result_schema import RESULT_JSON_SCHEMA, JSONSchema, ReviewVerdictEnvelope
from teatree.core.modelkit.phase_tools import tools_for_phase
from teatree.core.modelkit.review_contract import ENVELOPE_FINDINGS_RULE
from teatree.core.models import AutoReviewDispatch, MRReviewLock, ReviewVerdict, Task, Ticket
from teatree.core.models.auto_review_dispatch import LOOP_SCANNER_HOLDER, MAX_DISPATCH_ATTEMPTS, build_review_contract

REVIEWING_PHASE = "reviewing"

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

SLUG = "souliane/teatree"
HEAD = "feedfacecafebabe1234567890abcdef12345678"
NEW_HEAD = "0123456789abcdef0123456789abcdef01234567"
URL = f"https://github.com/{SLUG}/pull/6230"


class TestEnqueueCreatesClaimableTask:
    def test_first_enqueue_creates_one_pending_reviewing_task(self) -> None:
        row = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")

        assert row is not None
        assert row.task is not None
        task = row.task
        assert task.phase == "reviewing"
        assert task.status == Task.Status.PENDING
        assert task.ticket.role == Ticket.Role.REVIEWER
        assert task.ticket.issue_url == URL

    def test_task_execution_reason_carries_the_return_envelope_contract(self) -> None:
        row = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")

        assert row is not None
        assert row.task is not None
        reason = row.task.execution_reason
        # corr-11: the headless reviewer RETURNS the verdict envelope; it must
        # NOT be told to run the shell-only `t3 <overlay> review record`.
        assert "review_verdict" in reason
        assert "Do NOT run `t3 <overlay> review record`" in reason
        assert HEAD in reason

    def test_blank_slug_or_head_does_not_enqueue(self) -> None:
        assert AutoReviewDispatch.enqueue(slug="", pr_id=1, head_sha=HEAD) is None
        assert AutoReviewDispatch.enqueue(slug=SLUG, pr_id=1, head_sha="") is None
        assert Task.objects.count() == 0

    def test_str_renders_slug_pr_and_short_head(self) -> None:
        row = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        assert row is not None
        assert str(row) == f"auto-review<{row.pk}:{SLUG}#6230@{HEAD[:8]}>"


class TestDedupPerHead:
    def test_second_enqueue_same_head_returns_none_and_creates_no_second_task(self) -> None:
        first = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        second = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")

        assert first is not None
        assert second is None
        assert AutoReviewDispatch.objects.count() == 1
        assert Task.objects.filter(phase="reviewing").count() == 1

    def test_new_head_while_prior_review_in_flight_does_not_rearm(self) -> None:
        # #1405: the MRReviewLock is keyed on the MR, not the head — a fresh
        # push while the prior review hasn't concluded must not arm a SECOND,
        # concurrent reviewer for the new head.
        first = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        rearmed = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=NEW_HEAD, pr_url=URL, overlay="teatree")

        assert first is not None
        assert rearmed is None
        assert AutoReviewDispatch.objects.count() == 1
        assert Task.objects.filter(phase="reviewing").count() == 1

    def test_new_head_rearms_once_the_prior_review_has_resolved(self) -> None:
        AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        MRReviewLock.resolve(slug=SLUG, pr_id=6230, holder=LOOP_SCANNER_HOLDER)

        rearmed = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=NEW_HEAD, pr_url=URL, overlay="teatree")

        assert rearmed is not None
        assert rearmed.task is not None
        assert AutoReviewDispatch.objects.count() == 2
        assert Task.objects.filter(phase="reviewing").count() == 2

    def test_re_enqueue_same_head_after_resolve_is_still_a_dedup_no_op(self) -> None:
        # A fresh lock acquire alone isn't enough to rearm — the AutoReviewDispatch
        # row's own (slug, pr_id, head_sha) uniqueness still dedups a re-enqueue on
        # the EXACT same head, even once the lock has resolved and become acquirable.
        AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        MRReviewLock.resolve(slug=SLUG, pr_id=6230, holder=LOOP_SCANNER_HOLDER)

        second = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")

        assert second is None
        assert AutoReviewDispatch.objects.count() == 1
        assert Task.objects.filter(phase="reviewing").count() == 1

    def test_distinct_prs_are_independent(self) -> None:
        AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        other = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6231, head_sha=HEAD, pr_url=URL, overlay="teatree")

        assert other is not None
        assert Task.objects.filter(phase="reviewing").count() == 2


class TestReviewContract:
    def test_contract_instructs_returning_the_verdict_envelope(self) -> None:
        contract = build_review_contract(slug=SLUG, pr_id=1, head_sha=HEAD, pr_url=URL)
        assert "review_verdict" in contract
        assert "merge_safe" in contract
        assert HEAD in contract

    def test_contract_forbids_the_shell_record_cli(self) -> None:
        contract = build_review_contract(slug=SLUG, pr_id=1, head_sha=HEAD, pr_url=URL)
        assert "Do NOT run `t3 <overlay> review record`" in contract


#: Phrasings that assert the reviewing phase cannot shell out. Each is a claim
#: :func:`tools_for_phase` is the authority on, so any of them appearing while
#: the table grants ``shell`` is the drift :class:`TestContractMatchesPhaseToolGrant` pins.
SHELL_DENIAL_PHRASINGS = (
    "no shell",
    "shell denied",
    "denied the shell",
    "shell is denied",
    "without a shell",
    "no bash",
    "cannot shell out",
)


def _reviewing_capability_claims() -> dict[str, str]:
    """Every prose surface that tells a reviewing-phase agent what it may call."""
    return {
        "stamped execution_reason contract": build_review_contract(slug=SLUG, pr_id=1, head_sha=HEAD, pr_url=URL),
        "build_review_contract docstring": build_review_contract.__doc__ or "",
        "ReviewVerdictEnvelope docstring": ReviewVerdictEnvelope.__doc__ or "",
        # The block the headless brief actually appends. Absent from this set it
        # kept telling reviewers "this phase has no shell" for the whole of #3775.
        "headless reviewing brief lines": "\n".join(_REVIEW_VERDICT_RETURN_LINES),
    }


class TestContractMatchesPhaseToolGrant:
    """The reviewer's brief may never deny a capability ``phase_tools`` grants.

    #2971 wrote "this phase has no shell" into the contract when it was true;
    #3549 granted every verdict-producing review phase the shell 17 days later
    and left the prose. A reviewer that believes its brief will not check out
    the reviewed head or run the tests, so the claim and the grant are pinned
    to each other here rather than to a fixed sentence.
    """

    def test_reviewing_phase_is_granted_the_shell(self) -> None:
        assert "shell" in tools_for_phase(REVIEWING_PHASE)

    def test_no_capability_claim_denies_the_granted_shell(self) -> None:
        if "shell" not in tools_for_phase(REVIEWING_PHASE):
            pytest.skip("phase_tools no longer grants the shell — the denial prose would be truthful")
        offenders = [
            (surface, phrasing)
            for surface, text in _reviewing_capability_claims().items()
            for phrasing in SHELL_DENIAL_PHRASINGS
            if phrasing in text.lower()
        ]
        assert not offenders, (
            f"tools_for_phase({REVIEWING_PHASE!r}) grants the shell, but these surfaces deny it: {offenders}"
        )

    def test_contract_directs_the_reviewer_to_the_sanctioned_checkout(self) -> None:
        contract = build_review_contract(slug=SLUG, pr_id=1, head_sha=HEAD, pr_url=URL)
        assert "t3 review checkout" in contract

    def test_contract_states_maker_checker_as_the_reason_for_the_record_ban(self) -> None:
        contract = build_review_contract(slug=SLUG, pr_id=1, head_sha=HEAD, pr_url=URL)
        assert "maker" in contract.lower()


#: Phrasings that tie ``findings`` exclusively to a blocking verdict. Read as an
#: instruction, each licenses an empty array on a pass — the disposal the review
#: skill reserves for the colleague-facing lane, where noise costs credibility,
#: and forbids in the envelope, where nothing is published and an omission is a
#: lost record.
FINDINGS_ONLY_WHEN_BLOCKING_PHRASINGS = (
    'with a "findings" array when blocking',
    "with a `findings` array when blocking",
    "with the blocking findings",
    "findings array when blocking",
    "findings only when blocking",
    "findings when you block",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
REVIEWER_AGENT_MD = REPO_ROOT / "agents" / "reviewer.md"
REVIEW_SKILL_MD = REPO_ROOT / "skills" / "review" / "SKILL.md"


def _normalized(text: str) -> str:
    """Collapse every whitespace run so a line-wrapped rendering still matches."""
    return " ".join(text.split())


def _schema_node(*path: str) -> JSONSchema:
    """Walk :data:`RESULT_JSON_SCHEMA` down *path*, keeping the untyped blob navigable."""
    node = RESULT_JSON_SCHEMA
    for key in path:
        node = cast("JSONSchema", node[key])
    return node


def _code_owned_reviewer_briefs() -> dict[str, str]:
    """The reviewer-facing briefs this repo renders, each of which must carry the rule."""
    return {
        "stamped execution_reason contract": build_review_contract(slug=SLUG, pr_id=1, head_sha=HEAD, pr_url=URL),
        "headless reviewing brief lines": "\n".join(_REVIEW_VERDICT_RETURN_LINES),
        "reviewer agent definition": REVIEWER_AGENT_MD.read_text(encoding="utf-8"),
    }


def _findings_prose_surfaces() -> dict[str, str]:
    """Every surface that tells a reviewer what belongs in ``findings`` — briefs and doctrine alike."""
    return {
        **_code_owned_reviewer_briefs(),
        "review skill": REVIEW_SKILL_MD.read_text(encoding="utf-8"),
        "ReviewVerdictEnvelope docstring": ReviewVerdictEnvelope.__doc__ or "",
        "review_verdict JSON schema": json.dumps(_schema_node("properties", "review_verdict")),
    }


class TestFindingsAreARecordNotABlockingArtifact:
    """Every reviewer-facing brief renders one shared findings rule, and none contradicts it.

    The brief reaches the reviewer before the skill does — it is stamped into
    ``execution_reason`` and read first — so a brief that says the opposite of
    the skill wins on ordering. Pinning the briefs to :data:`ENVELOPE_FINDINGS_RULE`
    binds them to one another and to the skill's envelope-lane doctrine, rather
    than to a sentence each surface is free to reword on its own.
    """

    def test_no_surface_ties_findings_exclusively_to_blocking(self) -> None:
        offenders = [
            (surface, phrasing)
            for surface, text in _findings_prose_surfaces().items()
            for phrasing in FINDINGS_ONLY_WHEN_BLOCKING_PHRASINGS
            if phrasing in text.lower()
        ]
        assert not offenders, (
            f"findings record what was observed, whatever the verdict, but these surfaces "
            f"tie them to a block: {offenders}"
        )

    def test_every_code_owned_brief_renders_the_shared_rule(self) -> None:
        rule = _normalized(ENVELOPE_FINDINGS_RULE)
        missing = [surface for surface, text in _code_owned_reviewer_briefs().items() if rule not in _normalized(text)]
        assert not missing, f"these reviewer briefs do not render ENVELOPE_FINDINGS_RULE: {missing}"

    def test_the_shared_rule_still_requires_findings_on_a_block(self) -> None:
        lowered = ENVELOPE_FINDINGS_RULE.lower()
        assert "hold" in lowered
        assert "block" in lowered

    def test_the_shared_rule_reaches_the_reviewer_through_the_stamped_task(self) -> None:
        row = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")

        assert row is not None
        assert row.task is not None
        assert _normalized(ENVELOPE_FINDINGS_RULE) in _normalized(row.task.execution_reason)

    def test_the_json_schema_teaches_the_rule_to_a_structured_output_model(self) -> None:
        findings = _schema_node("properties", "review_verdict", "properties", "findings")
        assert _normalized(ENVELOPE_FINDINGS_RULE) in _normalized(str(findings["description"]))

    def test_the_worked_envelope_example_does_not_model_an_empty_findings_pass(self) -> None:
        example = envelope_example(REVIEWING_PHASE)["review_verdict"]
        assert example["verdict"] == "merge_safe"
        assert example["findings"], "the example a reviewer copies must not model a pass with nothing recorded"


class TestDispatchedTaskReachesTerminalState:
    """The auto-created Ticket + reviewing Task reach DELIVERED on the happy path.

    The whole point of arming the dispatch is that the enqueued unit can run
    to completion: the reviewer claims the ``Task(phase=reviewing)``, records
    its verdict (stamping ``reviewed_sha`` on the reviewer-role ticket), and
    completing the task short-circuits the ticket to ``REVIEW_POSTED`` via
    ``mark_reviewed_externally``. An armed dispatch that never reached a
    terminal state would re-pump the same review forever.
    """

    def test_reviewer_completing_task_short_circuits_ticket_to_review_posted(self) -> None:
        row = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        assert row is not None
        assert row.task is not None
        task = row.task
        ticket = task.ticket
        assert ticket.role == Ticket.Role.REVIEWER
        assert ticket.state == Ticket.State.NOT_STARTED

        # The reviewer records the verdict bound to the reviewed head — the
        # ``review record`` CLI stamps ``reviewed_sha`` on the ticket, which
        # ``mark_reviewed_externally`` persists into ``last_review_state``.
        ticket.merge_extra(set_keys={"reviewed_sha": HEAD})

        task.complete()

        ticket.refresh_from_db()
        task.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_POSTED
        assert task.status == Task.Status.COMPLETED


class TestStrandedDispatchIsReArmable:
    """The #3920 invariant: a head with no verdict and no live reviewing task re-arms.

    ``AutoReviewDispatch`` was a ``get_or_create`` claim with no terminal state,
    no deadline and no reaper, so a reviewing task that died without recording a
    verdict left the row behind and ``enqueue`` returned ``None`` for that head
    forever — the PR became unmergeable until someone force-pushed. #3887, #3893
    and #3914 were all in exactly that state while the sweep declined to act.
    """

    @staticmethod
    def _arm() -> AutoReviewDispatch:
        row = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        assert row is not None
        return row

    @staticmethod
    def _expire(row: AutoReviewDispatch) -> None:
        """Push the claim's deadline into the past — the reviewer never came back."""
        AutoReviewDispatch.objects.filter(pk=row.pk).update(deadline=timezone.now() - dt.timedelta(minutes=1))
        MRReviewLock.objects.filter(slug=SLUG, pr_id=6230).update(deadline=timezone.now() - dt.timedelta(minutes=1))

    def test_a_live_claim_still_dedups(self) -> None:
        self._arm()
        assert AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL) is None
        assert Task.objects.filter(phase=REVIEWING_PHASE).count() == 1

    def test_an_expired_claim_whose_task_died_re_arms(self) -> None:
        first = self._arm()
        assert first.task is not None
        first.task.status = Task.Status.FAILED
        first.task.save(update_fields=["status"])
        self._expire(first)

        again = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")

        assert again is not None, "an expired dispatch whose task died must be re-armable"
        assert again.pk == first.pk, "the re-arm reuses the claim row rather than stacking a duplicate"
        assert again.task is not None
        assert again.task.pk != first.task.pk
        assert again.task.status == Task.Status.PENDING

    def test_re_arming_counts_the_attempt(self) -> None:
        first = self._arm()
        assert first.attempts == 1
        self._expire(first)
        again = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL)
        assert again is not None
        assert again.attempts == 2

    def test_a_permanently_failing_review_stops_re_arming(self) -> None:
        row = self._arm()
        for _ in range(MAX_DISPATCH_ATTEMPTS - 1):
            self._expire(row)
            row = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL)
            assert row is not None
        assert row.attempts == MAX_DISPATCH_ATTEMPTS

        self._expire(row)
        assert AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL) is None, (
            "the retry budget must bound a permanently-failing review rather than re-arm forever"
        )
        assert Task.objects.filter(phase=REVIEWING_PHASE).count() == MAX_DISPATCH_ATTEMPTS

    def test_a_recorded_verdict_is_terminal_and_never_re_arms(self) -> None:
        row = self._arm()
        ReviewVerdict.record(
            pr_id=6230,
            slug=SLUG,
            reviewed_sha=HEAD,
            verdict=ReviewVerdict.Verdict.HOLD,
            reviewer_identity="an-independent-reviewer",
        )
        self._expire(row)

        assert AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL) is None, (
            "a head that already has a verdict is spent — re-arming it would be review churn"
        )

    def test_an_expired_claim_with_budget_left_is_not_saturated(self) -> None:
        # The re-armable case: this claim died, but the next sweep will arm it
        # again, so it is not the doctor's business. Saturation is the END of the
        # retry, not any one dead attempt.
        row = self._arm()
        self._expire(row)

        assert row.attempts < MAX_DISPATCH_ATTEMPTS
        assert AutoReviewDispatch.saturated().count() == 0

    def test_saturated_claims_are_reported_for_the_doctor(self) -> None:
        row = self._arm()
        assert AutoReviewDispatch.saturated().count() == 0
        for _ in range(MAX_DISPATCH_ATTEMPTS - 1):
            self._expire(row)
            row = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL)
            assert row is not None
        self._expire(row)
        assert AutoReviewDispatch.saturated().count() == 1

    def test_the_last_attempt_is_not_saturated_while_its_deadline_is_still_live(self) -> None:
        # Saturation is "nothing will re-arm this head", not "the budget is spent":
        # the final reviewer is still running and may yet record a verdict, so
        # reporting it to the doctor now would be a false call for a human.
        row = self._arm()
        for _ in range(MAX_DISPATCH_ATTEMPTS - 1):
            self._expire(row)
            row = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL)
            assert row is not None

        assert row.attempts == MAX_DISPATCH_ATTEMPTS
        assert row.deadline > timezone.now()
        assert AutoReviewDispatch.saturated().count() == 0


class TestHeldLockLeavesNoOrphanClaim:
    def test_a_lock_held_by_another_reviewer_arms_nothing_at_all(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=6230, holder="someone-else", mr_url=URL)

        assert AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL) is None
        assert AutoReviewDispatch.objects.count() == 0, (
            "a refused enqueue must leave no claim row behind — a half-claimed head would "
            "dedup the next tick out of arming a review that was never dispatched"
        )
        assert Task.objects.filter(phase=REVIEWING_PHASE).count() == 0


class TestUnverdictedHeadIsAlwaysReArmable:
    """The #3920 invariant, stated once and asserted end to end.

    A PR with no ``merge_safe`` verdict at its live head and no review still in
    flight is always re-armable. #3887, #3893 and #3914 were each in exactly
    that state while the sweep declined to act, because the dispatch claim they
    had already spent was permanent.

    Liveness is read from the claim's DEADLINE and nowhere else — the same proxy
    ``MRReviewLock`` uses. A second liveness answer (the task's status) would
    deadlock on the zombie a crashed worker leaves in ``claimed``, which is the
    failure this fix exists to remove.
    """

    def test_the_head_re_arms_and_the_merge_lock_is_free_again(self) -> None:
        first = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        assert first is not None
        assert MRReviewLock.active_lock_for(slug=SLUG, pr_id=6230) is not None

        # The reviewer dies: no verdict is ever recorded, and both claims expire.
        past = timezone.now() - dt.timedelta(minutes=1)
        AutoReviewDispatch.objects.filter(pk=first.pk).update(deadline=past)
        MRReviewLock.objects.filter(slug=SLUG, pr_id=6230).update(deadline=past)

        assert ReviewVerdict.objects.filter(slug=SLUG, pr_id=6230, reviewed_sha=HEAD).count() == 0
        assert MRReviewLock.active_lock_for(slug=SLUG, pr_id=6230) is None, (
            "an expired lock must not read as a review in flight"
        )

        again = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")

        assert again is not None
        assert again.task is not None
        assert again.task.status == Task.Status.PENDING
        assert MRReviewLock.active_lock_for(slug=SLUG, pr_id=6230) is not None

    def test_the_superseded_task_stops_counting_as_armed(self) -> None:
        # The claim carries exactly one task FK, so re-arming moves it to the new
        # task and the dead one falls out of `not_auto_review_armed()`. That is
        # what lets the loop's orphan reaper clear the zombie: #3910 keeps it off
        # ARMED tasks, and after the re-arm this one is no longer armed.
        first = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        assert first is not None
        assert first.task is not None
        stale_task_pk = first.task.pk
        assert Task.objects.filter(pk=stale_task_pk).not_auto_review_armed().count() == 0

        past = timezone.now() - dt.timedelta(minutes=1)
        AutoReviewDispatch.objects.filter(pk=first.pk).update(deadline=past)
        MRReviewLock.objects.filter(slug=SLUG, pr_id=6230).update(deadline=past)
        assert AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL) is not None

        assert Task.objects.filter(pk=stale_task_pk).not_auto_review_armed().count() == 1
