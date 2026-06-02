"""Factory Boy factories for teatree core models used in DB-backed tests.

These build rows directly via the ORM (``DjangoModelFactory`` ⇒
``objects.create``), deliberately bypassing guarded model factories like
``MergeClear.issue()`` so a test can construct an *invalid* row and prove the
merge-time gate refuses it independently of the issue-time gate.
"""

import factory
from factory.django import DjangoModelFactory

from teatree.core.models import (
    EvalRunRecord,
    EvalScenarioResult,
    EvalVerdict,
    ImplementedIssueMarker,
    MergeClear,
    ReviewVerdict,
    Ticket,
)

_FORTY_HEX = "c" * 40


class TicketFactory(DjangoModelFactory[Ticket]):
    class Meta:
        model = Ticket

    overlay = "t3-teatree"
    state = Ticket.State.IN_REVIEW


class MergeClearFactory(DjangoModelFactory[MergeClear]):
    class Meta:
        model = MergeClear

    ticket = factory.SubFactory(TicketFactory)
    pr_id = factory.Sequence(lambda n: 900 + n)
    slug = "souliane/teatree"
    reviewed_sha = _FORTY_HEX
    reviewer_identity = "cold-reviewer"
    gh_verify_result = MergeClear.VerifyResult.GREEN
    blast_class = MergeClear.BlastClass.LOGIC

    class Params:
        pending = factory.Trait(gh_verify_result=MergeClear.VerifyResult.PENDING)
        failed = factory.Trait(gh_verify_result=MergeClear.VerifyResult.FAILED)
        substrate = factory.Trait(blast_class=MergeClear.BlastClass.SUBSTRATE)
        docs = factory.Trait(blast_class=MergeClear.BlastClass.DOCS)


class ReviewVerdictFactory(DjangoModelFactory[ReviewVerdict]):
    class Meta:
        model = ReviewVerdict

    ticket = factory.SubFactory(TicketFactory)
    pr_id = factory.Sequence(lambda n: 900 + n)
    slug = "souliane/teatree"
    reviewed_sha = _FORTY_HEX
    verdict = ReviewVerdict.Verdict.MERGE_SAFE
    reviewer_identity = "cold-reviewer"
    blast_class = MergeClear.BlastClass.LOGIC
    gh_verify_result = MergeClear.VerifyResult.GREEN
    findings = factory.LazyFunction(list)

    class Params:
        hold = factory.Trait(
            verdict=ReviewVerdict.Verdict.HOLD,
            gh_verify_result=MergeClear.VerifyResult.FAILED,
        )


class ImplementedIssueMarkerFactory(DjangoModelFactory[ImplementedIssueMarker]):
    class Meta:
        model = ImplementedIssueMarker

    issue_url = factory.Sequence(lambda n: f"https://github.com/souliane/teatree/issues/{n}")
    overlay = "t3-teatree"
    state = ImplementedIssueMarker.State.DISPATCHED
    head_sha = _FORTY_HEX

    class Params:
        ticket_created = factory.Trait(state=ImplementedIssueMarker.State.TICKET_CREATED)
        abandoned = factory.Trait(state=ImplementedIssueMarker.State.ABANDONED)


class EvalRunRecordFactory(DjangoModelFactory[EvalRunRecord]):
    class Meta:
        model = EvalRunRecord

    model = "haiku"
    git_sha = _FORTY_HEX

    class Params:
        baseline = factory.Trait(is_baseline=True)


class EvalScenarioResultFactory(DjangoModelFactory[EvalScenarioResult]):
    class Meta:
        model = EvalScenarioResult

    run = factory.SubFactory(EvalRunRecordFactory)
    scenario_name = factory.Sequence(lambda n: f"scenario_{n}")
    model = "haiku"
    verdict = EvalVerdict.PASS
    score = 1.0
    trials = 1

    class Params:
        failing = factory.Trait(verdict=EvalVerdict.FAIL, score=0.0)
        was_skipped = factory.Trait(verdict=EvalVerdict.SKIP, score=0.0)
