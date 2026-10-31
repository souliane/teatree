"""A merged pull request that links its ticket's issue without closing it and declares a remainder.

The keystone post hook asks :func:`read_merged_pr` what the merged body says, then
:func:`hold_for_remainder` keeps the ticket out of ``merged`` and queues the planning
for what the body left open. A link phrase alone never holds a ticket: most pull
requests say ``Part of #N``, and only a remainder the body itself declares counts.
"""

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from django.db import transaction
from django_fsm import TransitionNotAllowed

from teatree.core.issue_writes.issue_candidate_selection import description_of
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.merge.host_kind import resolve_host_kind
from teatree.core.models import MergeClear, PullRequest, Task, Ticket
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.ticket_ledger import stamp_reopened_over
from teatree.utils.close_keywords import CLOSING_REFERENCE_RE
from teatree.utils.pr_ref import PrRef
from teatree.utils.url_slug import repo_namespaced_key

if TYPE_CHECKING:
    from collections.abc import Iterator


logger = logging.getLogger(__name__)

_ISSUE_REF = r"(?P<ref>(?:[\w./-]+)?#\d+|https?://\S+/issues/\d+)"
_PARTIAL = re.compile(rf"\b(?:part\s+of|relates?\s+to|related\s+to)(?::\s*|\s+){_ISSUE_REF}", re.IGNORECASE)

# The remainder markers — every phrase that declares work left over, in one place.
_STATUS_UNMET = ("unmet", "not met", "partly met", "partially met")
_REMAINDER_LINE = re.compile(
    r"^\W*(?:unmet|remaining after this (?:pr|pull request))\W*:\s*(?P<item>\S.*)$", re.IGNORECASE
)
_STAYS_ON = re.compile(rf"\b(?:stays?|remains?)\s+on\s+{_ISSUE_REF}", re.IGNORECASE)
_DECLARES_OPEN = re.compile(
    r"\b(?:does\s*n[o']t|will\s*n[o']t|won't)\s+close\b"
    r"|\b(?:issue|ticket)\s+(?:remains|stays)\s+open\b"
    r"|\b(?:keeps?|leaves?)\s+(?:the\s+)?(?:issue|ticket)\s+open\b",
    re.IGNORECASE,
)
_NOTHING_LEFT = frozenset({"none", "n/a", "nothing", "no", "nil"})

