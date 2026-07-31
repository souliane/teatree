from typing import TYPE_CHECKING, ClassVar

from django.db import models
from django.utils import timezone
from django_fsm import FSMField, transition

from teatree.url_classify import repo_and_iid

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.types import JSONObject


class PullRequestQuerySet(models.QuerySet):
    def for_pr(self, *, slug: str, pr_id: int) -> "models.QuerySet":
        """Every row for *(slug, pr_id)*, matching the slug case-INSENSITIVELY.

        A forge slug is case-insensitive, so a row recorded as ``Owner/Repo``
        names the very PR an ``owner/repo`` merge just landed. Matching it
        exactly marks zero rows and resolves no ticket, which is indistinguishable
        from "this PR has no records" — the same reason the §15 sibling supersede
        and the merge-quality gate both resolve slugs with ``__iexact``.
        """
        return self.filter(repo__iexact=slug, iid=str(pr_id))

    def owning_ticket(self, *, slug: str, pr_id: int, pr_url: str = "") -> "Ticket | None":
        """The delivery ticket that owns *(slug, pr_id)*, or ``None``.

        The FK is authoritative. The JSON fallback covers a PR with no row —
        one opened outside the pipeline, or opened before the row write existed —
        and reads EVERY store a ticket records its own PRs in, because a resolver
        that reads one store while the writer fills another resolves nothing: the
        keystone then has no FSM to advance and a merge lands with the board
        unmoved (#3840). A ticket whose ``issue_url`` merely equals the PR url is
        never the owner — that shape is the reviewer-role ticket, which carries no
        delivery lease.
        """
        row = self.for_pr(slug=slug, pr_id=pr_id).select_related("ticket").order_by("-id").first()
        if row is not None:
            return row.ticket
        return self._ticket_carrying_pr(slug=slug, pr_id=pr_id, pr_url=pr_url)

    @staticmethod
    def _names_pr(key: str, *, slug: str, pr_id: int, pr_url: str) -> bool:
        """True iff the recorded url *key* is this PR.

        Keyed on the PARSED ``(slug, pr_id)`` as well as the literal url, because
        the callers that most need this arm — the merge keystone and the CLEAR
        backfill — hold only that pair: a ``MergeClear`` carries no PR url.
        """
        if pr_url and key == pr_url:
            return True
        ref = repo_and_iid(key)
        return ref is not None and ref[0].casefold() == slug.casefold() and ref[1] == pr_id

    @staticmethod
    def _recorded_pr_urls(extra: "JSONObject | None") -> list[str]:
        """Every PR url a ticket's own ``extra`` names, across all three stores.

        ``prs`` is the forge-sync map (url → payload); ``pr_urls`` is the flat list
        and ``pr_url_by_branch`` the per-branch index, both written by the ship
        pipeline the moment it opens a PR. All three are read together so which
        writer ran never decides whether the PR is attributable.
        """
        if not isinstance(extra, dict):
            return []
        urls: list[str] = []
        synced = extra.get("prs")
        if isinstance(synced, dict):
            urls.extend(key for key in synced if isinstance(key, str))
        by_branch = extra.get("pr_url_by_branch")
        if isinstance(by_branch, dict):
            urls.extend(value for value in by_branch.values() if isinstance(value, str))
        recorded = extra.get("pr_urls")
        if isinstance(recorded, list):
            urls.extend(item for item in recorded if isinstance(item, str))
        return urls

    @classmethod
    def _ticket_carrying_pr(cls, *, slug: str, pr_id: int, pr_url: str) -> "Ticket | None":
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415 — deferred: sibling model, imported at call time

        for ticket in Ticket.objects.exclude(extra={}).only("issue_url", "extra", "id"):
            urls = cls._recorded_pr_urls(ticket.extra)
            if any(cls._names_pr(url, slug=slug, pr_id=pr_id, pr_url=pr_url) for url in urls):
                return ticket
        return None

    def record_opened(self, *, ticket: "Ticket", url: str, overlay: str = "") -> "PullRequest | None":
        """Persist the arbiter row for a PR *ticket* just opened; ``None`` if *url* names none.

        ``PullRequest`` is the PR-facts arbiter every merge-time consumer resolves
        through — the keystone's ticket adoption, the board reconcile's merged-row
        rule, the merge-evidence gate. Writing it only from the tick-time open-PR
        reconciler meant a PR that opened and merged between two ticks never got a
        row at all, so those consumers had nothing to read. The row is written by
        the path that opens the PR, where the ticket is already in hand.

        Idempotent on the PR url (its unique key), so a ship retry reuses the row.
        ``create_verification`` is stamped CONFIRMED because the ship path persists
        only after its verify-by-re-read confirmed the PR is live.
        """
        ref = repo_and_iid(url)
        if ref is None:
            return None
        row, _ = self.get_or_create(
            url=url,
            defaults={
                "ticket": ticket,
                "overlay": overlay or ticket.overlay,
                "repo": ref[0],
                "iid": str(ref[1]),
                "create_verification": PullRequest.CreateVerification.CONFIRMED,
                "create_verified_at": timezone.now(),
            },
        )
        return row

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

    def live(self) -> "PullRequestQuerySet":
        """Rows that still evidence an attempt in flight — neither landed nor abandoned.

        The ONE definition of "this ticket has an open PR", shared by every liveness
        reader (the #3978 intake-budget release rules and its doctor alarm). MERGED and
        CLOSED are both settled: the first succeeded, the second was given up on, and
        neither is a reason to keep holding an in-flight budget slot.
        """
        return self.exclude(state__in=(PullRequest.State.MERGED, PullRequest.State.CLOSED))

    @staticmethod
    def settle_forge_state(row: "PullRequest", live_state: str) -> bool:
        """Record a live forge verdict on *row*; ``True`` when the row moved.

        The forge is the authority on whether a PR is still live, and every caller
        that pays for that read (the teardown gate re-reads each non-MERGED row)
        otherwise discards the answer — so a PR closed without merging kept a row
        saying OPEN forever, and the dashboard chip built from it advertised live
        work that no longer exists.

        Only the two TERMINAL verdicts settle a row. ``open`` is not news, and
        ``unknown`` is the fail-open value every reader maps an auth error, a
        network failure or an unparsable payload to — settling on it would let a
        transient outage mark live PRs closed.
        """
        settle = {"merged": PullRequest.State.MERGED, "closed": PullRequest.State.CLOSED}.get(str(live_state))
        if settle is None or row.state == settle:
            return False
        if settle is PullRequest.State.MERGED:
            row.mark_merged()
        else:
            row.mark_closed()
        row.save(update_fields=["state"])
        return True


PullRequestManager = models.Manager.from_queryset(PullRequestQuerySet)


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
        #: Closed on the forge without merging — terminal, and NOT merged. Without it
        #: every abandoned PR stayed non-MERGED forever, so a chip rendered from the row
        #: advertised live work indefinitely and the teardown gate had to re-probe the
        #: forge on every pass to learn the same settled answer again.
        CLOSED = "closed", "Closed"

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

    objects = PullRequestManager()

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

    @transition(field=state, source=[State.OPEN, State.REVIEW_REQUESTED, State.APPROVED], target=State.CLOSED)
    def mark_closed(self) -> None:
        """Record that the forge closed this PR WITHOUT merging it.

        MERGED is deliberately not a source: a merge is irreversible and terminal,
        so a later "not open" reading of a merged PR must never demote it to CLOSED.
        """
