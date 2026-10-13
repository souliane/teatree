"""The audited record that a forge merge never delivered its content to the default branch."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, TypedDict

from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from teatree.utils.pr_ref import PrRef

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
    """Every recorded void, read once: the only answer to "is this PR's merge voided"."""

    voids: tuple["MergeVoid", ...]

    def __bool__(self) -> bool:
        return bool(self.voids)

    def __contains__(self, ref: object) -> bool:
        return isinstance(ref, PrRef) and self.void_for(ref) is not None

    def void_for(self, ref: PrRef | None) -> "MergeVoid | None":
        return next((void for void in self.voids if ref is not None and void.ref.same_pr(ref)), None)


class MergeVoidQuerySet(models.QuerySet):
    def voided_refs(self) -> VoidedRefs:
        return VoidedRefs(tuple(self.all()))

    def restored_ticket_ids(self) -> set[int]:
        return set(self.exclude(tickets=None).values_list("tickets", flat=True))

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

    @property
    def ref(self) -> PrRef:
        return PrRef(slug=self.slug, pr_id=self.pr_id, host_kind=self.host_kind)

    def describe(self) -> str:
        return (
            f"merge voided: {self.pr_url} @{self.merged_sha[:8]} by {self.actor} at "
            f"{self.voided_at.isoformat(timespec='seconds')}: {self.reason} "
            f"({len(self.changed_paths)} changed paths absent on {self.default_branch})"
        )
