"""Read-only proof that every dream gap has exactly one owner, and no memory row is stranded.

Ownership mirrors :func:`~teatree.loops.dream.batch_promote.covering_ticket`: the umbrella
host's pending ledger; a live host's ``dream_gap_batch`` (all of it while unreconciled,
only what it delivered or rejected once reconciled). An IGNORED ticket owns nothing.

Every gap any batch ever carried is known, so a gap nobody owns — one a reconciled host
dropped included — is an orphan even when its memory is back in a held drain;
one owned twice is a duplicate; an IGNORED ticket still carrying a batch is a retired owner; a TICKETED memory row
on a promotion anchor whose gap nobody owns is stranded — the loss nothing would ever
re-offer. Scoped to a host, orphan checks cover gaps it owns or last held, while
duplicate and link checks cover that host's gaps and add its undispositioned ones.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from teatree.core.models import ConsolidatedMemory, Ticket
from teatree.core.models.dream_gap_ledger import (
    BATCH_KEY,
    batch_entries,
    pending_entries,
    umbrella_ticket,
    undispositioned_gap_keys,
)
from teatree.loops.dream.batch_promote import is_reconciled, settled_gap_keys
from teatree.loops.dream.umbrella_ledger import is_promotion_anchor


@dataclass(frozen=True, slots=True)
class GapCoverageReport:
    gaps: int
    orphan: list[str] = field(default_factory=list)
    duplicate: dict[str, list[int]] = field(default_factory=dict)
    retired_owner: list[int] = field(default_factory=list)
    stranded: list[str] = field(default_factory=list)
    undispositioned: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.orphan or self.duplicate or self.retired_owner or self.stranded or self.undispositioned)


@dataclass(slots=True)
class _Ownership:
    owners: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    cluster_of: dict[str, str] = field(default_factory=dict)

    def know(self, gap_key: str, cluster_key: str) -> None:
        self.owners.setdefault(gap_key, [])
        self.cluster_of.setdefault(gap_key, cluster_key or gap_key)

    def own(self, gap_key: str, cluster_key: str, owner_pk: int) -> None:
        self.know(gap_key, cluster_key)
        self.owners[gap_key].append(owner_pk)

    def orphans(self) -> list[str]:
        return sorted(key for key, pks in self.owners.items() if not pks)


def _owned_by_host(ticket: Ticket) -> list[dict[str, str]]:
    entries = batch_entries(ticket)
    if not is_reconciled(ticket):
        return entries
    settled = settled_gap_keys(ticket)
    return [entry for entry in entries if entry.get("gap_key") in settled]


def _ownership(umbrella_url: str) -> _Ownership:
    ownership = _Ownership()
    umbrella = umbrella_ticket(umbrella_url)
    if umbrella is not None:
        for entry in pending_entries(umbrella):
            ownership.own(str(entry.get("gap_key")), str(entry.get("cluster_key") or ""), umbrella.pk)
    for ticket in Ticket.objects.exclude(**{f"extra__{BATCH_KEY}__isnull": True}):
        for entry in batch_entries(ticket):
            ownership.know(str(entry.get("gap_key")), str(entry.get("cluster_key") or ""))
        if ticket.state == Ticket.State.IGNORED:
            continue
        for entry in _owned_by_host(ticket):
            ownership.own(str(entry.get("gap_key")), str(entry.get("cluster_key") or ""), ticket.pk)

    return ownership


def _stranded(ownership: _Ownership) -> list[str]:
    owned_clusters = {ownership.cluster_of[key] for key, pks in ownership.owners.items() if pks}
    rows = ConsolidatedMemory.objects.filter(disposition=ConsolidatedMemory.Disposition.TICKETED)
    return sorted(
        row.cluster_key for row in rows if is_promotion_anchor(row.ticket_url) and row.cluster_key not in owned_clusters
    )


def gap_coverage(*, umbrella_url: str, host: Ticket | None = None) -> GapCoverageReport:
    ownership = _ownership(umbrella_url)
    owners = ownership.owners
    if host is not None:
        scope = {key for key, pks in owners.items() if host.pk in pks}
        scope.update(str(entry.get("gap_key")) for entry in batch_entries(host))
        return GapCoverageReport(
            gaps=len(scope),
            orphan=[key for key in ownership.orphans() if key in scope],
            duplicate={key: pks for key, pks in sorted(owners.items()) if key in scope and len(pks) > 1},
            undispositioned=undispositioned_gap_keys(host),
        )
    retired = Ticket.objects.exclude(**{f"extra__{BATCH_KEY}__isnull": True}).filter(state=Ticket.State.IGNORED)
    return GapCoverageReport(
        gaps=len(owners),
        orphan=ownership.orphans(),
        duplicate={key: pks for key, pks in sorted(owners.items()) if len(pks) > 1},
        retired_owner=[ticket.pk for ticket in retired if batch_entries(ticket)],
        stranded=_stranded(ownership),
    )
