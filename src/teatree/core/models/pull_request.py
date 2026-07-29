from typing import TYPE_CHECKING, ClassVar

from django.db import models
from django.utils import timezone
from django_fsm import FSMField, transition

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class PullRequestQuerySet(models.QuerySet):
    def for_pr(self, *, slug: str, pr_id: int) -> "models.QuerySet":
        return self.filter(repo=slug, iid=str(pr_id))

    def owning_ticket(self, *, slug: str, pr_id: int, pr_url: str = "") -> "Ticket | None":
        """The delivery ticket that owns *(slug, pr_id)*, or ``None``.

        The FK is authoritative (the ship pipeline persists it). ``Ticket.extra["prs"]``
        is the fallback for a PR opened outside the pipeline, which has no row at all.
        A ticket whose ``issue_url`` merely equals the PR url is never the owner — that
        shape is the reviewer-role ticket, which carries no delivery lease.
        """
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415 — deferred: sibling model, imported at call time

        row = self.for_pr(slug=slug, pr_id=pr_id).select_related("ticket").order_by("-id").first()
        if row is not None:
            return row.ticket
        if not pr_url:
            return None
        for ticket in Ticket.objects.exclude(extra={}).only("issue_url", "extra", "id"):
            prs = ticket.extra.get("prs") if isinstance(ticket.extra, dict) else None
            if isinstance(prs, dict) and pr_url in prs:
                return ticket
        return None

    def record_forge_merge(self, *, slug: str, pr_id: int) -> int:
        """Transition every non-merged row for *(slug, pr_id)* to MERGED; return the count.

        The merge keystone is the authoritative moment a PR becomes merged — the
        open-PR-only scanner that used to be the sole caller of :meth:`PullRequest.mark_merged`
        can never observe a PR that merged between two of its ticks.
        """
        merged = 0
        for row in self.for_pr(slug=slug, pr_id=pr_id).exclude(state=PullRequest.State.MERGED):
            row.mark_merged()
            row.save(update_fields=["state"])
            merged += 1
        return merged


class PullRequest(models.Model):
    """The system of record for a ticket's pull requests — the PR-facts ARBITER (F1.3).

    A ``PullRequest`` row (its FSM ``state`` in particular) is the AUTHORITY for
    PR facts. ``Ticket.extra['prs']`` (see ``PREntrySerialized``) is a
    DENORMALIZED SYNC CACHE: a JSON snapshot the dashboard read-model consumes,
    kept in step with these rows but never co-equal with them. When the two
    disagree the row wins; a stale ``extra['prs']`` entry is a cache-refresh gap,
    not a rival source of truth. Read-path unification of the two stores is
    deferred — this declaration records the arbiter so the ambiguity is at least
    documented in the interim.
    """

    class State(models.TextChoices):
        OPEN = "open", "Open"
        REVIEW_REQUESTED = "review_requested", "Review requested"
        APPROVED = "approved", "Approved"
        MERGED = "merged", "Merged"

    class CreateVerification(models.TextChoices):
        """Whether a row's forge PR was verify-by-re-read confirmed at creation (#1194).

        A ``create_pr`` returning a URL is not proof the PR is actually live —
        an eventual-consistency race, a mis-resolved cross-project mirror, or a
        ``gh``/``glab`` exit-0 no-op can all produce a URL for a PR that a fresh
        GET does not find. A row is only persisted once an independent re-read
        CONFIRMED the PR exists, so ``CONFIRMED`` is the standing invariant for a
        real row; ``PENDING`` is the pre-verify default (legacy rows / mid-write).
        """

        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"

    ticket = models.ForeignKey("core.Ticket", on_delete=models.CASCADE, related_name="pull_requests")
    overlay = models.CharField(max_length=255, blank=True)
    url = models.URLField(max_length=500)
    repo = models.CharField(max_length=255)
    iid = models.CharField(max_length=50)
    slack_url = models.URLField(max_length=500, blank=True)
    review_requested_at = models.DateTimeField(null=True, blank=True)
    state = FSMField(max_length=32, choices=State.choices, default=State.OPEN)
    create_verification = models.CharField(
        max_length=16,
        choices=CreateVerification.choices,
        default=CreateVerification.PENDING,
    )
    create_verified_at = models.DateTimeField(null=True, blank=True)

    objects: ClassVar[PullRequestQuerySet] = PullRequestQuerySet.as_manager()

    class Meta:
        db_table = "teatree_pull_request"
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["url"],
                name="unique_pull_request_url",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.repo} #{self.iid}"

    @transition(field=state, source=State.OPEN, target=State.REVIEW_REQUESTED)
    def request_review(self, *, slack_url: str = "", review_requested_at: "models.DateTimeField | None" = None) -> None:
        if slack_url:
            self.slack_url = slack_url
        self.review_requested_at = review_requested_at or timezone.now()

    @transition(field=state, source=State.REVIEW_REQUESTED, target=State.APPROVED)
    def approve(self) -> None:
        pass

    @transition(field=state, source=[State.OPEN, State.REVIEW_REQUESTED, State.APPROVED], target=State.MERGED)
    def mark_merged(self) -> None:
        pass
