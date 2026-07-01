"""``t3 <overlay> handover`` — hand all current work to another session.

Reuses the PreCompact durable-state snapshot as the hand-off payload and
the ``t3-master`` slot for the default target. ``create`` persists a
:class:`~teatree.core.models.SessionHandover` row (source of truth) and
mirrors it to the XDG file; ``whoami`` prints this session's id;
``claim-on-start`` is the SessionStart-hook entry point that atomically
claims an unclaimed hand-off for a starting session and returns its payload.

ORM access is here (a management command, not a plain typer command) per
the project's "anything touching the ORM is a management command" rule.
"""

import json
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.handover import create_handover
from teatree.loop.session_identity import current_session_id


class Command(TyperCommand):
    help = "Hand all current work from this session to another session."

    @initialize()
    def init(self) -> None:
        """``t3 <overlay> handover`` group root."""

    @command()
    def create(
        self,
        *,
        to: Annotated[
            str,
            typer.Option("--to", help="Target session id. Omit to hand to the live loop owner, else park for next."),
        ] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Hand this session's full durable state to another session.

        No ``--to`` → the live ``t3-master`` slot holder; if none, parked
        for whichever session starts next. Always persists the
        :class:`SessionHandover` row AND mirrors it to the XDG file.
        """
        from_session = current_session_id()
        if not from_session:
            msg = "no Claude session id — run inside a Claude Code session to hand off its state"
            if json_output:
                self.stdout.write(json.dumps({"ok": False, "error": msg}, indent=2))
            else:
                self.stdout.write(f"ERROR  {msg}")
            raise SystemExit(2)

        handover, mirror = create_handover(from_session=from_session, explicit_to=to)
        recipient = handover.to_session or "next-session"
        if json_output:
            self.stdout.write(
                json.dumps(
                    {
                        "ok": True,
                        "from_session": handover.from_session,
                        "to_session": handover.to_session,
                        "parked_for_next": handover.is_for_next_session,
                        "mirror_path": str(mirror),
                    },
                    indent=2,
                )
            )
        else:
            self.stdout.write(f"OK    handed off to {recipient}; mirror written to {mirror}.")

    @command()
    def whoami(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Print this Claude session's own id (the hand-off ``--to`` target)."""
        session_id = current_session_id()
        if json_output:
            self.stdout.write(json.dumps({"session_id": session_id}, indent=2))
        elif session_id:
            self.stdout.write(session_id)
        else:
            self.stdout.write("(no Claude session id — not running inside a Claude Code session)")

    @command(name="claim-on-start")
    def claim_on_start(
        self,
        *,
        session: Annotated[str, typer.Option("--session", help="The starting session id claiming a hand-off.")] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = True,
    ) -> None:
        """Atomically claim an unclaimed hand-off for *session* and print its payload.

        The SessionStart hook calls this for a fresh / non-owner session: it
        claims a hand-off targeted AT the session (preferred) or parked for
        "next session", marks it claimed so it injects exactly once, and
        prints the payload. Empty payload when nothing is claimable.
        """
        from teatree.core.models import SessionHandover  # noqa: PLC0415

        session_id = session or current_session_id()
        claimed = SessionHandover.objects.claim_next(session_id) if session_id else None
        payload = claimed.payload if claimed else ""
        if json_output:
            self.stdout.write(
                json.dumps(
                    {
                        "claimed": claimed is not None,
                        "from_session": claimed.from_session if claimed else "",
                        "payload": payload,
                    },
                    indent=2,
                )
            )
        elif payload:
            self.stdout.write(payload)
