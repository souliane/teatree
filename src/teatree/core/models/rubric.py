"""Per-ticket rubric of checkable acceptance criteria graded by an independent verifier (#2241).

Encodes the standing rule "declare done only on a verified, full-spec outcome" as
a durable, mechanical record instead of vibes. Each ticket carries a :class:`Rubric`
of N :class:`RubricCriterion` rows; an independent verifier (the grader, ``!= maker``)
records a per-criterion PASS/FAIL bound to the reviewed tree's SHA. The rubric is
"satisfied" only when EVERY criterion is PASS by a non-maker grader at the current
head — :meth:`Rubric.is_fully_passed_at`. It is the highest-value lever identified
in an earlier loop-design retro: a verifier sub-agent grading a checklist beats
self-critique.

The record follows the durable, compaction-surviving pattern of
:class:`teatree.core.models.review_verdict.ReviewVerdict` / ``MergeClear``: the DB
row is the truth, and the guarded :meth:`RubricCriterion.record_grade` factory shares
``MergeClear``'s validation primitives (``is_commit_sha``, ``is_independent_reviewer_identity``) so
the rubric grade contract and the CLEAR/verdict contract cannot drift apart.

The plan is the PRIMARY producer: ``PlanArtifact.record`` turns its manifest's
``acceptance_criteria`` into rows via :meth:`Rubric.add_criteria`. ``ticket rubric-set``
is the operator seam alongside it, taking explicit criteria.
The LLM grader prior art lives in :mod:`teatree.eval.judge` (``JudgeSpec.rubric`` +
``ClaudeJudge.grade``, the in-process Agent SDK); it is kept SEPARATE here on
purpose — extracting a shared grader would couple the metered-LLM path to this
DB-record path.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.merge_clear import SHA_FULL_LEN, is_commit_sha
from teatree.core.models.reviewer_identity import is_independent_reviewer_identity, unrecognised_reviewer_message
from teatree.core.models.ticket import Ticket
from teatree.core.models.types import RubricGrade
from teatree.quality.falsifiable_criteria import falsifiability_violation

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import QuerySet


#: The probe-first standard, one gradeable criterion per phase that has one.
#:
#: ``core.Rubric`` shipped with a CLI, a manager, a done-gate and an error type, and zero
#: rows — a checklist nobody writes is one nobody grades, so the gate over it could only ever
#: be armed against an empty table. These are the content it lacked: the standard the factory
#: has to hold itself to WITHOUT the owner in the loop, stated so a verifier can fail it.
#:
#: Each is positive by construction — it names something that must be PRESENT — because a
#: criterion satisfied by inaction certifies nothing, which is what
#: :func:`~teatree.quality.falsifiable_criteria.falsifiability_violation` refuses at
#: population and what a seeded criterion must never be.
PHASE_CRITERIA: dict[str, str] = {
    "planning": "the plan cites at least one measurement of the LIVE system, with its venue named",
    "design": "each claim about existing behaviour names the consumer that was read, not prose inferred from it",
    "investigation": "every empty result is paired with a control proving the probe would have found something",
    "fix": "a RED pin exists and was observed failing before the fix",
    "coding": "changed behaviour is asserted by a test that fails without the change",
}


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
    PR's live head SHA; an empty / ungraded / failed / uncited / stale-SHA / maker-graded
    rubric is NOT fully passed — the gate fails CLOSED.
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

        A checklist whose criteria are ALL satisfiable by inaction ("X stays
        unchanged", "the suite passes unmodified") is refused for the same reason
        emptiness is (#3762): no state of the world makes it FAIL, so a correct
        verifier grading it PASS certifies nothing — which is how a silently
        skipped implementation phase gets certified as a success.
        """
        cleaned = [text.strip() for text in criteria if text.strip()]
        if not cleaned:
            msg = "a rubric needs at least one non-empty criterion — an empty rubric passes the gate vacuously"
            raise RubricError(msg)
        violation = falsifiability_violation(cleaned)
        if violation:
            raise RubricError(violation)
        with transaction.atomic():
            rubric, _ = cls.objects.get_or_create(ticket=ticket)
            rubric.criteria.delete()
            RubricCriterion.objects.bulk_create(
                [RubricCriterion(rubric=rubric, ordinal=ordinal, text=text) for ordinal, text in enumerate(cleaned)]
            )
        return rubric

    @classmethod
    def add_criteria(cls, ticket: Ticket, criteria: list[str]) -> "Rubric | None":
        """ADD *criteria* to *ticket*'s rubric, idempotent on text; ``None`` when none are given.

        Additive rather than :meth:`populate`'s replace, and that is the whole design: a
        replace resets every grade, so a ticket graded on its plan would reach coding with
        that grade silently gone, and an operator's own checklist would be overwritten.
        The plan producer relies on both properties — ``plan-reaffirm`` re-records the
        same manifest against a new base SHA, and must not destroy a verifier's grades.

        The checklist is still refused when every criterion is satisfiable by inaction
        (:func:`~teatree.quality.falsifiable_criteria.falsifiability_violation`), judged
        on the criteria being ADDED: an unfalsifiable set certifies nothing whether it
        arrives through the operator seam or the plan.
        """
        cleaned = [text.strip() for text in criteria if text.strip()]
        if not cleaned:
            return None
        violation = falsifiability_violation(cleaned)
        if violation:
            raise RubricError(violation)
        with transaction.atomic():
            rubric, _ = cls.objects.get_or_create(ticket=ticket)
            existing = set(rubric.criteria.values_list("text", flat=True))
            ordinal = rubric.criteria.count()
            for text in cleaned:
                if text in existing:
                    continue
                RubricCriterion.objects.create(rubric=rubric, ordinal=ordinal, text=text)
                existing.add(text)
                ordinal += 1
        return rubric

    @classmethod
    def seed_phase_criterion(cls, ticket: Ticket, phase: str) -> "Rubric | None":
        """ADD *phase*'s standing criterion to *ticket*'s rubric; ``None`` when it has none."""
        text = PHASE_CRITERIA.get(phase)
        if text is None:
            return None
        return cls.add_criteria(ticket, [text])

    @staticmethod
    def normalize_grades(payload: object) -> list[RubricGrade]:
        """Every item of *payload* in the :class:`RubricGrade` shape, or a refusal.

        The ONE per-item normaliser both producers run — the ``rubric-grade`` operator
        command and the reviewing recorder's returned envelope. The recorder normalised
        only the CONTAINER, so a malformed ITEM (a bare string, a ``grade`` key where
        ``status`` belongs, an ordinal that is not a whole number) raised out of
        :meth:`ungraded_ordinals` / :meth:`apply_grades` instead of being refused — and a
        traceback is not a refusal any agent-facing retry can act on.

        A payload that is not a list graded nothing, so it normalises to ``[]`` and the
        caller's coverage question answers it: naming the criteria left PENDING says more
        than a shape refusal would.
        """
        if not isinstance(payload, list):
            return []
        grades: list[RubricGrade] = []
        for item in payload:
            if not isinstance(item, dict):
                msg = f"each grade must be an object, not a {type(item).__name__}: {item!r}"
                raise RubricError(msg)
            if item.get("ordinal") is None or item.get("status") is None:
                msg = f"each grade needs an ordinal and a status: {item!r}"
                raise RubricError(msg)
            grades.append(
                RubricGrade(
                    ordinal=_grade_ordinal(item["ordinal"]),
                    status=str(item["status"]),
                    rationale=str(item.get("rationale", "")),
                )
            )
        return grades

    def ungraded_ordinals(self, grades: "list[RubricGrade]") -> list[int]:
        """The criteria *grades* names no grade for, ascending — the coverage question.

        Asked BEFORE anything is stamped: a verdict that leaves a criterion PENDING
        fails the done-gate closed, so the producer refuses the whole envelope rather
        than record a verdict the merge can never clear. An ordinal no criterion
        carries contributes nothing — grading something that does not exist is not
        coverage of something that does.
        """
        named = {int(grade["ordinal"]) for grade in grades if grade.get("ordinal") is not None}
        return [criterion.ordinal for criterion in self.criteria.all() if criterion.ordinal not in named]

    def apply_grades(self, grades: "list[RubricGrade]", *, grader_identity: str, reviewed_sha: str) -> int:
        """Stamp every grade through the guarded factory, all-or-nothing; return the count.

        ONE atomic, so a refusal on the last grade un-stamps the first: a mid-batch
        refusal used to leave the earlier criteria graded and the rest PENDING, which
        reads to the done-gate as a half-graded rubric nobody chose to record.

        An unknown ordinal and an invalid grade (uncited PASS / maker grader / bad SHA)
        both raise :class:`RubricError` — one refusal type, so a caller that must
        translate a refusal into an envelope error catches once.
        """
        graded = 0
        with transaction.atomic():
            for grade in grades:
                ordinal = grade["ordinal"]
                try:
                    criterion = self.criteria.get(ordinal=ordinal)
                except RubricCriterion.DoesNotExist as exc:
                    msg = f"no criterion with ordinal {ordinal!r} on rubric {self.pk}"
                    raise RubricError(msg) from exc
                criterion.record_grade(
                    status=str(grade["status"]),
                    grader_identity=grader_identity,
                    reviewed_sha=reviewed_sha,
                    rationale=str(grade.get("rationale", "")),
                )
                graded += 1
        return graded

    def unverified_reason(self, head_sha: str | None = None, *, waived: bool = False) -> str:
        """The first reason this rubric is not fully verified, or ``""``.

        The ONE ladder both consumers walk — the delivered-time gate passes no
        *head_sha* (a delivered ticket's head has long since moved past the reviewed
        one), the merge gate passes the live head so the stale rung applies. Each rung
        NAMES the criteria that failed it, so the remediation points at what to fix
        rather than at a count.

        *waived* is the human-authorized plan-bypass, and it keeps exactly one rung: a
        recorded FAIL. A bypass says "there was nothing to declare"; it cannot say "the
        verifier's FAIL does not count".
        """
        criteria = list(self.criteria.all())
        if not waived and not criteria:
            return "the rubric has no criteria recorded"
        for rung in _CRITERION_RUNGS:
            if waived and rung is not _FAIL_RUNG:
                continue
            offending = [c for c in criteria if rung.matches(c)]
            if offending:
                return rung.message(offending, len(criteria))
        if head_sha is None or waived:
            return ""
        return _stale_reason(criteria, head_sha)

    def is_fully_passed_at(self, head_sha: str) -> bool:
        """True iff EVERY criterion is a cited PASS by an independent grader at ``head_sha``."""
        return not self.unverified_reason(head_sha)


