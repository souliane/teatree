"""Per-ticket rubric of checkable acceptance criteria graded by an independent verifier (#2241).

Encodes the standing rule "declare done only on a verified, full-spec outcome" as
a durable, mechanical record instead of vibes. Each ticket carries a :class:`Rubric`
of N :class:`RubricCriterion` rows; an independent verifier (the grader, ``!= maker``)
records a per-criterion PASS/FAIL bound to the reviewed tree's SHA. The rubric is
"satisfied" only when EVERY criterion is PASS by a non-maker grader at the current
head — :meth:`Rubric.is_fully_passed_at`. It is the highest-value lever from the
Fable-5 loop-design thread: a verifier sub-agent grading a checklist beats
self-critique.

The record follows the durable, compaction-surviving pattern of
:class:`teatree.core.models.review_verdict.ReviewVerdict` / ``MergeClear``: the DB
row is the truth, and the guarded :meth:`RubricCriterion.record_grade` factory shares
``MergeClear``'s validation primitives (``is_commit_sha``, ``is_non_reviewer_role``) so
the rubric grade contract and the CLEAR/verdict contract cannot drift apart.

Population (``ticket rubric-set``) accepts EXPLICIT criteria only — auto-derivation
from ``/plan`` is the [#2240](https://github.com/souliane/teatree/issues/2240) follow-up.
The LLM grader prior art lives in :mod:`teatree.eval.judge` (``JudgeSpec.rubric`` +
``ClaudeJudge.grade``, the in-process Agent SDK); it is kept SEPARATE here on
purpose — extracting a shared grader would couple the metered-LLM path to this
DB-record path.
"""

from typing import TYPE_CHECKING, ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.merge_clear import SHA_FULL_LEN, is_commit_sha, is_non_reviewer_role
from teatree.core.models.ticket import Ticket

if TYPE_CHECKING:
    from django.db.models import QuerySet


class RubricError(ValueError):
    """A rubric population or grade was rejected at record time — the contract failed."""


class RubricManager(models.Manager["Rubric"]):
    """Read surface for the per-ticket rubric lookup (the done-gate + the CLI)."""

    def active_for_ticket(self, ticket: Ticket) -> "Rubric | None":
        """The ticket's active (most-recently-created) rubric, or ``None``.

        ``populate`` is a get-or-create so a ticket has at most one rubric; ordering
        by ``-created_at`` and taking the first is the active row.
        """
        return self.filter(ticket=ticket).order_by("-created_at").first()


