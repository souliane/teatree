"""The audited record that a forge merge never delivered its content to the default branch."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, TypedDict

from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Iterable

    from teatree.core.models.ticket import Ticket


class TicketRestore(TypedDict):
    ticket_id: int
    from_state: str
    to_state: str


class TicketKept(TypedDict):
    ticket_id: int
    reason: str


class PullRequestClose(TypedDict):
    pull_request_id: int
    from_state: str
    to_state: str


class VoidEvidence(TypedDict):
    tickets: list[TicketRestore]
    kept: list[TicketKept]
    pull_requests: list[PullRequestClose]
    open_clears: list[int]


class MergeVoidFields(TypedDict):
    host_kind: str
    slug: str
    pr_id: int
    pr_url: str
    merged_sha: str
    content_head: str
    merge_base: str
    default_branch: str
    default_tip: str
    changed_paths: list[str]
    evidence: VoidEvidence
    reason: str
    actor: str


@dataclass(frozen=True, slots=True)
class VoidedRefs:
    refs: frozenset[tuple[str, str, int]]

    def covers(self, *, slug: str, pr_id: int, host_kind: str = "") -> bool:
        """Evidence that names no host cannot prove it is a different PR, so it counts as voided."""
        wanted = slug.strip().casefold()
        return any(
            voided_slug == wanted and voided_id == pr_id and (not host_kind or voided_host == host_kind)
            for voided_host, voided_slug, voided_id in self.refs
        )


class MergeVoidQuerySet(models.QuerySet):
    def for_pr(self, *, slug: str, pr_id: int, host_kind: str = "") -> "MergeVoidQuerySet":
        voids = self.filter(slug__iexact=slug.strip(), pr_id=pr_id)
        return voids.filter(host_kind=host_kind) if host_kind else voids

    def voided_refs(self) -> VoidedRefs:
        rows = self.values_list("host_kind", "slug", "pr_id")
        return VoidedRefs(frozenset((host, slug.casefold(), pr_id) for host, slug, pr_id in rows))

    def record(self, fields: MergeVoidFields, *, tickets: "Iterable[Ticket]") -> "MergeVoid":
        void = self.create(**fields)
        void.tickets.add(*tickets)
        return void


class MergeVoid(models.Model):
    host_kind = models.CharField(max_length=16)
    slug = models.CharField(max_length=255)
    pr_id = models.PositiveIntegerField()
    pr_url = models.URLField(max_length=500)
    merged_sha = models.CharField(max_length=64)
    content_head = models.CharField(max_length=64)
    merge_base = models.CharField(max_length=64)
    default_branch = models.CharField(max_length=255)
    default_tip = models.CharField(max_length=64)
    changed_paths = models.JSONField(default=list)
    evidence = models.JSONField(default=dict)
    reason = models.TextField()
    actor = models.CharField(max_length=255)
    voided_at = models.DateTimeField(default=timezone.now)
    tickets = models.ManyToManyField("core.Ticket", related_name="merge_voids", blank=True)

    objects = MergeVoidQuerySet.as_manager()

    class Meta:
        db_table = "teatree_merge_void"
        ordering: ClassVar = ["-voided_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                models.F("host_kind"), Lower("slug"), models.F("pr_id"), name="unique_merge_void_per_pr"
            ),
            models.CheckConstraint(condition=~models.Q(reason=""), name="merge_void_reason_required"),
            models.CheckConstraint(condition=~models.Q(actor=""), name="merge_void_actor_required"),
        ]

    def __str__(self) -> str:
        return f"merge-void<{self.pr_url}@{self.merged_sha[:8]}>"

    def describe(self) -> str:
        changed = len(self.changed_paths)
        return (
            f"merge voided: {self.pr_url} @{self.merged_sha[:8]} by {self.actor} at "
            f"{self.voided_at.isoformat(timespec='seconds')}: {self.reason} "
            f"({changed}/{changed} paths absent on {self.default_branch})"
        )