_ACCEPTANCE_HEADING = re.compile(r"^#{1,6}\s*(?:acceptance|definition of done|done when)\b", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s")
_UNCHECKED_BOX = re.compile(r"^\s*[-*]\s*\[ \]\s*(?P<item>\S.*)$")

_ITEM_CAP = 300
_REWORK_FROM = frozenset({Ticket.State.CODED, Ticket.State.TESTED, Ticket.State.SELF_REVIEWED})
_REOPEN_FROM = frozenset({Ticket.State.PR_OPENED, Ticket.State.REVIEW_REQUESTED})


class PrLink(StrEnum):
    NONE = "none"
    PARTIAL = "partial"
    CLOSING = "closing"


def _plain(text: str) -> str:
    return re.sub(r"[\s*_`]+", " ", text).strip().casefold()


def capped_remainder_item(item: str) -> str:
    return item[:_ITEM_CAP].strip()


def _says_nothing_left(item: str) -> bool:
    words = re.sub(r"[^\w/ ]+", " ", _plain(item)).split()
    return not words or words[0] in _NOTHING_LEFT


def _names_ticket(ref: str, ticket: Ticket) -> bool:
    if ref.lower().startswith("http"):
        return bool(ticket.repo_namespaced_key) and repo_namespaced_key(ref) == ticket.repo_namespaced_key
    repo, _, number = ref.rpartition("#")
    if number != ticket.issue_number:
        return False
    own_repo = ticket.repo_namespaced_key.rpartition("#")[0]
    return not repo or not own_repo or repo.casefold() == own_repo.casefold()


def _links_ticket(pattern: re.Pattern[str], body: str, ticket: Ticket) -> bool:
    return any(_names_ticket(match.group("ref"), ticket) for match in pattern.finditer(body))


def _status_table_items(lines: list[str]) -> "Iterator[str]":
    status_column = -1
    for line in lines:
        if not line.lstrip().startswith("|"):
            status_column = -1
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if status_column < 0:
            status_column = next((i for i, cell in enumerate(cells) if _plain(cell) == "status"), -1)
        elif status_column < len(cells) and _plain(cells[status_column]).startswith(_STATUS_UNMET):
            yield cells[0]


def _marker_line_items(lines: list[str], ticket: Ticket) -> "Iterator[str]":
    for raw in lines:
        line = raw.strip().lstrip("-*• ").strip()
        if not line or raw.lstrip().startswith("|"):
            continue
        if (match := _REMAINDER_LINE.match(line)) and not _says_nothing_left(match.group("item")):
            yield match.group("item")
        elif _DECLARES_OPEN.search(line) or any(
            _names_ticket(m.group("ref"), ticket) for m in _STAYS_ON.finditer(line)
        ):
            yield line


def _unchecked_acceptance_boxes(issue_body: str) -> "Iterator[str]":
    in_acceptance = False
    for line in issue_body.splitlines():
        if _HEADING.match(line):
            in_acceptance = bool(_ACCEPTANCE_HEADING.match(line))
        elif in_acceptance and (box := _UNCHECKED_BOX.match(line)):
            yield box.group("item")


@dataclass(frozen=True, slots=True)
class MergedPrReading:
    link: PrLink = PrLink.NONE
    unmet: tuple[str, ...] = ()

    @property
    def leaves_remainder(self) -> bool:
        return self.link is PrLink.PARTIAL and bool(self.unmet)

    @classmethod
    def parse(cls, body: str, ticket: Ticket) -> "MergedPrReading":
        if _links_ticket(CLOSING_REFERENCE_RE, body, ticket):
            return cls(PrLink.CLOSING)
        if not _links_ticket(_PARTIAL, body, ticket):
            return cls()
        lines = body.splitlines()
        declared = (*_status_table_items(lines), *_marker_line_items(lines, ticket))
        return cls(PrLink.PARTIAL, tuple(dict.fromkeys(capped_remainder_item(item) for item in declared)))

    def with_unclaimed_issue_boxes(self, issue_body: str, pr_body: str) -> "MergedPrReading":
        claimed = _plain(pr_body)
        boxes = (box for box in _unchecked_acceptance_boxes(issue_body) if _plain(box) not in claimed)
        return MergedPrReading(self.link, tuple(dict.fromkeys(capped_remainder_item(box) for box in boxes)))


def read_merged_pr(clear: MergeClear, repo_slug: str) -> MergedPrReading:
    """What the merged body says about its ticket's issue; a forge that does not answer reads as ``NONE``."""
    slug = repo_slug or clear.slug
    ticket = clear.ticket or PullRequest.objects.owning_ticket(slug=slug, pr_id=clear.pr_id)
    if ticket is None or not ticket.issue_number:
        return MergedPrReading()
    try:
        query = CodeHostQuery.for_ref(
            PrRef(slug=slug, pr_id=clear.pr_id, host_kind=resolve_host_kind(clear, repo_slug=slug))
        )
        message = query.pr_message()
        if message is None:
            logger.warning("partial merge: %s#%s body unreadable; treating the merge as complete", slug, clear.pr_id)
            return MergedPrReading()
        reading = MergedPrReading.parse(message.body, ticket)
        if reading.link is PrLink.PARTIAL and not reading.unmet:
            return reading.with_unclaimed_issue_boxes(
                description_of(query.backend.get_issue(ticket.issue_url)), message.body
            )
    except Exception:
        logger.exception("partial merge: could not read %s#%s; treating the merge as complete", slug, clear.pr_id)
        return MergedPrReading()
    return reading


def _branches_bound_to_merged_prs(ticket: Ticket) -> list[tuple[str, str]]:
    """The ``pr_url_by_branch`` entries ship would adopt as if the merged pull request were still the branch's."""
    merged_urls = set(
        PullRequest.objects.filter(ticket=ticket, state=PullRequest.State.MERGED).values_list("url", flat=True)
    )
    bound = (ticket.extra or {}).get("pr_url_by_branch")
    bound = bound if isinstance(bound, dict) else {}
    return [("pr_url_by_branch", branch) for branch, url in bound.items() if url in merged_urls]


def _remainder_listing(reading: MergedPrReading) -> str:
    return f"Open after that merge ({len(reading.unmet)}):\n" + "\n".join(f"- {item}" for item in reading.unmet)


def _can_hold_locked(ticket: Ticket, listing: str, *, settled: bool) -> bool:
    holdable = ticket.state in (
        Ticket.merged_states() if settled else _REOPEN_FROM | _REWORK_FROM | Ticket.EARLY_STATES
    )
    if not holdable or ticket.role != Ticket.Role.AUTHOR:
        return False
    if settled and ticket.tasks.filter(status__in=Task.Status.active()).exists():
        return False
    if ticket.tasks.filter(phase="planning", execution_reason__endswith=listing).exists():
        logger.info("partial merge: ticket %s already planned this remainder; not reopening again", ticket.pk)
        return False
    return True


def preview_remainder_hold(ticket: Ticket, reading: MergedPrReading) -> str | None:
    listing = _remainder_listing(reading)
    with transaction.atomic():
        locked = Ticket.objects.select_for_update().get(pk=ticket.pk)
        return locked.state if _can_hold_locked(locked, listing, settled=True) else None


def hold_for_remainder(
    ticket: Ticket, reading: MergedPrReading, *, merged_pr: str, settled: bool = False
) -> "Task | None":
    """Queue one planning task for a merged PR's remainder.

    Planning completes only from an early state, so a shipped or coded ticket goes back to
    ``work_started`` through the transition it already has. The stamp keeps the board's
    per-tick reconcile from merging the same pull request again.
    """
    listing = _remainder_listing(reading)
    intent = (
        f"{merged_pr} merged without closing the issue. Start the remainder on a fresh branch from the default "
        f"branch; the merged one has landed.\n{listing}"
    )
    try:
        with transaction.atomic():
            if settled:
                ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
            if not _can_hold_locked(ticket, listing, settled=settled):
                return None
            if settled or ticket.state in _REOPEN_FROM:
                ticket.reopen()
            elif ticket.state in _REWORK_FROM:
                ticket.rework()
            stamp_reopened_over(ticket)
            ticket.save()
            ticket.merge_extra(pop_keys=_branches_bound_to_merged_prs(ticket))
            task = ticket.begin_planning(intent=intent)
    except (InvalidTransitionError, TransitionNotAllowed):
        logger.warning("partial merge: ticket %s could not be held; treating the merge as complete", ticket.pk)
        ticket.refresh_from_db()
        return None
    ticket.refresh_from_db()
    return task
