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
    IncomingEvent,
    MergeAudit,
    MergeClear,
    PullRequest,
    RedCardSignal,
    RedMrFixAttempt,
    ReplyDispatch,
    ReviewVerdict,
    Rubric,
    RubricCriterion,
    Session,
    Task,
    TaskAttempt,
    Ticket,
    TicketTransition,
    Worktree,
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


class MergeAuditFactory(DjangoModelFactory[MergeAudit]):
    class Meta:
        model = MergeAudit

    clear = factory.SubFactory(MergeClearFactory)
    merged_sha = _FORTY_HEX
    required_checks_status = "green"


class RedMrFixAttemptFactory(DjangoModelFactory[RedMrFixAttempt]):
    class Meta:
        model = RedMrFixAttempt

    pr_url = factory.Sequence(lambda n: f"https://github.com/souliane/teatree/pull/{900 + n}")
    head_sha = factory.Sequence(lambda n: f"{n:040x}")
    overlay = "t3-teatree"


class TicketTransitionFactory(DjangoModelFactory[TicketTransition]):
    """Backdate ``created_at`` (``auto_now_add``) with ``update()`` after build."""

    class Meta:
        model = TicketTransition

    ticket = factory.SubFactory(TicketFactory)
    from_state = Ticket.State.STARTED
    to_state = Ticket.State.CODED


class RedCardSignalFactory(DjangoModelFactory[RedCardSignal]):
    class Meta:
        model = RedCardSignal

    overlay = "t3-teatree"
    channel = "C123"
    slack_ts = factory.Sequence(lambda n: f"1700000000.{n:06d}")
    signal_kind = RedCardSignal.Kind.RED_CARD_TEXT
    user_id = "U123"


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


class RubricFactory(DjangoModelFactory[Rubric]):
    class Meta:
        model = Rubric

    ticket = factory.SubFactory(TicketFactory)


class RubricCriterionFactory(DjangoModelFactory[RubricCriterion]):
    """A criterion row built directly via the ORM (bypassing the guarded factory).

    ``RubricCriterion.record_grade`` refuses an invalid grade; building directly lets
    a test construct one (maker grader / stale SHA / ungraded) and prove the done-gate
    refuses it independently of the record-time guard.
    """

    class Meta:
        model = RubricCriterion

    rubric = factory.SubFactory(RubricFactory)
    ordinal = factory.Sequence(int)
    text = factory.Sequence(lambda n: f"criterion {n}")
    status = RubricCriterion.Status.PENDING

    class Params:
        passed = factory.Trait(
            status=RubricCriterion.Status.PASS,
            grader_identity="cold-reviewer",
            reviewed_sha=_FORTY_HEX,
        )
        failed = factory.Trait(
            status=RubricCriterion.Status.FAIL,
            grader_identity="cold-reviewer",
            reviewed_sha=_FORTY_HEX,
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
        completed = factory.Trait(state=ImplementedIssueMarker.State.COMPLETED)
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


class WorktreeFactory(DjangoModelFactory[Worktree]):
    class Meta:
        model = Worktree

    ticket = factory.SubFactory(TicketFactory)
    overlay = "t3-teatree"
    repo_path = "souliane/teatree"
    branch = factory.Sequence(lambda n: f"feat/wt-{n}")
    state = Worktree.State.PROVISIONED


class SessionFactory(DjangoModelFactory[Session]):
    class Meta:
        model = Session

    ticket = factory.SubFactory(TicketFactory)
    overlay = "t3-teatree"
    agent_id = "coding"


class TaskFactory(DjangoModelFactory[Task]):
    class Meta:
        model = Task

    ticket = factory.SubFactory(TicketFactory)
    session = factory.SubFactory(SessionFactory)
    phase = "coding"
    status = Task.Status.PENDING
    # INTERACTIVE keeps ``status`` deterministic: the HEADLESS save-override
    # reroute only touches ``execution_target``, never the status the tests count.
    execution_target = Task.ExecutionTarget.INTERACTIVE


class TaskAttemptFactory(DjangoModelFactory[TaskAttempt]):
    """Backdate ``started_at`` (``auto_now_add``) with ``update()`` after build."""

    class Meta:
        model = TaskAttempt

    task = factory.SubFactory(TaskFactory)
    execution_target = Task.ExecutionTarget.HEADLESS
    exit_code = 0
    iteration = 1


class PullRequestFactory(DjangoModelFactory[PullRequest]):
    class Meta:
        model = PullRequest

    ticket = factory.SubFactory(TicketFactory)
    overlay = "t3-teatree"
    url = factory.Sequence(lambda n: f"https://github.com/souliane/teatree/pull/{1000 + n}")
    repo = "souliane/teatree"
    iid = factory.Sequence(lambda n: str(1000 + n))
    state = PullRequest.State.OPEN


class IncomingEventFactory(DjangoModelFactory[IncomingEvent]):
    class Meta:
        model = IncomingEvent

    source = IncomingEvent.Source.SLACK
    actor = "U123"
    body = factory.Sequence(lambda n: f"event body {n}")
    idempotency_key = factory.Sequence(lambda n: f"evt-{n}")


class ReplyDispatchFactory(DjangoModelFactory[ReplyDispatch]):
    class Meta:
        model = ReplyDispatch

    event = factory.SubFactory(IncomingEventFactory)
    target_ref = "C123"
    action_name = "reply"
    idempotency_key = factory.Sequence(lambda n: f"disp-{n}")
    status = ReplyDispatch.Status.SENT

    class Params:
        dead = factory.Trait(status=ReplyDispatch.Status.DEAD_LETTER)
