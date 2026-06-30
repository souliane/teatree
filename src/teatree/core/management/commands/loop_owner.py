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


def _refresh_loop_owner_statusline() -> None:
    """Re-render the statusline after a global ``loop-owner`` ownership change.

    The foreign-hijack RED anchor reads the DB ``loop-owner`` lease, but the
    rendered zones file is rewritten only on a tick or an explicit re-render —
    so a ``claim``/``take-over`` that transfers the lease to THIS session left
    the stale pre-claim RED line (written by this session's own earlier foreign
    render) alive in the file, split-brained against the live per-session badge
    ``statusline.sh`` reads from the loop registry. Recomputing the anchor here
    against the just-written owner (now this session) clears it in the same
    command. Reuses the #2625 self-heal render seam. Fails open: a render error
    must never fail the claim it follows.
    """
    try:
        from teatree.loop.phases.render import rerender_statusline  # noqa: PLC0415

        rerender_statusline()
    except Exception:  # noqa: BLE001
        return


def _claim(slot: str, *, take_over: bool, json_output: bool, stdout_write) -> None:  # noqa: ANN001
    import os  # noqa: PLC0415

    from teatree.core.loop_lease_manager import GLOBAL_OWNER_SLOT, is_per_loop_owner_slot  # noqa: PLC0415
    from teatree.core.models import LoopLease  # noqa: PLC0415
    from teatree.loop.session_identity import current_session_id, current_session_pid  # noqa: PLC0415

    session_id = current_session_id()
    if not session_id:
        msg = "refusing to claim loop ownership without a Claude session id — run inside a Claude Code session"
        if json_output:
            stdout_write(json.dumps({"ok": False, "error": msg}, indent=2))
        else:
            stdout_write(f"ERROR  {msg}")
        raise SystemExit(2)
    # Record the durable SESSION pid for the ``loop-owner`` slot — and for a
    # per-loop ``loop:<name>`` owner (#1834), which is a persistent
    # session-scoped owner of the same kind — so ``evict_stale_owner`` / the
    # pid-anchored liveness check can tell a post-compaction same-process
    # self-reclaim from a genuinely foreign live lease, and a busy owner past
    # its TTL is never hijacked. It MUST be the long-lived session process,
    # not ``os.getppid()``: ``t3 loop claim`` runs in a Bash-tool shell torn
    # down seconds later, so anchoring on its pid stored a dead pid — the
    # take-over then "only held until the next fresh session" (the new
    # session saw a dead pid + lapsed TTL and stole the loop). The durable
    # pid comes from the loop-registry record the SessionStart hook wrote;
    # ``os.getppid()`` is the fallback only for a direct in-session call.
    # Other infra slots (e.g. ``loop-slack-answer-owner``) are per-tick
    # ephemeral and don't need it.
    pid_anchored = slot == "loop-owner" or is_per_loop_owner_slot(slot)
    owner_pid = (current_session_pid() or os.getppid()) if pid_anchored else None
    won, owner = LoopLease.objects.claim_ownership(
        slot, session_id=session_id, take_over=take_over, owner_pid=owner_pid
    )
    if won and slot == GLOBAL_OWNER_SLOT:
        # The lease now names THIS session — clear any stale foreign-hijack
        # anchor the rendered statusline still carries from before the claim.
        _refresh_loop_owner_statusline()
    if json_output:
        stdout_write(json.dumps({"ok": won, "slot": slot, "owner_session": owner}, indent=2))
    elif won:
        stdout_write(f"OK    claimed loop slot {slot!r} for this session ({session_id}).")
    else:
        stdout_write(f"SKIP  loop slot {slot!r} held by session {owner} — pass --take-over to seize it.")


def _owner(slot: str, *, json_output: bool, stdout_write) -> None:  # noqa: ANN001
    from teatree.core.models import LoopLease  # noqa: PLC0415
    from teatree.loop.session_identity import current_session_id  # noqa: PLC0415

    # Surface THIS session's own id alongside the owner, so a session
    # always knows whether IT is the owner — not just who the owner is.
    you = current_session_id()
    status = LoopLease.objects.ownership_status(slot)
    if json_output:
        stdout_write(
            json.dumps(
                {
                    "slot": slot,
                    "you": you,
                    "owner_session": status.owner_session,
                    "you_are_owner": bool(you) and status.is_live and you == status.owner_session,
                    "expires_at": status.expires_at.isoformat() if status.expires_at else "",
                    "is_live": status.is_live,
                },
                indent=2,
            )
        )
        return
    stdout_write(f"you are: {you or '(no session id)'}")
    if status.is_live:
        stdout_write(f"OWNER {slot}: session {status.owner_session} (live until {status.expires_at.isoformat()}).")
    else:
        stdout_write(f"OWNER {slot}: unclaimed (no live owner).")


def _whoami(*, json_output: bool, stdout_write) -> None:  # noqa: ANN001
    """Print this Claude session's own id — the hand-off ``--to`` target."""
    from teatree.loop.session_identity import current_session_id  # noqa: PLC0415

    session_id = current_session_id()
    if json_output:
        stdout_write(json.dumps({"session_id": session_id}, indent=2))
    elif session_id:
        stdout_write(session_id)
    else:
        stdout_write("(no Claude session id — not running inside a Claude Code session)")


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

    @command(name="whoami")
    def whoami(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Print this Claude session's own id."""
        _whoami(json_output=json_output, stdout_write=self.stdout.write)

    @command(name="release")
    def release(
        self,
        *,
        slot: Annotated[str, typer.Option("--slot", help="Loop-owner slot name (default: loop-owner).")] = "loop-owner",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Release this session's loop-owner claim (CAS — non-owner is a no-op)."""
        _release(slot, json_output=json_output, stdout_write=self.stdout.write)
