"""The async user-proxy critic's LLM verdict (SELFCATCH-5) — the semantic half.

The deterministic prechecks (``done_not_done`` / ``spec_not_plan`` / ``completeness``)
catch the mechanical classes at ``mark_delivered``. The SEMANTIC classes the human
had to point out — concept-conflation, duplication, deferred-by-prose, ignored
input, unenforced guarantees — can only be judged by reading the delivered
artifacts, so a headless critic (:class:`~teatree.core.models.critic_dispatch.CriticDispatch`)
reads them and RETURNS a ``critic_verdict`` envelope. This model is the durable
record of that verdict — the mirror of :class:`~teatree.core.models.review_verdict.ReviewVerdict`
the plan calls for: keyed ``(ticket, transition, head_sha)``, per-item PASS/FAIL +
mandatory citation, with ``grader_identity`` validated maker≠checker.

Maker≠checker is structural, not prompt-level: the verdict is recorded server-side
from the returned envelope by a DIFFERENT actor (``attempt_recorder``), and
``record`` admits a ``grader_identity`` only when it POSITIVELY identifies an
independent checker, via the same ``is_independent_reviewer_identity`` primitive
``ReviewVerdict``/``MergeClear`` use — a self-attestation is a typed refusal.

Anti-theater (SIG-PR-1 never-fake-green): a PASS item with NO citation is stored as
``instrumentation_gap`` and COUNTS AS A FAIL — a lazy model that waves an item
through without naming the artifact it inspected does not get a free pass. The
verdict is ADVISORY (the gate records findings from it, never blocks on it), so the
"no LLM in the blocking path" property holds; the blocking teeth stay the
deterministic prechecks.
"""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.critic_dispatch import CriticDispatch
from teatree.core.models.critic_points import CriticPoint, PriorFindingResolution
from teatree.core.models.finding_standard import FindingKind
from teatree.core.models.reviewer_identity import is_independent_reviewer_identity, unrecognised_reviewer_message

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class CriticVerdictError(ValueError):
    """A ``CriticVerdict`` was rejected at record time — the maker≠checker contract failed."""


class CriticEvidenceUnavailableError(CriticVerdictError):
    pass


CRITIC_EVIDENCE_UNAVAILABLE_PREFIX = "critic evidence unavailable:"


@dataclass(frozen=True, slots=True)
class CriticItemVerdict:
    """One rubric item's LLM judgment: the slug, its status, and the citing artifact.

    ``status`` is normalised to ``fail`` / ``pass`` / ``instrumentation_gap``. A
    ``pass`` with a blank ``citation`` is downgraded to ``instrumentation_gap`` at
    construction (the never-fake-green rule) so an uncited pass can never read as a
    genuine pass downstream.
    """

    slug: str
    status: str
    citation: str = ""
    verified: bool = True
    points: tuple[CriticPoint, ...] = ()
    resolutions: tuple[PriorFindingResolution, ...] = ()

    OK: ClassVar[str] = "pass"  # the clean status VALUE the LLM returns; named OK to avoid the S105 name heuristic
    FAIL: ClassVar[str] = "fail"
    INSTRUMENTATION_GAP: ClassVar[str] = "instrumentation_gap"
    UNVERIFIED: ClassVar[str] = "unverified"

    @classmethod
    def coerce(cls, raw: dict, *, nit_follow_up: bool = False) -> "CriticItemVerdict":
        slug = str(raw.get("slug") or "").strip()
        status = str(raw.get("status") or "").strip().lower()
        citation = str(raw.get("citation") or "").strip()
        verified = raw.get("verified", True) is True
        raw_points = raw.get("points")
        points = (
            tuple(
                CriticPoint.coerce(point)
                if isinstance(point, dict)
                else CriticPoint(FindingKind.BLOCKING, "malformed point")
                for point in raw_points
            )
            if isinstance(raw_points, list)
            else ()
        )
        raw_resolutions = raw.get("resolutions")
        resolutions = (
            tuple(
                PriorFindingResolution.coerce(resolution)
                for resolution in raw_resolutions
                if isinstance(resolution, dict)
            )
            if isinstance(raw_resolutions, list)
            else ()
        )
        if nit_follow_up:
            points = tuple(replace(point, kind=FindingKind.BLOCKING) for point in points)
        if not verified or status == cls.UNVERIFIED:
            return cls(
                slug=slug,
                status=cls.UNVERIFIED,
                citation=citation,
                verified=False,
                points=points,
                resolutions=resolutions,
            )
        if status not in {cls.OK, cls.FAIL, cls.INSTRUMENTATION_GAP}:
            status = cls.INSTRUMENTATION_GAP  # an unknown/blank status is inconclusive, never a silent pass
        if status == cls.OK and not citation:
            status = cls.INSTRUMENTATION_GAP  # a pass must cite the artifact it inspected
        if any(point.kind == FindingKind.BLOCKING for point in points):
            status = cls.FAIL
        elif points and status == cls.FAIL:
            status = cls.OK if citation else cls.INSTRUMENTATION_GAP
        return cls(slug=slug, status=status, citation=citation, verified=True, points=points, resolutions=resolutions)

    def is_fail(self) -> bool:
        return self.status in {self.FAIL, self.INSTRUMENTATION_GAP}

    def as_dict(self) -> dict:
        return {
            "slug": self.slug,
            "status": self.status,
            "citation": self.citation,
            "verified": self.verified,
            "points": [point.as_dict() for point in self.points],
            "resolutions": [resolution.as_dict() for resolution in self.resolutions],
        }


