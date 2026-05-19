"""``t3 <overlay> pending-chat`` — manage the inbound Slack-DM queue (#1063).

Subcommands. ``t3 <overlay> pending-chat list`` prints rows from the
last hour (or all pending if ``--all``), oldest first.
``t3 <overlay> pending-chat mark-answered <slack_ts> [--overlay X]``
stamps ``answered_at`` on the matching row(s); the agent calls this
once per direct reply to a queued user question, so the Stop hook
stops nagging on already-answered rows.

The ``mark-answered`` path is also reachable via ``notify_user``'s
``answering_slack_ts=`` kwarg or the ``answer-<anything>-<ts>``
idempotency-key convention (see :mod:`teatree.core.notify`).
"""

from datetime import timedelta
from typing import Annotated

import typer
from django.utils import timezone
from django_typer.management import TyperCommand, command, initialize

from teatree.core.models import PendingChatInjection


def _format_row(row: PendingChatInjection) -> str:
    when = row.received_at.isoformat() if row.received_at is not None else "?"
    flags: list[str] = []
    if row.is_question:
        flags.append("question")
    if row.consumed_at is not None:
        flags.append("consumed")
    if row.answered_at is not None:
        flags.append("answered")
    if not flags:
        flags.append("pending")
    snippet = row.text.strip().replace("\n", " ")[:120]
    return f"  #{row.pk} [{'/'.join(flags)}] ts={row.slack_ts} {when}\n     {snippet}"


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 <overlay> pending-chat`` group root."""

    @command(name="list")
    def list_rows(
        self,
        *,
        all_rows: Annotated[
            bool,
            typer.Option("--all/--recent", help="Include rows older than 1h; default is last hour only."),
        ] = False,
    ) -> str:
        """List inbound Slack-DM rows; the last hour by default."""
        qs = PendingChatInjection.objects.all().order_by("received_at")
        if not all_rows:
            cutoff = timezone.now() - timedelta(hours=1)
            qs = qs.filter(received_at__gte=cutoff)
        rows = list(qs)
        if not rows:
            return "no inbound rows."
        lines = [f"{len(rows)} inbound row(s):"]
        lines.extend(_format_row(row) for row in rows)
        return "\n".join(lines)

    @command(name="mark-answered")
    def mark_answered(
        self,
        slack_ts: Annotated[str, typer.Argument(help="The Slack ts of the question being answered.")],
        overlay: Annotated[
            str,
            typer.Option("--overlay", help="Scope the stamp to one overlay (default: empty / v1 single-overlay)."),
        ] = "",
    ) -> str:
        """Stamp ``answered_at = now`` on rows matching ``(overlay, slack_ts)``.

        Idempotent: zero rows is a successful no-op (the second call
        sees the row already stamped). Empty ``slack_ts`` is rejected.
        """
        if not slack_ts.strip():
            self.stderr.write("slack_ts must not be empty")
            raise SystemExit(2)
        stamped = PendingChatInjection.agent_answered_question(slack_ts, overlay=overlay)
        return f"stamped {stamped} row(s) as answered (ts={slack_ts}, overlay={overlay!r})."
