"""What an issue asks for: its acceptance items, and the tests for a pull request echoing or a rubric covering them.

One reading serves the hold on a non-closing merge, the rubric gate's merge refusal and the
pull request's closing keyword, so the three cannot drift apart on what counts as an item.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.backend_registry import get_backend_provider
from teatree.core.intake.factory_admission import payload_labels, resolve_umbrella_labels
from teatree.core.intake.umbrella import umbrella_reason
from teatree.core.issue_writes.issue_candidate_selection import description_of
from teatree.core.overlay_loader import get_overlay_for_ticket

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.core.models.ticket import Ticket
    from teatree.types import RawAPIDict

_ACCEPTANCE_HEADING = re.compile(r"^#{1,6}\s*(?:acceptance|definition of done|done when)\b", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[(?P<mark>[ xX])\]\s*)?(?P<text>\S.*)$")
_TICKED = frozenset({"x", "X"})
_OPEN_STATES = frozenset({"open", "opened"})
_KNOWN_STATES = _OPEN_STATES | {"closed"}


class IssueUnreadableError(RuntimeError):
    """The forge did not return an issue whose state and description can be trusted."""


def plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`]", "", text)).strip().casefold()


def holds_on_issue(ticket: "Ticket") -> bool:
    """Whether the ticket's delivery answers a forge issue: an author ticket whose URL names one."""
    return ticket.role == ticket.Role.AUTHOR and bool(ticket.repo_namespaced_key)


def acceptance_items(issue_body: str) -> tuple[str, ...]:
    items: dict[str, None] = {}
    in_acceptance = False
    for line in issue_body.splitlines():
        if _HEADING.match(line):
            in_acceptance = bool(_ACCEPTANCE_HEADING.match(line))
        elif in_acceptance and (item := _ITEM.match(line)) and item["mark"] not in _TICKED:
            items[item["text"].strip()] = None
    return tuple(items)


def _comparable(item: str) -> str:
    return plain(item).rstrip(" .,;:!?")


def unechoed(items: Iterable[str], delivered_text: str) -> tuple[str, ...]:
    delivered = plain(delivered_text)
    return tuple(item for item in items if _comparable(item) not in delivered)


def uncovered(items: Iterable[str], criteria: Iterable[str]) -> tuple[str, ...]:
    stated = [plain(criterion) for criterion in criteria]
    return tuple(item for item in items if not any(_comparable(item) in criterion for criterion in stated))


@dataclass(frozen=True, slots=True)
class IssueRead:
    payload: "RawAPIDict"
    is_open: bool

    @property
    def description(self) -> str:
        return description_of(self.payload)

    def umbrella(self, ticket: "Ticket") -> str:
        return umbrella_reason(
            body=self.description,
            labels=payload_labels(self.payload),
            umbrella_labels=resolve_umbrella_labels(ticket.overlay),
        )

    def items_for(self, ticket: "Ticket") -> tuple[str, ...]:
        """The items a delivery must answer; none once the issue is closed or when it is an umbrella."""
        return acceptance_items(self.description) if self.is_open and not self.umbrella(ticket) else ()


def read_issue(host: "CodeHostBackend", issue_url: str) -> IssueRead:
    payload = host.get_issue(issue_url)
    state = payload.get("state") if isinstance(payload, dict) else None
    if state not in _KNOWN_STATES:
        msg = f"issue {issue_url} came back without a known state"
        raise IssueUnreadableError(msg)
    return IssueRead(payload=payload, is_open=state in _OPEN_STATES)


def read_ticket_issue(ticket: "Ticket") -> IssueRead:
    host = get_backend_provider().get_code_host_for_url(get_overlay_for_ticket(ticket), ticket.issue_url)
    if host is None:
        msg = f"no code host resolves for {ticket.issue_url}"
        raise IssueUnreadableError(msg)
    return read_issue(host, ticket.issue_url)