class CriticVerdictManager(models.Manager["CriticVerdict"]):
    def latest_for(self, *, ticket: "Ticket", transition: str, head_sha: str = "") -> "CriticVerdict | None":
        """The freshest verdict for ``(ticket, transition)`` — optionally pinned to *head_sha*.

        A blank *head_sha* returns the newest verdict regardless of head (the
        advisory read); a non-blank one pins to the exact delivered tree.
        """
        qs = self.filter(ticket=ticket, transition=transition)
        if head_sha.strip():
            qs = qs.filter(head_sha=head_sha.strip().lower())
        return qs.order_by("-recorded_at", "-pk").first()


class CriticVerdict(models.Model):
    """One recorded LLM critic judgment over a ticket's delivery at an exact tree."""

    ticket = models.ForeignKey("core.Ticket", on_delete=models.CASCADE, related_name="critic_verdicts")
    transition = models.CharField(max_length=64)
    head_sha = models.CharField(max_length=64, blank=True, default="")
    grader_identity = models.CharField(max_length=255)
    items = models.JSONField(default=list, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now)

    objects: ClassVar[CriticVerdictManager] = CriticVerdictManager()

    class Meta:
        db_table = "teatree_critic_verdict"
        ordering: ClassVar = ["-recorded_at"]
        indexes: ClassVar = [models.Index(fields=["ticket", "transition", "head_sha"])]

    if TYPE_CHECKING:
        # Django synthesises the ``<fk>_id`` shadow attribute at class-prep time —
        # invisible to a static checker. Declared here (annotation-only, never
        # evaluated at runtime) so ``__str__`` reads the id without a relation query.
        ticket_id: int

    def __str__(self) -> str:
        return f"critic-verdict<ticket:{self.ticket_id} {self.transition}@{self.head_sha[:8]}>"

    def item_verdicts(self) -> list[CriticItemVerdict]:
        return [CriticItemVerdict.coerce(item) for item in self.items if isinstance(item, dict)]

    def failed_items(self) -> list[CriticItemVerdict]:
        """Every item the verdict FAILED — a genuine fail OR an uncited/inconclusive one."""
        return [item for item in self.item_verdicts() if item.is_fail()]

    @classmethod
    def _require_previous_resolutions(
        cls, *, ticket: "Ticket", transition: str, head_sha: str, items: list[CriticItemVerdict]
    ) -> None:
        if transition != "merge":
            return
        previous = (
            cls.objects.filter(ticket=ticket, transition=transition)
            .exclude(head_sha=head_sha)
            .order_by("-recorded_at", "-pk")
            .first()
        )
        if previous is None:
            return
        current_items = {item.slug: item for item in items}
        for old in previous.item_verdicts():
            findings = tuple(point.summary for point in old.points)
            if not findings and old.is_fail():
                findings = (old.slug,)
            current = current_items.get(old.slug)
            resolutions = {item.finding: item for item in current.resolutions} if current is not None else {}
            for finding in findings:
                resolution = resolutions.get(finding)
                if resolution is None or resolution.status not in {"fixed", "not_fixed"} or not resolution.citation:
                    msg = f"previous findings need a cited fixed/not_fixed resolution: {old.slug}/{finding}"
                    raise CriticEvidenceUnavailableError(msg)
                was_blocking = any(
                    point.summary == finding and point.kind == FindingKind.BLOCKING for point in old.points
                )
                if resolution.status == "not_fixed" and was_blocking and current is not None and not current.is_fail():
                    msg = f"unfixed blocking previous finding must fail its item: {old.slug}/{finding}"
                    raise CriticEvidenceUnavailableError(msg)

    @classmethod
    def record(
        cls,
        *,
        ticket: "Ticket",
        transition: str,
        head_sha: str,
        grader_identity: str,
        items: list[CriticItemVerdict],
    ) -> "CriticVerdict":
        """The single guarded factory — refuses a maker-graded or itemless verdict before any write.

        Mirrors ``ReviewVerdict.record``'s maker≠checker refusal: a
        ``grader_identity`` that does not positively identify an independent
        checker is rejected (:class:`CriticVerdictError`). An empty identity is
        likewise refused — an anonymous verdict is unattributable.

        An ITEMLESS verdict is refused too, and for the same never-fake-green
        reason ``CriticItemVerdict.coerce`` downgrades an uncited pass: a verdict
        that judged nothing would still COVER the head and retire its
        :class:`CriticDispatch` terminally, so the merge-quality gate would refuse
        every merge at that tree with no critic left to re-arm. Refusing here
        leaves the dispatch DISPATCHED, so it expires and is re-armed.
        """
        grader = grader_identity.strip()
        if not grader:
            msg = "grader_identity is required — an anonymous critic verdict is unattributable"
            raise CriticVerdictError(msg)
        if not is_independent_reviewer_identity(grader):
            raise CriticVerdictError(unrecognised_reviewer_message(grader, subject="a critic verdict", verb="recorded"))
        if not items:
            msg = (
                "items is required — an itemless verdict judges nothing yet would cover the delivered "
                "head and spend its dispatch claim, leaving the gate unsatisfiable at that tree"
            )
            raise CriticVerdictError(msg)
        if isinstance(ticket.extra, dict) and ticket.extra.get("nit_follow_up") is True:
            items = [CriticItemVerdict.coerce(item.as_dict(), nit_follow_up=True) for item in items]
        if any(item.status == CriticItemVerdict.UNVERIFIED or not item.verified for item in items):
            msg = "one or more critic items were not graded from the change; retry the critic with diff-reading tools"
            raise CriticEvidenceUnavailableError(msg)
        normalized_head = head_sha.strip().lower()
        with transaction.atomic():
            cls._require_previous_resolutions(
                ticket=ticket, transition=transition, head_sha=normalized_head, items=items
            )
            recorded = cls.objects.create(
                ticket=ticket,
                transition=transition,
                head_sha=normalized_head,
                grader_identity=grader,
                items=[item.as_dict() for item in items],
            )
            # The claim this verdict concludes, retired in the same transaction:
            # a verdict covers this exact delivered tree, so the head is spent
            # and must never be re-armed by the expiry reclaim (#3920).
            CriticDispatch.mark_resolved(ticket=ticket, transition=transition, head_sha=normalized_head)
            return recorded

    @classmethod
    def record_from_envelope(
        cls, *, ticket: "Ticket", transition: str, head_sha: str, envelope: dict
    ) -> "CriticVerdict":
        """Record a returned ``critic_verdict`` envelope (corr-11 shape).

        The envelope is ``{grader_identity, items: [{slug, status, citation}, …]}``.
        Item statuses pass through :meth:`CriticItemVerdict.coerce`, so an uncited
        pass is stored as ``instrumentation_gap`` at the boundary; a missing or
        empty ``items`` reaches :meth:`record`'s itemless refusal rather than
        recording a verdict that judged nothing.
        """
        raw_items = envelope.get("items")
        items = (
            [CriticItemVerdict.coerce(item) for item in raw_items if isinstance(item, dict)]
            if isinstance(raw_items, list)
            else []
        )
        return cls.record(
            ticket=ticket,
            transition=transition,
            head_sha=head_sha,
            grader_identity=str(envelope.get("grader_identity") or ""),
            items=items,
        )
