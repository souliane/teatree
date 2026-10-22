"""Shared dashboard rows for the browser lane and the visual QA server."""

from django.contrib.auth import get_user_model

from e2e.dash.pom import SeededBoard
from teatree.core.models.loop import Loop
from teatree.core.models.ticket import Ticket
from tests.factories import PullRequestFactory, TicketFactory, TicketTransitionFactory


def seed_board() -> SeededBoard:
    get_user_model().objects.create_superuser(username="visual_qa", email="", password=None)
    board = SeededBoard(
        backlog=TicketFactory(state=Ticket.State.NOT_STARTED, short_description="triage the inbox"),
        building=TicketFactory(state=Ticket.State.WORK_STARTED, short_description="build the widget"),
        reviewing=TicketFactory(state=Ticket.State.REVIEW_REQUESTED, short_description="awaiting cold review"),
        landed=TicketFactory(state=Ticket.State.MERGED, short_description="landed the change"),
    )
    TicketTransitionFactory(ticket=board.reviewing, from_state=Ticket.State.WORK_STARTED, to_state=Ticket.State.CODED)
    PullRequestFactory(ticket=board.reviewing)
    Loop.objects.create(name="e2e_loop", script="teatree.loops.review", delay_seconds=60)
    return board