class Rubric(models.Model):
    """The per-ticket acceptance-criteria checklist an independent verifier grades.

    One active rubric per ticket: :meth:`populate` is a get-or-create that replaces
    the criteria atomically, so re-running ``rubric-set`` re-states the checklist
    rather than stacking duplicates. The done-gate (:func:`teatree.core.gates.
    rubric_gate.check_rubric_satisfied`) reads :meth:`is_fully_passed_at` against the
    PR's live head SHA; an empty / ungraded / failed / stale-SHA / maker-graded rubric
    is NOT fully passed — the gate fails CLOSED.
    """

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="rubrics")
    created_at = models.DateTimeField(default=timezone.now)

    objects: ClassVar[RubricManager] = RubricManager()

    class Meta:
        db_table = "teatree_rubric"
        ordering: ClassVar = ["-created_at"]
        indexes: ClassVar = [models.Index(fields=["ticket", "-created_at"])]

    def __str__(self) -> str:
        return f"rubric<ticket={self.ticket_id} criteria={self.criteria.count()}>"  # ty: ignore[unresolved-attribute]

    @property
    def criteria(self) -> "QuerySet[RubricCriterion]":
        """The rubric's criteria, ordered by ``ordinal`` (the explicit reverse accessor).

        An explicit ``objects.filter`` property (the ``eval_run.EvalRunRecord.results``
        pattern) so the type checker resolves the reverse relation, rather than relying
        on the implicit Django related-manager.
        """
        return RubricCriterion.objects.filter(rubric=self).order_by("ordinal")

    @classmethod
    def populate(cls, ticket: Ticket, criteria: list[str]) -> "Rubric":
        """Get-or-create the ticket's rubric and replace its criteria, all PENDING.

        ``criteria`` is the explicit list of acceptance-criterion texts (the
        ``rubric-set`` seam — no ``/plan`` derivation). An empty list is refused: a
        rubric with no criteria would pass the gate vacuously (``is_fully_passed_at``
        already fails closed on it, but refusing here surfaces the mistake at
        population time). Replacing the criteria resets every grade to PENDING, so a
        re-stated rubric must be re-graded — a stale PASS can never carry over to a
        changed checklist.
        """
        cleaned = [text.strip() for text in criteria if text.strip()]
        if not cleaned:
            msg = "a rubric needs at least one non-empty criterion — an empty rubric passes the gate vacuously"
            raise RubricError(msg)
        with transaction.atomic():
            rubric, _ = cls.objects.get_or_create(ticket=ticket)
            rubric.criteria.delete()
            RubricCriterion.objects.bulk_create(
                [RubricCriterion(rubric=rubric, ordinal=ordinal, text=text) for ordinal, text in enumerate(cleaned)]
            )
        return rubric

    def is_fully_passed_at(self, head_sha: str) -> bool:
        """True iff EVERY criterion is PASS by an independent grader at ``head_sha``.

        Fail-closed by construction: an empty rubric (no criteria) is False; any
        criterion still PENDING or FAIL is False; a criterion graded against a
        different (stale) ``reviewed_sha`` is False; a criterion with an empty or
        maker/coding-agent/loop ``grader_identity`` is False. The SHA compare is
        case-insensitive on the stripped value so a mixed-case forge ``headRefOid``
        cannot silently miss. Only an all-PASS, non-maker-graded, head-bound rubric
        satisfies the done-gate.
        """
        target = head_sha.strip().lower()
        if not target:
            return False
        criteria = list(self.criteria.all())
        if not criteria:
            return False
        return all(criterion.is_passing_at(target) for criterion in criteria)

    def block_reason(self, head_sha: str) -> str:
        """The precise why-blocked clause for the done-gate remediation message.

        Mirrors :func:`teatree.core.gates.anti_vacuity_gate._block_reason`: it names
        the FIRST failing condition (no criteria, an ungraded/failed criterion, a
        maker grader, or a stale SHA) so the remediation points at exactly what to fix.
        """
        target = head_sha.strip().lower()
        criteria = list(self.criteria.all())
        if not criteria:
            return "the rubric has no criteria recorded"
        pending = [c for c in criteria if c.status == RubricCriterion.Status.PENDING]
        if pending:
            return (
                f"{len(pending)} of {len(criteria)} criteria are ungraded (fail-closed) — "
                f"every criterion must be graded"
            )
        failed = [c for c in criteria if c.status == RubricCriterion.Status.FAIL]
        if failed:
            return f"{len(failed)} of {len(criteria)} criteria are graded FAIL — every criterion must PASS"
        maker_graded = [c for c in criteria if is_non_reviewer_role(c.grader_identity) or not c.grader_identity.strip()]
        if maker_graded:
            return (
                f"{len(maker_graded)} of {len(criteria)} criteria were graded by a maker/coding-agent/loop role or "
                f"by no one — a rubric is graded by an INDEPENDENT verifier, never the maker"
            )
        stale = [c for c in criteria if c.reviewed_sha != target]
        if stale:
            recorded = stale[0].reviewed_sha
            return (
                f"{len(stale)} of {len(criteria)} criteria were graded against head "
                f"{recorded[:8] or recorded!r}, not the current head {target[:8] or target!r} — the grade is stale "
                f"(force-push / new commits); re-grade at the current SHA"
            )
        return ""


