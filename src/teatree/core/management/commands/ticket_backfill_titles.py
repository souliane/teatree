"""``manage.py ticket_backfill_titles`` — populate ``extra['issue_title']``.

Tickets created before the loop stamped the forge issue title
(:meth:`Ticket.stamp_issue_title`) have a blank ``extra['issue_title']`` and a
blank ``short_description``, so the dashboard shows only a number. This one-shot
sweep fetches each such ticket's title from its forge (:func:`fetch_issue_title`:
the issue, or the pull/merge request an ``issue_url`` names) and stamps it, seeding
a human ``short_description`` immediately. Synthetic loop keys (``scanning-news://``, ``eval-local://`` …) are
not forge URLs and are skipped. Idempotent: a ticket that already carries
``extra['issue_title']`` is left untouched, so re-running is a no-op.
"""

from collections.abc import Callable

from django_typer.management import TyperCommand, command

from teatree.backends.errors import IssueNotFoundError
from teatree.core.backend_protocols import BackendResolutionError
from teatree.core.issue_title import MergeRequestTitleUnreadableError, fetch_issue_title
from teatree.core.models import Ticket
from teatree.utils.run import CommandFailedError

_HTTP_SCHEMES = ("http://", "https://")

#: Forge-layer failures for a single ticket that must not abort the whole sweep:
#: a 404, a forge CLI/API failure, an unresolvable backend, or a request without a title.
_FORGE_ERRORS = (IssueNotFoundError, CommandFailedError, BackendResolutionError, MergeRequestTitleUnreadableError)


def _needs_title(ticket: Ticket) -> bool:
    if not ticket.issue_url.startswith(_HTTP_SCHEMES):
        return False
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    return not extra.get("issue_title")


def _backfill_all(*, stdout_write: Callable[[str], object]) -> None:
    filled = 0
    skipped = 0
    for ticket in Ticket.objects.order_by("pk"):
        if not _needs_title(ticket):
            skipped += 1
            continue
        try:
            title = fetch_issue_title(ticket)
        except _FORGE_ERRORS as exc:
            stdout_write(f"WARN  ticket {ticket.pk}: forge fetch failed — {exc}")
            continue
        if not title:
            stdout_write(f"NOOP  ticket {ticket.pk}: no title resolved — skipped")
            continue
        written = ticket.stamp_issue_title(title)
        filled += 1
        stdout_write(f"OK    ticket {ticket.pk}: issue_title={title!r} wrote={written}")
    stdout_write(f"DONE  filled {filled} ticket(s), skipped {skipped}")


class Command(TyperCommand):
    help: str = "Backfill Ticket.extra['issue_title'] from the forge for existing tickets."

    @command(name="backfill")
    def backfill(self) -> None:
        """Fetch and stamp the forge issue title for every ticket missing one."""
        _backfill_all(stdout_write=self.stdout.write)


__all__ = ["Command"]
