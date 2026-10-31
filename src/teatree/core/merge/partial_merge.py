"""A merged pull request that links its issue without closing it holds the ticket unless it delivers every item.

The keystone post hook, the board's rules A and B and the clear backfill all ask
:func:`blocks_settlement` whether the merge may move the ticket to ``merged``. It holds the
ticket out of ``merged`` and queues the planning for what the merge left open when the body
declares a remainder or does not echo an acceptance item of the issue as delivered, and it
refuses to settle while the body or the issue cannot be read.
"""

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from django.db import transaction
from django_fsm import TransitionNotAllowed

from teatree.core.gates.issue_coverage import (
    IssueRead,
    IssueUnreadableError,
    holds_on_issue,
    plain,
    read_issue,
    unechoed,
)
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.merge.host_kind import resolve_host_kind
from teatree.core.merge.last_merged_pr import LastMergedPr
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
_ISSUE_MENTION = re.compile(_ISSUE_REF)
_CODE_SPAN = re.compile(r"`[^`]*`")
_FENCE = ("```", "~~~")

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

_ITEM_CAP = 300
_REWORK_FROM = frozenset({Ticket.State.CODED, Ticket.State.TESTED, Ticket.State.SELF_REVIEWED})
_REOPEN_FROM = frozenset({Ticket.State.PR_OPENED, Ticket.State.REVIEW_REQUESTED})


class PrLink(StrEnum):
    NONE = "none"
    NON_CLOSING = "non_closing"
    CLOSING = "closing"


def capped_remainder_item(item: str) -> str:
    return item[:_ITEM_CAP].strip()


def _says_nothing_left(item: str) -> bool:
    words = re.sub(r"[^\w/ ]+", " ", plain(item)).split()
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


def _link_of(body: str, ticket: Ticket) -> PrLink:
    if _links_ticket(CLOSING_REFERENCE_RE, body, ticket):
        return PrLink.CLOSING
    return PrLink.NON_CLOSING if _links_ticket(_ISSUE_MENTION, body, ticket) else PrLink.NONE


def _prose_lines(lines: list[str]) -> list[str]:
    prose: list[str] = []
    fenced = False
    for raw in lines:
        stripped = raw.lstrip()
        if stripped.startswith(_FENCE):
            fenced = not fenced
        elif not fenced and not stripped.startswith(">"):
            prose.append(raw)
    return prose


def _status_table_items(lines: list[str]) -> "Iterator[tuple[str, str]]":
    status_column = -1
    for line in lines:
        if not line.lstrip().startswith("|"):
            status_column = -1
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if status_column < 0:
            status_column = next((i for i, cell in enumerate(cells) if plain(cell) == "status"), -1)
        elif status_column < len(cells) and plain(cells[status_column]).startswith(_STATUS_UNMET):
            yield line, cells[0]


def _marker_line_items(lines: list[str], ticket: Ticket) -> "Iterator[tuple[str, str]]":
    for raw in lines:
        line = raw.strip().lstrip("-*• ").strip()
        if not line or raw.lstrip().startswith("|"):
            continue
        prose = _CODE_SPAN.sub(lambda span: " " * len(span.group()), line)
        if (match := _REMAINDER_LINE.match(prose)) and not _says_nothing_left(match.group("item")):
            yield raw, line[match.start("item") : match.end("item")]
        elif _DECLARES_OPEN.search(prose) or any(
            _names_ticket(m.group("ref"), ticket) for m in _STAYS_ON.finditer(prose)
        ):
            yield raw, line


def _declared_remainder(body: str, ticket: Ticket) -> list[tuple[str, str]]:
    lines = _prose_lines(body.splitlines())
    return [*_status_table_items(lines), *_marker_line_items(lines, ticket)]


def _issue_or_failure(query: CodeHostQuery, ticket: Ticket) -> tuple[IssueRead | None, str]:
    try:
        return read_issue(query.backend, ticket.issue_url), ""
    except IssueUnreadableError:
        return None, "issue state unreadable"
    except Exception:  # core never imports backends (§17.6.2), so a 404 or forge error cannot be named
        logger.warning("partial merge: could not read %s", ticket.issue_url, exc_info=True)
        return None, "issue unreadable on the forge"


