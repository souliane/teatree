"""``ticket context show|add|edit`` — durable per-ticket knowledge store (#627, #2293).

Split out of ``ticket.py`` as a :class:`ContextCommands` mixin (the same MRO
split as ``RubricCommands`` / ``TicketShowCommands``) so the already-cap-bound
command god-module does not grow.
"""

from typing import TypedDict

import click
from django_typer.management import TyperCommand, group

from teatree.core.models import Ticket


class ContextResult(TypedDict, total=False):
    ticket_id: int
    repo_namespaced_key: str
    context: str


class ContextCommands(TyperCommand):
    """The ``ticket context`` command group, mounted via MRO inheritance.

    Lives here as a mixin the ``ticket`` command inherits (the same split as
    ``RubricCommands`` / ``TicketShowCommands``) so its LOC stays out of the
    already-cap-bound ``ticket.py``. django-typer collects ``@command``/
    ``@group`` methods from every ``TyperCommand`` base in the MRO, so the
    CLI surface is unchanged.
    """

    def _resolve_ticket_ref(self, ticket_id: str) -> Ticket:
        """Resolve *ticket_id* (pk / issue number / issue URL / repo-namespaced key) or abort.

        The durable context store is looked up by the same identifier set as
        ``pr create`` / ``lifecycle visit-phase`` (#694), including the
        collision-free repo-namespaced key (#2293) — so
        ``t3 <overlay> ticket context show owner/repo#42`` never risks
        landing on the wrong repo's issue #42.
        """
        try:
            return Ticket.objects.resolve(ticket_id)
        except Ticket.DoesNotExist:
            self.stderr.write(f"  Ticket {ticket_id!r} not found")
            raise SystemExit(1) from None

    @group(help="Durable per-ticket knowledge store (#627, repo-namespaced key #2293).")
    def context(self) -> None:
        """Group root — forces sub-commands to be addressed by name."""

    @context.command(name="show")
    def context_show(self, ticket_id: str) -> ContextResult:
        """Print the ticket's durable context store.

        ``ticket_id`` accepts the internal DB pk, the full issue URL, the
        bare issue number, or the repo-namespaced key (``owner/repo#42``) —
        the same identifier set as ``pr create`` (#694, #2293).
        """
        ticket = self._resolve_ticket_ref(ticket_id)
        self.stdout.write(ticket.context or "(empty)")
        return {
            "ticket_id": int(ticket.pk),
            "repo_namespaced_key": ticket.repo_namespaced_key,
            "context": ticket.context,
        }

    @context.command(name="add")
    def context_add(self, ticket_id: str, entry: str) -> ContextResult:
        """Append a timestamped ``<key>: <value>`` line to the context store.

        Append-only: parallel sessions never overwrite each other (open
        question 2). A blank entry is refused with a nonzero exit.
        ``ticket_id`` accepts the same identifier set as ``context show``.
        """
        ticket = self._resolve_ticket_ref(ticket_id)
        try:
            updated = ticket.append_context(entry)
        except ValueError as exc:
            self.stderr.write(f"  refused: {exc}")
            raise SystemExit(1) from exc
        self.stdout.write(f"  appended to ticket {ticket.pk} context")
        return {"ticket_id": int(ticket.pk), "repo_namespaced_key": ticket.repo_namespaced_key, "context": updated}

    @context.command(name="edit")
    def context_edit(self, ticket_id: str) -> ContextResult:
        """Open the full context store in ``$EDITOR`` and replace it.

        Unlike ``add``, ``edit`` is a full-field rewrite — for pruning stale
        entries or restructuring. An aborted edit (editor exits without
        saving) leaves the store untouched. ``ticket_id`` accepts the same
        identifier set as ``context show``.
        """
        ticket = self._resolve_ticket_ref(ticket_id)
        edited = click.edit(ticket.context)
        if edited is None:
            self.stdout.write(f"  edit aborted — ticket {ticket.pk} context unchanged")
            return {
                "ticket_id": int(ticket.pk),
                "repo_namespaced_key": ticket.repo_namespaced_key,
                "context": ticket.context,
            }
        ticket.context = edited
        ticket.save(update_fields=["context"])
        self.stdout.write(f"  ticket {ticket.pk} context replaced")
        return {"ticket_id": int(ticket.pk), "repo_namespaced_key": ticket.repo_namespaced_key, "context": edited}
