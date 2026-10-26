from typing import TYPE_CHECKING, ClassVar, TypedDict

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.finding_standard import FindingKind
from teatree.core.models.reviewer_identity import normalize_reviewer_identity

if TYPE_CHECKING:
    from teatree.core.models.review_verdict import ReviewVerdict


class HoldPointResponse(TypedDict):
    index: int
    classification: str
    citation: str


class ReviewHoldDispositionError(ValueError):
    pass


def _check_pair(hold: "ReviewVerdict", clearing_verdict: "ReviewVerdict") -> None:
    if hold.verdict != "hold" or clearing_verdict.verdict != "merge_safe":
        msg = "a hold requires a later merge-safe verdict"
        raise ReviewHoldDispositionError(msg)
    if (
        hold.slug.casefold() != clearing_verdict.slug.casefold()
        or hold.pr_id != clearing_verdict.pr_id
        or hold.reviewed_sha != clearing_verdict.reviewed_sha
    ):
        msg = "the clearing verdict must cover the same PR and head"
        raise ReviewHoldDispositionError(msg)
    if clearing_verdict.pk <= hold.pk or normalize_reviewer_identity(
        clearing_verdict.reviewer_identity
    ) == normalize_reviewer_identity(hold.reviewer_identity):
        msg = "a later independent reviewer must answer the hold"
        raise ReviewHoldDispositionError(msg)
    if hold.gh_verify_result == "failed":
        msg = "a confirmed failed check cannot be dispositioned"
        raise ReviewHoldDispositionError(msg)


def _check_points(hold: "ReviewVerdict", points: list[HoldPointResponse]) -> None:
    required = ReviewHoldDisposition.blocking_indices(hold)
    answered = [point.get("index") for point in points]
    if len(set(answered)) != len(answered) or set(answered) != required:
        msg = f"every held point requires a response; unanswered: {sorted(required - set(answered))}"
        raise ReviewHoldDispositionError(msg)
    for point in points:
        if (
            point.get("classification") not in {"venue_limit", "disproved_premise"}
            or not str(point.get("citation", "")).strip()
        ):
            msg = "each response requires a classification and citation"
            raise ReviewHoldDispositionError(msg)
        if (
            point["classification"] == "venue_limit"
            and point["index"] >= 0
            and hold.structured_findings[point["index"]].basis != "venue_limit"
        ):
            msg = "venue_limit can answer only a venue-limit finding"
            raise ReviewHoldDispositionError(msg)


class ReviewHoldDisposition(models.Model):
    hold = models.ForeignKey("core.ReviewVerdict", on_delete=models.PROTECT, related_name="hold_dispositions")
    clearing_verdict = models.ForeignKey("core.ReviewVerdict", on_delete=models.PROTECT, related_name="cleared_holds")
    points = models.JSONField(default=list)
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_review_hold_disposition"
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["hold", "clearing_verdict"], name="uniq_review_hold_disposition_pair")
        ]

    def __str__(self) -> str:
        return f"review-hold-disposition<{self.pk}>"

    @classmethod
    def record(
        cls, *, hold: "ReviewVerdict", clearing_verdict: "ReviewVerdict", points: list[HoldPointResponse]
    ) -> "ReviewHoldDisposition":
        _check_pair(hold, clearing_verdict)
        _check_points(hold, points)
        with transaction.atomic():
            row, created = cls.objects.get_or_create(
                hold=hold, clearing_verdict=clearing_verdict, defaults={"points": points}
            )
            if not created and row.points != points:
                msg = "a recorded disposition is immutable"
                raise ReviewHoldDispositionError(msg)
            return row

    @staticmethod
    def blocking_indices(hold: "ReviewVerdict") -> set[int]:
        indices = {
            index for index, finding in enumerate(hold.structured_findings) if finding.kind == FindingKind.BLOCKING
        }
        return indices or {-1}

    def covers(self, hold: "ReviewVerdict") -> bool:
        return {point["index"] for point in self.points} == self.blocking_indices(hold)
