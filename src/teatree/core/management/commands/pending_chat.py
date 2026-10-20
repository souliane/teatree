"""``t3 <overlay> pending-chat`` — manage the inbound Slack-DM queue (#1063).

Subcommands. ``t3 <overlay> pending-chat list`` prints rows from the
last hour (or all pending if ``--all``), oldest first. Each row carries the
reactive Slack-answer loop's disposition — ``queued`` while the loop has not
replied, else the ``answer_kind`` it settled on (``ack`` / ``simple`` /
``delegated`` / ``question_reply``) — so a handled row can never be misread as
one still waiting for the loop.
``t3 <overlay> pending-chat mark-answered <slack_ts>`` stamps
``answered_at`` on the row(s) matching ``slack_ts``; the agent calls
this once per direct reply to a queued user question. The stamp keys on
``slack_ts`` alone, so it clears the question regardless of which overlay
recorded it (the concurrent
multi-overlay case, where the recording overlay and the answering
session routinely differ).

The ``mark-answered`` path is also reachable via ``notify_user``'s
``answering_slack_ts=`` kwarg or the ``answer-<anything>-<ts>``
idempotency-key convention (see :mod:`teatree.core.notify`).
"""

import json
import os
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import typer
from django.db import transaction
from django.utils import timezone
from django_typer.management import TyperCommand, command, initialize

from teatree.core.modelkit.relay import RelayRefusal
from teatree.core.models import PendingChatInjection
from teatree.core.session_identity import session_id_from_env
from teatree.hooks.slack_first_scanner import RELAY_RECEIPT_MARKER, RELAY_REFUSED_MARKER
from teatree.loops.timer_reconciler import wake_slack_answer

EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_DELIVERY_FAILED = 4


def _loop_flag(row: PendingChatInjection) -> str:
    """How the reactive Slack-answer loop left this row — its ``answer_kind``, or ``queued``.

    Always emitted, because the absence of a flag is not readable as a state.
    A row the loop had already :eyes:-reacted, stamped ``delegated`` and dispatched an
    ``answering`` lane for printed as ``#52 [question]`` — byte-identical to a row
    nothing had touched. Read as "still queued", it produced a false "the loop selects
    nothing" diagnosis against a loop that had done its whole job.
    """
    if row.loop_replied_at is None:
        return "queued"
    return row.answer_kind or "loop-replied"


def _format_row(row: PendingChatInjection) -> str:
    when = row.received_at.isoformat() if row.received_at is not None else "?"
    flags: list[str] = []
    if row.is_question:
        flags.append("question")
    flags.append(_loop_flag(row))
    if row.answered_at is not None:
        flags.append("answered")
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
    ) -> str:
        """Stamp ``answered_at = now`` on rows matching ``slack_ts``.

        The stamp keys on ``slack_ts`` alone — the unique idempotency key —
        so it clears the question regardless of which overlay recorded it (the concurrent
        multi-overlay case). Idempotent: zero rows is a successful no-op.
        Empty ``slack_ts`` is rejected.
        """
        if not slack_ts.strip():
            self.stderr.write("slack_ts must not be empty")
            raise SystemExit(2)
        stamped = PendingChatInjection.agent_answered_question(slack_ts)
        return f"stamped {stamped} row(s) as answered (ts={slack_ts})."

    @command()
    def relay(
        self,
        *,
        relay_id: Annotated[
            str, typer.Option("--relay-id", help="Idempotency key for this prompt; a retry reuses it.")
        ] = "",
        text: Annotated[str, typer.Option("--text", help="The owner's prompt, verbatim.")] = "",
        text_file: Annotated[str, typer.Option("--text-file", help="Read the prompt from this file instead.")] = "",
        session_id: Annotated[
            str, typer.Option("--session-id", help="The relaying session; defaults to the session env.")
        ] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Also print the result as one JSON object.")] = False,
    ) -> None:
        """Relay an owner prompt verbatim into the owner's factory Slack DM; exit 3 or 4: answer in the terminal."""
        if not relay_id.strip() or bool(text) == bool(text_file):
            self.stderr.write("give --relay-id and exactly one of --text or --text-file")
            raise SystemExit(EXIT_USAGE)
        prompt = Path(text_file).read_text(encoding="utf-8") if text_file else text
        result = PendingChatInjection.objects.relay_attended_ask(
            os.environ.get("T3_OVERLAY_NAME", ""),
            prompt,
            session_id or session_id_from_env() or "",
            relay_id=relay_id.strip(),
        )
        if isinstance(result, RelayRefusal):
            self._report(
                f"{RELAY_REFUSED_MARKER} {result.reason}",
                json_output=json_output,
                status="delivery_failed" if result.delivery_failed else "refused",
                reason=result.reason,
            )
            raise SystemExit(EXIT_DELIVERY_FAILED if result.delivery_failed else EXIT_REFUSED)
        if result.created:
            transaction.on_commit(wake_slack_answer.enqueue, robust=True)
        self._report(
            f"{RELAY_RECEIPT_MARKER} pending_chat={result.pk} ts={result.ts} permalink={result.permalink}",
            json_output=json_output,
            status="relayed",
            **asdict(result),
        )

    def _report(self, marker: str, *, json_output: bool, **fields: str | int | bool) -> None:
        self.stdout.write(marker)
        if json_output:
            self.stdout.write(json.dumps({**fields, "marker": marker}))

    @command()
    def relays(
        self,
        *,
        session: Annotated[
            str | None, typer.Option("--session", help="Only this session's relays; omit for every session's.")
        ] = None,
        json_output: Annotated[bool, typer.Option("--json", help="Print the open relays as JSON.")] = False,
    ) -> str:
        """List the relayed owner prompts the factory has not answered, or whose delegated work went wrong."""
        rows = [
            {
                "id": row.pk,
                "session": row.relayed_by_session,
                "channel": row.channel,
                "slack_ts": row.slack_ts,
                "state": "delegated" if row.answer_kind == PendingChatInjection.AnswerKind.DELEGATED else "unanswered",
                "text": " ".join(row.text.split())[:120],
            }
            for row in PendingChatInjection.objects.open_relays(session)
        ]
        if json_output:
            return json.dumps(rows)
        if not rows:
            return "no open relays."
        return "\n".join(f"#{row['id']} [{row['state']}] session={row['session'] or '-'} {row['text']}" for row in rows)
