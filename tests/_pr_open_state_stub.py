"""Stub the forge's PR open-state read at the ``schedule_external_review`` boundary.

Every reviewer mint reads the reviewed PR's live state before it creates a task, so a
test that mints one names the state it expects. There is deliberately no autouse
default: a suite-wide OPEN would neutralise the gate it exists to pin.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import Task, Ticket
from teatree.core.models.ticket_external_review import schedule_external_review

_GATE = "teatree.core.review.pr_open_state"


class FakePrHost:
    def __init__(self, state: PrOpenState, *, raises: bool = False) -> None:
        self.state = state
        self.raises = raises
        self.calls: list[str] = []

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        self.calls.append(pr_url)
        if self.raises:
            msg = "forge unreachable"
            raise RuntimeError(msg)
        return self.state


class _Provider:
    def __init__(self, host: FakePrHost | None) -> None:
        self.host = host

    def get_code_host_for_url(self, overlay: object, url: str) -> FakePrHost | None:
        _ = (overlay, url)
        return self.host


@contextmanager
def pr_open_state(
    state: PrOpenState = PrOpenState.OPEN, *, raises: bool = False, no_host: bool = False
) -> Iterator[FakePrHost]:
    host = FakePrHost(state, raises=raises)
    with (
        patch(f"{_GATE}.get_backend_provider", return_value=_Provider(None if no_host else host)),
        patch(f"{_GATE}.get_overlay_for_ticket", return_value=object()),
    ):
        yield host


def mint_open_pr_review(ticket: Ticket) -> Task:
    with pr_open_state(PrOpenState.OPEN):
        result = schedule_external_review(ticket)
    assert isinstance(result, Task), result
    return result
