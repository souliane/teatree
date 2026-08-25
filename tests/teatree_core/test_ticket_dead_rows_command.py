"""`t3 <overlay> ticket dead-rows` — enumerate the rows no forge query can reach (#4527).

The doctor's dead-ticket WARN names only the first few and then points here, so this
command is the operator's whole remedy path: a WARN whose enumeration command does not
exist is a finding nobody can act on. Read-only by design — each row records a request
someone was told is tracked, so re-file-or-retire stays the operator's call.
"""

import json
from typing import cast

from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands._ticket_show import DeadRowResult
from teatree.core.models import Ticket
from teatree.utils.url_slug import slack_conversation_anchor


def _conversation_row(*, slack_ts: str, question: str) -> Ticket:
    """A lane row exactly as ``dispatch_work`` mints it — anchored, described, unfindable."""
    return Ticket.objects.create(
        issue_url=slack_conversation_anchor(channel="D-owner", slack_ts=slack_ts),
        overlay="t3-teatree",
        role=Ticket.Role.AUTHOR,
        short_description=question[:80],
        extra={"slack_answer": {"question": question}},
    )


def _rows() -> list[DeadRowResult]:
    return cast("list[DeadRowResult]", call_command("ticket", "dead-rows"))


class TicketDeadRowsTest(TestCase):
    def test_a_row_intake_cannot_find_is_listed_with_the_request_it_holds(self) -> None:
        _conversation_row(slack_ts="100.0", question="detect the open-PR bottleneck so it never recurs")

        rows = _rows()

        assert [row["held_text"] for row in rows] == ["detect the open-PR bottleneck so it never recurs"]

    def test_an_admissible_row_is_never_listed(self) -> None:
        Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/4527",
            overlay="t3-teatree",
            short_description="the shell-ticket fix",
        )

        assert _rows() == []

    def test_a_terminal_row_is_never_listed(self) -> None:
        row = _conversation_row(slack_ts="100.0", question="already dealt with")
        Ticket.objects.filter(pk=row.pk).update(state=Ticket.State.IGNORED)

        assert _rows() == []

    def test_json_emits_the_same_rows_the_human_form_lists(self) -> None:
        _conversation_row(slack_ts="100.0", question="make the token get this permission")

        rows = cast("list[DeadRowResult]", call_command("ticket", "dead-rows", "--json"))

        assert [row["ticket_id"] for row in rows] == [row["ticket_id"] for row in _rows()]
        assert json.dumps(rows), "the rows must be JSON-serialisable for the --json path"
