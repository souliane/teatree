"""``manage.py loop_owner`` — pilot the session-scoped loop-owner claim (#1073).

Backs ``t3 loop claim/owner/release``. The chat-only user uses this to
hand the loop off when a foreign session has hijacked it: ``claim
--take-over`` evicts a live claimant so the hijacker's next ``t3 loop
tick`` SKIPs within one tick. ORM access is here (a management command,
not a plain typer command) per the project's "anything touching the ORM
is a management command" rule.

Non-zero exits use ``raise SystemExit(N)`` — this runs under Django's
``call_command``; ``typer.Exit`` is the wrong primitive on that path
(it stays correct in ``cli.loop`` itself).

#1107 — the ``claim`` no-session refusal below is now reachable far less
often: ``current_session_id()`` gained a loop-registry fallback (read the
``t3-loop-tick-owner`` record when both session-id env vars are absent),
so an agent-driven ``t3 loop claim`` (a Bash-tool subprocess that never
sees ``CLAUDE_SESSION_ID`` as an env var) resolves the owner from the
durable registry instead of hard-refusing. The refusal still fires only
when there is genuinely no resolvable session id anywhere.
"""

import json
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command


def _claim(slot: str, *, take_over: bool, json_output: bool, stdout_write) -> None:  # noqa: ANN001
    import os  # noqa: PLC0415

    from teatree.core.models import LoopLease  # noqa: PLC0415
    from teatree.loop.session_identity import current_session_id  # noqa: PLC0415

    session_id = current_session_id()
    if not session_id:
        msg = "refusing to claim loop ownership without a Claude session id — run inside a Claude Code session"
        if json_output:
            stdout_write(json.dumps({"ok": False, "error": msg}, indent=2))
        else:
            stdout_write(f"ERROR  {msg}")
        raise SystemExit(2)
    # #1604: record the durable session pid for the ``loop-owner`` slot only
    # so ``evict_stale_owner`` can distinguish a post-compaction same-process
    # self-reclaim from a genuinely foreign live lease. Other slots (e.g.
    # ``loop-slack-answer-owner``) are per-tick ephemeral and don't need it.
    owner_pid = os.getppid() if slot == "loop-owner" else None
    won, owner = LoopLease.objects.claim_ownership(
        slot, session_id=session_id, take_over=take_over, owner_pid=owner_pid
    )
    if json_output:
        stdout_write(json.dumps({"ok": won, "slot": slot, "owner_session": owner}, indent=2))
    elif won:
        stdout_write(f"OK    claimed loop slot {slot!r} for this session ({session_id}).")
    else:
        stdout_write(f"SKIP  loop slot {slot!r} held by session {owner} — pass --take-over to seize it.")


def _owner(slot: str, *, json_output: bool, stdout_write) -> None:  # noqa: ANN001
    from teatree.core.models import LoopLease  # noqa: PLC0415

    status = LoopLease.objects.ownership_status(slot)
    if json_output:
        stdout_write(
            json.dumps(
                {
                    "slot": slot,
                    "owner_session": status.owner_session,
                    "expires_at": status.expires_at.isoformat() if status.expires_at else "",
                    "is_live": status.is_live,
                },
                indent=2,
            )
        )
    elif status.is_live:
        stdout_write(f"OWNER {slot}: session {status.owner_session} (live until {status.expires_at.isoformat()}).")
    else:
        stdout_write(f"OWNER {slot}: unclaimed (no live owner).")


def _release(slot: str, *, json_output: bool, stdout_write) -> None:  # noqa: ANN001
    from teatree.core.models import LoopLease  # noqa: PLC0415
    from teatree.loop.session_identity import current_session_id  # noqa: PLC0415

    session_id = current_session_id()
    released = LoopLease.objects.release_ownership(slot, session_id=session_id)
    if json_output:
        stdout_write(json.dumps({"ok": released, "slot": slot}, indent=2))
    elif released:
        stdout_write(f"OK    released loop slot {slot!r} (was held by this session).")
    else:
        stdout_write(f"NOOP  this session does not hold loop slot {slot!r} — nothing released.")


class Command(TyperCommand):
    help = "Claim, inspect, or release the session-scoped loop-owner slot (#1073)."

    @command(name="claim")
    def claim(
        self,
        *,
        take_over: Annotated[
            bool,
            typer.Option("--take-over", help="Evict a live claimant (the chat-only user's hand-off)."),
        ] = False,
        slot: Annotated[
            str,
            typer.Option("--slot", help="Loop-owner slot name (default: loop-owner)."),
        ] = "loop-owner",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Claim the loop-owner slot for this session."""
        _claim(slot, take_over=take_over, json_output=json_output, stdout_write=self.stdout.write)

    @command(name="owner")
    def owner(
        self,
        *,
        slot: Annotated[str, typer.Option("--slot", help="Loop-owner slot name (default: loop-owner).")] = "loop-owner",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Show which session owns the loop-owner slot."""
        _owner(slot, json_output=json_output, stdout_write=self.stdout.write)

    @command(name="release")
    def release(
        self,
        *,
        slot: Annotated[str, typer.Option("--slot", help="Loop-owner slot name (default: loop-owner).")] = "loop-owner",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Release this session's loop-owner claim (CAS — non-owner is a no-op)."""
        _release(slot, json_output=json_output, stdout_write=self.stdout.write)
