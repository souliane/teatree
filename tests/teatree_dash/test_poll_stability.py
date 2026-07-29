"""A polled panel must not re-enter itself, and a morphed card must keep its identity.

The board polls every 4s and morphs the result over itself. On the deployed box the
board took 6.4s to render, so each poll was still in flight when the next fired:
requests overlapped, the column tree re-morphed continuously, and a click landed on
an element mid-replacement — the operator saw a card that simply did not open, with
no console error and no failed request to explain it.

Both halves are markup contracts, so both are asserted here rather than in a browser:
``hx-sync`` is what stops a poll overlapping itself, and a stable ``id`` is what lets
idiomorph morph a card in place instead of replacing the node under the cursor.
"""

# test-path: cross-cutting — scans EVERY dash template for the poll contract, seeding core models

import re
from pathlib import Path

from django.test import TestCase
from django.urls import reverse

from teatree.core.models.ticket import Ticket
from tests.factories import TicketFactory

_TEMPLATES = Path(__file__).resolve().parents[2] / "src/teatree/dash/templates"
_POLLING_ELEMENT = re.compile(r"<[^>]*hx-trigger=\"every [^\"]+\"[^>]*>", re.DOTALL)


def _polling_elements() -> list[tuple[str, str]]:
    return [
        (str(path.relative_to(_TEMPLATES)), match.group(0))
        for path in sorted(_TEMPLATES.rglob("*.html"))
        for match in _POLLING_ELEMENT.finditer(path.read_text(encoding="utf-8"))
    ]


class PollsCannotOverlapTestCase(TestCase):
    def test_there_is_at_least_one_polling_element_to_check(self) -> None:
        assert _polling_elements(), 'the scan found no `hx-trigger="every …"` element — it cannot prove anything'

    def test_every_polling_element_serialises_its_own_requests(self) -> None:
        offenders = [where for where, element in _polling_elements() if "hx-sync=" not in element]
        assert not offenders, (
            "these polled elements can start a request while their previous one is still "
            "in flight, so a slow page re-morphs under the cursor — add `hx-sync`: " + ", ".join(offenders)
        )


class MorphedCardsKeepTheirIdentityTestCase(TestCase):
    def test_every_board_card_carries_a_stable_id(self) -> None:
        tickets = [TicketFactory(state=Ticket.State.STARTED) for _ in range(3)]
        body = self.client.get(reverse("dash:board")).content.decode()
        for ticket in tickets:
            assert f'id="card-{ticket.pk}"' in body, f"card for ticket {ticket.pk} has no stable id to morph onto"