class RubricCriterion(models.Model):
    """One checkable acceptance criterion plus its independent-verifier grade.

    The grade is recorded ONLY through the guarded :meth:`record_grade` factory,
    which enforces (mirroring ``ReviewVerdict.record``): a full 40-char hex
    ``reviewed_sha`` (so the head-bind compare cannot silently fail), a non-empty
    ``grader_identity`` that is NOT a maker/coding-agent/loop role (the maker can
    never self-attest a criterion — ``is_non_reviewer_role``), and a terminal
    ``pass``/``fail`` status. An ungraded criterion stays PENDING and fails the
    done-gate closed.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PASS = "pass", "Pass"
        FAIL = "fail", "Fail"

    rubric = models.ForeignKey(Rubric, on_delete=models.CASCADE, related_name="criteria_set")
    ordinal = models.IntegerField()
    text = models.TextField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    grader_identity = models.CharField(max_length=255, default="")
    reviewed_sha = models.CharField(max_length=64, default="")
    rationale = models.TextField(default="", blank=True)
    graded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_rubric_criterion"
        ordering: ClassVar = ["rubric", "ordinal"]
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["rubric", "ordinal"], name="uniq_rubric_criterion_ordinal"),
        ]

    def __str__(self) -> str:
        return f"criterion<rubric={self.rubric_id} #{self.ordinal} {self.status}>"  # ty: ignore[unresolved-attribute]

    def record_grade(self, *, status: str, grader_identity: str, reviewed_sha: str, rationale: str = "") -> None:
        """The single guarded factory for a criterion grade — validate, then stamp.

        Raises :class:`RubricError` with a precise reason on the first violation: a
        terminal ``pass``/``fail`` ``status`` (PENDING is not a grade); a non-empty
        ``grader_identity`` that is not a maker/coding-agent/loop role (the same
        ``is_non_reviewer_role`` guard ``MergeClear.issue`` / ``ReviewVerdict.record``
        use — the grader is an INDEPENDENT verifier); a full 40-char hex
        ``reviewed_sha`` (the same bind-to-the-exact-tree rule, so the done-gate's
        head-equality check cannot silently fail on a truncated SHA).
        """
        normalized_status = status.strip().lower()
        valid_grades = {self.Status.PASS.value, self.Status.FAIL.value}
        if normalized_status not in valid_grades:
            msg = f"Unknown grade status {status!r}; a grade is one of {sorted(valid_grades)} (PENDING is not a grade)"
            raise RubricError(msg)
        graded_status = self.Status(normalized_status)

        grader = grader_identity.strip()
        if not grader:
            msg = "grader_identity is required and must be non-empty"
            raise RubricError(msg)
        if is_non_reviewer_role(grader):
            msg = (
                f"grader_identity {grader!r} is a maker/coding-agent/loop role — a rubric is graded by an "
                f"INDEPENDENT verifier, never self-attested by the maker (§17.8 clause 3; mirrors "
                f"ReviewVerdict.record / MergeClear.issue rejecting a non-reviewer author)"
            )
            raise RubricError(msg)

        if not is_commit_sha(reviewed_sha):
            candidate = reviewed_sha.strip()
            msg = (
                f"reviewed_sha {reviewed_sha!r} (length={len(candidate)}) is not a full {SHA_FULL_LEN}-char hex "
                f"commit SHA — a grade binds to the exact reviewed tree so the done-gate's live-head equality "
                f"check can compare it against the forge's headRefOid. Pass the full 40-char SHA (e.g. "
                f"`git rev-parse HEAD`)"
            )
            raise RubricError(msg)

        self.status = graded_status
        self.grader_identity = grader
        self.reviewed_sha = reviewed_sha.strip().lower()
        self.rationale = rationale.strip()
        self.graded_at = timezone.now()
        self.save(update_fields=["status", "grader_identity", "reviewed_sha", "rationale", "graded_at"])

    def is_passing_at(self, head_sha: str) -> bool:
        """True iff this criterion is a PASS by an independent grader bound to ``head_sha``.

        All four conditions are required: ``status == pass``; a non-empty
        ``grader_identity`` that is not a maker/coding-agent/loop role; and the
        recorded ``reviewed_sha`` equals ``head_sha`` (both lower-cased). Any other
        state — PENDING, FAIL, a maker grader, or a stale SHA — is False so the
        rubric fails the done-gate closed.
        """
        target = head_sha.strip().lower()
        grader = self.grader_identity.strip()
        return (
            self.status == self.Status.PASS
            and bool(grader)
            and not is_non_reviewer_role(grader)
            and self.reviewed_sha == target
        )