@dataclass(frozen=True, slots=True)
class MergedPrReading:
    link: PrLink = PrLink.NONE
    unmet: tuple[str, ...] = ()
    unreadable: str = ""
    issue: IssueRead | None = field(default=None, compare=False)

    @property
    def leaves_remainder(self) -> bool:
        return self.link is PrLink.NON_CLOSING and bool(self.unmet)

    @classmethod
    def parse(cls, body: str, ticket: Ticket, issue: IssueRead | None = None) -> "MergedPrReading":
        link = _link_of(body, ticket)
        if link is not PrLink.NON_CLOSING or (issue is not None and not issue.is_open):
            return cls(link, issue=issue)
        declared = _declared_remainder(body, ticket)
        unmet = [item for _, item in declared]
        if issue is not None:
            sources = {row for row, _ in declared}
            delivered = "\n".join(line for line in body.splitlines() if line not in sources)
            unmet += unechoed(issue.items_for(ticket), delivered)
        return cls(link, tuple(dict.fromkeys(capped_remainder_item(item) for item in unmet)), issue=issue)

    @classmethod
    def read(cls, query: CodeHostQuery, ticket: Ticket, *, with_issue: bool = False) -> "MergedPrReading":
        """What the merged body and its linked issue say is left; unreadable when either does not answer."""
        if not holds_on_issue(ticket):
            return cls()
        issue, failure = _issue_or_failure(query, ticket) if with_issue else (None, "")
        if failure:
            return cls(unreadable=failure)
        try:
            message = query.pr_message()
        except Exception:
            logger.warning("partial merge: could not read the body of %s", ticket.issue_url, exc_info=True)
            message = None
        if message is None:
            return cls(unreadable="pull request body unreadable", issue=issue)
        if issue is None and _link_of(message.body, ticket) is PrLink.NON_CLOSING:
            issue, failure = _issue_or_failure(query, ticket)
            if failure:
                return cls(unreadable=failure)
        return cls.parse(message.body, ticket, issue)


def _query_for(ref: PrRef) -> CodeHostQuery | None:
    try:
        return CodeHostQuery.for_ref(ref)
    except Exception:
        logger.exception("partial merge: no code host resolves for %s#%s", ref.slug, ref.pr_id)
        return None


def read_merged_pr(clear: MergeClear, repo_slug: str) -> MergedPrReading:
    """What the just-merged pull request says about its ticket's issue; unreadable when the forge does not answer."""
    slug = repo_slug or clear.slug
    ticket = clear.ticket or PullRequest.objects.owning_ticket(slug=slug, pr_id=clear.pr_id)
    if ticket is None or not holds_on_issue(ticket):
        return MergedPrReading()
    ref = PrRef(slug=slug, pr_id=clear.pr_id, host_kind=resolve_host_kind(clear, repo_slug=slug))
    query = _query_for(ref)
    return MergedPrReading.read(query, ticket) if query else MergedPrReading(unreadable="code host unresolvable")


def read_merged_pr_at(ticket: Ticket, latest: LastMergedPr | None) -> tuple[MergedPrReading, str]:
    """The reading of the *latest* merged pull request a caller looked up, with its URL for the audit line."""
    if latest is None:
        return MergedPrReading(unreadable="last merged pull request unknown"), ""
    pr, ref = latest
    query = _query_for(ref)
    reading = MergedPrReading.read(query, ticket) if query else MergedPrReading(unreadable="code host unresolvable")
    return reading, pr.url


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


def preview_remainder_hold(ticket: Ticket, reading: MergedPrReading, *, settled: bool = True) -> str | None:
    listing = _remainder_listing(reading)
    with transaction.atomic():
        locked = Ticket.objects.select_for_update().get(pk=ticket.pk)
        return locked.state if _can_hold_locked(locked, listing, settled=settled) else None


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
            locked = Ticket.objects.select_for_update().get(pk=ticket.pk)
            if not _can_hold_locked(locked, listing, settled=settled):
                return None
            if settled or locked.state in _REOPEN_FROM:
                locked.reopen()
            elif locked.state in _REWORK_FROM:
                locked.rework()
            stamp_reopened_over(locked)
            locked.save()
            locked.merge_extra(pop_keys=_branches_bound_to_merged_prs(locked))
            task = locked.begin_planning(intent=intent)
    except (InvalidTransitionError, TransitionNotAllowed):
        logger.warning("partial merge: ticket %s could not be held; treating the merge as complete", ticket.pk)
        return None
    finally:
        ticket.refresh_from_db()
    return task


def blocks_settlement(ticket: Ticket, reading: MergedPrReading, *, merged_pr: str, dry_run: bool = False) -> bool:
    """Whether the merge must not move the ticket to ``merged``; a remainder is held (planned) as a side effect."""
    if reading.unreadable:
        logger.warning(
            "partial merge: %s is unreadable (%s); ticket %s is not settled", merged_pr, reading.unreadable, ticket.pk
        )
        return True
    if not reading.leaves_remainder:
        return False
    if dry_run:
        return preview_remainder_hold(ticket, reading, settled=False) is not None
    return hold_for_remainder(ticket, reading, merged_pr=merged_pr) is not None
