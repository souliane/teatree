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
``record`` refuses a maker/coding/loop ``grader_identity`` via the same
``is_non_reviewer_role`` primitive ``ReviewVerdict``/``MergeClear`` use — a
self-attestation is a typed refusal.

Anti-theater (SIG-PR-1 never-fake-green): a PASS item with NO citation is stored as
``instrumentation_gap`` and COUNTS AS A FAIL — a lazy model that waves an item
through without naming the artifact it inspected does not get a free pass. The
verdict is ADVISORY (the gate records findings from it, never blocks on it), so the
"no LLM in the blocking path" property holds; the blocking teeth stay the
deterministic prechecks.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from django.db import models
from django.utils import timezone

from teatree.core.models.merge_clear import is_non_reviewer_role

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class CriticVerdictError(ValueError):
    """A ``CriticVerdict`` was rejected at record time — the maker≠checker contract failed."""


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

    OK: ClassVar[str] = "pass"  # the clean status VALUE the LLM returns; named OK to avoid the S105 name heuristic
    FAIL: ClassVar[str] = "fail"
    INSTRUMENTATION_GAP: ClassVar[str] = "instrumentation_gap"

    @classmethod
    def coerce(cls, raw: dict) -> "CriticItemVerdict":
        slug = str(raw.get("slug") or "").strip()
        status = str(raw.get("status") or "").strip().lower()
        citation = str(raw.get("citation") or "").strip()
        if status not in {cls.OK, cls.FAIL, cls.INSTRUMENTATION_GAP}:
            status = cls.INSTRUMENTATION_GAP  # an unknown/blank status is inconclusive, never a silent pass
        if status == cls.OK and not citation:
            status = cls.INSTRUMENTATION_GAP  # a pass must cite the artifact it inspected
        return cls(slug=slug, status=status, citation=citation)

    def is_fail(self) -> bool:
        return self.status in {self.FAIL, self.INSTRUMENTATION_GAP}

    def as_dict(self) -> dict:
        return {"slug": self.slug, "status": self.status, "citation": self.citation}


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

    def __str__(self) -> str:
        return f"critic-verdict<ticket:{self.ticket_id} {self.transition}@{self.head_sha[:8]}>"  # type: ignore[attr-defined]  # Django FK accessor

    def item_verdicts(self) -> list[CriticItemVerdict]:
        return [CriticItemVerdict.coerce(item) for item in self.items if isinstance(item, dict)]

    def failed_items(self) -> list[CriticItemVerdict]:
        """Every item the verdict FAILED — a genuine fail OR an uncited/inconclusive one."""
        return [item for item in self.item_verdicts() if item.is_fail()]

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
        """The single guarded factory — refuses a maker-graded verdict before any write.

        Mirrors ``ReviewVerdict.record``'s maker≠checker refusal: a
        ``grader_identity`` that is a maker/coding-agent/loop role is a
        self-attestation and is rejected (:class:`CriticVerdictError`). An empty
        identity is likewise refused — an anonymous verdict is unattributable.
        """
        grader = grader_identity.strip()
        if not grader:
            msg = "grader_identity is required — an anonymous critic verdict is unattributable"
            raise CriticVerdictError(msg)
        if is_non_reviewer_role(grader):
            msg = (
                f"grader_identity {grader!r} is a maker/coding-agent/loop role — the critic verdict "
                f"records an INDEPENDENT judgment, never a self-attestation (maker≠checker, mirrors "
                f"ReviewVerdict.record / MergeClear.issue)"
            )
            raise CriticVerdictError(msg)
        return cls.objects.create(
            ticket=ticket,
            transition=transition,
            head_sha=head_sha.strip().lower(),
            grader_identity=grader,
            items=[item.as_dict() for item in items],
        )

    @classmethod
    def record_from_envelope(
        cls, *, ticket: "Ticket", transition: str, head_sha: str, envelope: dict
    ) -> "CriticVerdict":
        """Record a returned ``critic_verdict`` envelope (corr-11 shape).

        The envelope is ``{grader_identity, items: [{slug, status, citation}, …]}``.
        Item statuses pass through :meth:`CriticItemVerdict.coerce`, so an uncited
        pass is stored as ``instrumentation_gap`` at the boundary.
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
