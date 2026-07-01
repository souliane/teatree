"""Tests for AI-generated ``Ticket.short_description`` rendering (#1156).

The pre-#1156 statusline rendered active tickets as ``#N`` + optional
cached tracker title (``ticket.extra["issue_title"]``). #1156 adds a
short, AI-generated, terminal-friendly summary on the ``Ticket`` model
itself; the active-tickets scanner prefers it over the tracker title.
"""

import pytest

from teatree.core.models.ticket import Ticket
from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering import zones_for


def _ticket_active_action(*, number: str, title: str, url: str = "") -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="anchors",
        detail=f"#{number} started",
        payload={
            "ticket_number": number,
            "state": "started",
            "issue_url": url or f"https://example.com/issues/{number}",
            "title": title,
            "overlay": "teatree",
        },
    )


def _render_blob(actions: list[DispatchAction]) -> str:
    zones = zones_for(actions, colorize=False)
    return "\n".join(
        item if isinstance(item, str) else item.text
        for zone in (zones.anchors, zones.action_needed, zones.in_flight)
        for item in zone
    )


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestShortDescriptionInScanner:
    """``ActiveTicketsScanner`` prefers ``ticket.short_description`` over ``issue_title``."""

    def test_ticket_line_renders_short_description_when_present(self) -> None:
        from teatree.loop.scanners.active_tickets import ActiveTicketsScanner  # noqa: PLC0415

        Ticket.objects.create(
            overlay="teatree",
            issue_url="https://example.com/issues/541",
            state=Ticket.State.STARTED,
            short_description="fix t3-master hijack",
            extra={"issue_title": "Loop owner ownership hijack — original tracker title"},
        )

        signals = ActiveTicketsScanner(overlay_name="teatree").scan()

        assert len(signals) == 1
        payload = signals[0].payload
        assert payload["title"] == "fix t3-master hijack", payload

    def test_falls_back_to_issue_title_when_short_description_blank(self) -> None:
        from teatree.loop.scanners.active_tickets import ActiveTicketsScanner  # noqa: PLC0415

        Ticket.objects.create(
            overlay="teatree",
            issue_url="https://example.com/issues/542",
            state=Ticket.State.STARTED,
            short_description="",
            extra={"issue_title": "Cached tracker title"},
        )

        signals = ActiveTicketsScanner(overlay_name="teatree").scan()

        assert len(signals) == 1
        assert signals[0].payload["title"] == "Cached tracker title", signals[0].payload


class TestShortDescriptionTruncation:
    """The canonical-item shape collapses the description to a terse topic."""

    def test_description_collapsed_to_terse_topic(self) -> None:
        long_title = "a" * 80
        blob = _render_blob([_ticket_active_action(number="100", title=long_title)])

        # Terse topic budget is 24 chars including the ellipsis.
        truncated = "a" * 23 + "…"
        assert truncated in blob, repr(blob)
        # The full 80-char title must NOT appear.
        assert long_title not in blob, repr(blob)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestEveryActiveTicketHasDescriptionChunk:
    """The scanner + renderer emit a description chunk for every active ticket."""

    def test_every_active_ticket_has_description_chunk(self) -> None:
        from teatree.loop.scanners.active_tickets import ActiveTicketsScanner  # noqa: PLC0415

        Ticket.objects.create(
            overlay="teatree",
            issue_url="https://example.com/issues/1001",
            state=Ticket.State.STARTED,
            short_description="multi-loop anchor",
        )
        Ticket.objects.create(
            overlay="teatree",
            issue_url="https://example.com/issues/1002",
            state=Ticket.State.STARTED,
            short_description="AI short descriptions",
        )

        signals = ActiveTicketsScanner(overlay_name="teatree").scan()

        titles = {s.payload["title"] for s in signals}
        assert titles == {"multi-loop anchor", "AI short descriptions"}, titles