class RubricCriterion(models.Model):
    """One checkable acceptance criterion plus its independent-verifier grade.

    The grade is recorded ONLY through the guarded :meth:`record_grade` factory,
    which enforces (mirroring ``ReviewVerdict.record``): a full 40-char hex
    ``reviewed_sha`` (so the head-bind compare cannot silently fail), a non-empty
    ``grader_identity`` that is NOT a maker/coding-agent/loop role (the maker can
    never self-attest a criterion — ``is_independent_reviewer_identity``), and a terminal
    ``pass``/``fail`` status, and — for a PASS — a non-empty ``rationale`` citing what
    proves it. An ungraded criterion stays PENDING and fails the done-gate closed.
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
        ``is_independent_reviewer_identity`` guard ``MergeClear.issue`` / ``ReviewVerdict.record``
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

        cited = rationale.strip()
        if graded_status == self.Status.PASS and not cited:
            msg = (
                "a PASS needs a rationale citing what proves the criterion — a unit, integration, functional "
                "or e2e test, named in free-form prose. An uncited PASS is the 'declared done on an unrun test' "
                "claim this rubric exists to refuse (a FAIL needs no citation)"
            )
            raise RubricError(msg)

        grader = grader_identity.strip()
        if not grader:
            msg = "grader_identity is required and must be non-empty"
            raise RubricError(msg)
        if not is_independent_reviewer_identity(grader):
            raise RubricError(unrecognised_reviewer_message(grader, subject="a rubric criterion", verb="graded"))

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
        self.rationale = cited
        self.graded_at = timezone.now()
        self.save(update_fields=["status", "grader_identity", "reviewed_sha", "rationale", "graded_at"])

    def unverified_reason(self) -> str:
        """Why this criterion is not a cited PASS by an independent grader, or ``""``.

        The single home for per-criterion truth: :meth:`is_verified`,
        :meth:`is_passing_at` and :meth:`Rubric.unverified_reason` all read the same
        ordered :data:`_CRITERION_RUNGS`, so the rubric-level refusal can never
        disagree with the criterion-level predicate.
        """
        for rung in _CRITERION_RUNGS:
            if rung.matches(self):
                return rung.reason
        return ""

    def is_verified(self) -> bool:
        """True iff this criterion is a CITED PASS by an independent grader, ignoring the head bind."""
        return not self.unverified_reason()

    def is_passing_at(self, head_sha: str) -> bool:
        """True iff this criterion :meth:`is_verified` AND its grade is bound to ``head_sha``."""
        return self.is_verified() and self.reviewed_sha == head_sha.strip().lower()


@dataclass(frozen=True)
class _CriterionRung:
    """One rung of the unverified ladder: what disqualifies a criterion, and how it reads."""

    matches: "Callable[[RubricCriterion], bool]"
    reason: str
    remedy: str

    def message(self, offending: "list[RubricCriterion]", total: int) -> str:
        return f"{len(offending)} of {total} criteria are {self.reason} — {_named(offending)} — {self.remedy}"


def _grade_ordinal(value: object) -> int:
    """*value* as the int the criterion lookup keys on, or a refusal naming the field.

    A JSON ``"0"`` is the shape a hand-written payload most often takes and means
    exactly criterion 0, so it is coerced. Anything that is not a whole number is
    refused rather than silently truncated — a grade aimed at a criterion nobody can
    name is worse than no grade.
    """
    if isinstance(value, bool):
        msg = f"a grade ordinal must be an integer, not a boolean: {value!r}"
        raise RubricError(msg)
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        msg = f"a grade ordinal must be an integer naming a criterion: {value!r}"
        raise RubricError(msg) from exc


def _named(criteria: "list[RubricCriterion]") -> str:
    return ", ".join(f"#{criterion.ordinal} {criterion.text[:60]!r}" for criterion in criteria)


_FAIL_RUNG = _CriterionRung(
    matches=lambda c: c.status == RubricCriterion.Status.FAIL,
    reason="graded FAIL",
    remedy="every criterion must PASS",
)

#: The ladder, in refusal order. ``Rubric.unverified_reason`` reports the FIRST rung any
#: criterion trips; a waived rubric evaluates only :data:`_FAIL_RUNG`.
_CRITERION_RUNGS: tuple[_CriterionRung, ...] = (
    _CriterionRung(
        matches=lambda c: c.status == RubricCriterion.Status.PENDING,
        reason="ungraded (fail-closed)",
        remedy="every criterion must be graded",
    ),
    _FAIL_RUNG,
    _CriterionRung(
        matches=lambda c: not c.rationale.strip(),
        reason="graded PASS with no rationale",
        remedy=(
            "a PASS must cite what proves it (a unit, integration, functional or e2e test; free-form prose naming it)"
        ),
    ),
    _CriterionRung(
        matches=lambda c: not is_independent_reviewer_identity(c.grader_identity.strip()),
        reason="graded by an identity that is not a recognised independent verifier",
        remedy="a rubric is graded by an INDEPENDENT verifier, never the maker",
    ),
)


def _stale_reason(criteria: "list[RubricCriterion]", head_sha: str) -> str:
    """The merge-time rung: a grade bound to a head the branch has since moved off."""
    target = head_sha.strip().lower()
    stale = [criterion for criterion in criteria if criterion.reviewed_sha != target]
    if not stale:
        return ""
    recorded = stale[0].reviewed_sha
    return (
        f"{len(stale)} of {len(criteria)} criteria were graded against head "
        f"{recorded[:8] or recorded!r}, not the current head {target[:8] or target!r} — {_named(stale)} — the grade "
        f"is stale (force-push / new commits); re-grade at the current SHA"
    )
