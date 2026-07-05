"""``manage.py loop_owner`` — pilot the session-scoped t3-master claim (#1073).

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
    """Re-render the statusline after a global ``t3-master`` ownership change.

    The foreign-hijack RED anchor reads the DB ``t3-master`` lease, but the
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


_DRIVERLESS_WARNING = (
    "WARN  loop slot {slot!r} claimed but DRIVERLESS — no tick driver is registered, so this loop "
    "will not tick.\n"
    "      Register one of:\n"
    "        - run `t3 worker` (or `config_setting set loop_runner_enabled true` then restart the "
    "session for the SessionStart resurrection) for the loop runner,\n"
    "        - keep the owning Claude session alive for the Stop self-pump,\n"
    "        - `t3 loop claim --slot {slot} --driver external` if a foreign scheduler drives it."
)


def _claim(command: TyperCommand, slot: str, *, take_over: bool, driver: str, json_output: bool) -> None:
    import os  # noqa: PLC0415

    from teatree.core.loop_lease_manager import T3_MASTER_SLOT, is_per_loop_owner_slot  # noqa: PLC0415
    from teatree.core.models import LoopDriver, LoopLease  # noqa: PLC0415 — deferred
    from teatree.loop.driver_detection import detect_driver  # noqa: PLC0415 — deferred
    from teatree.loop.session_identity import current_session_id, current_session_pid  # noqa: PLC0415

    stdout_write = command.stdout.write
    stderr_write = command.stderr.write
    if driver and driver not in LoopDriver.values:
        msg = f"invalid --driver {driver!r} — must be one of: {', '.join(LoopDriver.values)}"
        if json_output:
            stdout_write(json.dumps({"ok": False, "error": msg}, indent=2))
        else:
            stdout_write(f"ERROR  {msg}")
        raise SystemExit(2)
    session_id = current_session_id()
    if not session_id:
        msg = "refusing to claim loop ownership without a Claude session id — run inside a Claude Code session"
        if json_output:
            stdout_write(json.dumps({"ok": False, "error": msg}, indent=2))
        else:
            stdout_write(f"ERROR  {msg}")
        raise SystemExit(2)
    # Record the durable SESSION pid for the ``t3-master`` slot — and for a
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
    pid_anchored = slot == T3_MASTER_SLOT or is_per_loop_owner_slot(slot)
    owner_pid = (current_session_pid() or os.getppid()) if pid_anchored else None
    # Only the pid-anchored ownership layer (t3-master + loop:<name>) carries a
    # driver; an explicit ``--driver`` overrides detection (the only path to
    # ``external``, since a foreign scheduler is invisible to teatree).
    resolved_driver = (driver or detect_driver(session_id)) if pid_anchored else ""
    # take-over is an unconditional steal (evicts a live claimant); the plain
    # claim is the pid-anchored CAS that never evicts a live owner.
    claim = LoopLease.objects.take_over_ownership if take_over else LoopLease.objects.claim_ownership
    won, owner = claim(slot, session_id=session_id, owner_pid=owner_pid, driver=resolved_driver)
    if won and slot == T3_MASTER_SLOT:
        # The lease now names THIS session — clear any stale foreign-hijack
        # anchor the rendered statusline still carries from before the claim.
        _refresh_loop_owner_statusline()
    driverless = pid_anchored and not resolved_driver
    if json_output:
        stdout_write(
            json.dumps(
                {"ok": won, "slot": slot, "owner_session": owner, "driver": resolved_driver, "driverless": driverless},
                indent=2,
            )
        )
    elif won:
        stdout_write(f"OK    claimed loop slot {slot!r} for this session ({session_id}).")
    else:
        stdout_write(f"SKIP  loop slot {slot!r} held by session {owner} — pass --take-over to seize it.")
    # A successful pid-anchored claim with no driver is a silently-stalled loop —
    # warn loudly (stderr) even though the claim itself succeeded.
    if won and driverless:
        stderr_write(_DRIVERLESS_WARNING.format(slot=slot))


def _owner(slot: str, *, json_output: bool, stdout_write) -> None:  # noqa: ANN001
    from teatree.core.models import LoopLease  # noqa: PLC0415
    from teatree.loop.session_identity import current_session_id  # noqa: PLC0415

    # Surface THIS session's own id alongside the owner, so a session
    # always knows whether IT is the owner — not just who the owner is.
    you = current_session_id()
    status = LoopLease.objects.ownership_status(slot)
    driverless = status.is_live and not status.driver
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
                    "generation": status.generation,
                    "driver": status.driver,
                    "driverless": driverless,
                },
                indent=2,
            )
        )
        return
    stdout_write(f"you are: {you or '(no session id)'}")
    if status.is_live:
        stdout_write(f"OWNER {slot}: session {status.owner_session} (live until {status.expires_at.isoformat()}).")
        stdout_write(f"driver: {status.driver or 'DRIVERLESS'}")
    else:
        stdout_write(f"OWNER {slot}: unclaimed (no live owner).")


def _whoami(*, json_output: bool, stdout_write) -> None:  # noqa: ANN001
    """Print this Claude session's own id — the hand-off ``--to`` target."""
    from teatree.loop.driver_detection import detect_driver  # noqa: PLC0415 — deferred
    from teatree.loop.session_identity import current_session_id  # noqa: PLC0415

    session_id = current_session_id()
    driver = detect_driver(session_id)
    if json_output:
        stdout_write(json.dumps({"session_id": session_id, "driver": driver, "driverless": not driver}, indent=2))
        return
    if session_id:
        stdout_write(session_id)
        stdout_write(f"driver: {driver or 'DRIVERLESS'}")
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
    help = "Claim, inspect, or release the session-scoped t3-master slot (#1073)."

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
            typer.Option("--slot", help="t3-master slot name (default: t3-master)."),
        ] = "t3-master",
        driver: Annotated[
            str,
            typer.Option(
                "--driver",
                help="Explicit tick driver (self_pump/loop_runner/external); overrides detection. "
                "Use 'external' for a foreign scheduler.",
            ),
        ] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Claim the t3-master slot for this session."""
        _claim(self, slot, take_over=take_over, driver=driver, json_output=json_output)

    @command(name="owner")
    def owner(
        self,
        *,
        slot: Annotated[str, typer.Option("--slot", help="t3-master slot name (default: t3-master).")] = "t3-master",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Show which session owns the t3-master slot."""
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
        slot: Annotated[str, typer.Option("--slot", help="t3-master slot name (default: t3-master).")] = "t3-master",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Release this session's t3-master claim (CAS — non-owner is a no-op)."""
        _release(slot, json_output=json_output, stdout_write=self.stdout.write)
