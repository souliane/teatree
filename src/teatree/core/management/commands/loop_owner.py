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
``t3-loop-tick-owner`` record when the session-id env vars are absent),
so an agent-driven ``t3 loop claim`` (a Bash-tool subprocess that never
sees the session id as an env var) resolves the owner from the
durable registry instead of hard-refusing. The refusal still fires only
when there is genuinely no resolvable session id anywhere.
"""

from typing import IO, Annotated, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit


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
        from teatree.loop.phases.render import rerender_statusline  # noqa: PLC0415 — deferred: lazy command import

        rerender_statusline()
    except Exception:  # noqa: BLE001 — best-effort side-effect; a failure degrades to no-op
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
    import os  # noqa: PLC0415 — deferred: loaded only when this command runs

    from teatree.core.loop_lease_manager import (  # noqa: PLC0415 — deferred: keeps command import light
        T3_MASTER_SLOT,
        is_per_loop_owner_slot,
    )
    from teatree.core.models import LoopDriver, LoopLease  # noqa: PLC0415 — deferred
    from teatree.loop.driver_detection import detect_driver  # noqa: PLC0415 — deferred
    from teatree.loop.session_identity import loop_principal  # noqa: PLC0415 — deferred: keeps command import light

    out = cast("IO[str]", command.stdout)
    err = cast("IO[str]", command.stderr)
    stderr_write = command.stderr.write
    if driver and driver not in LoopDriver.values:
        msg = f"invalid --driver {driver!r} — must be one of: {', '.join(LoopDriver.values)}"
        emit({"ok": False, "error": msg}, json_output=json_output, out=out, err=err, human=f"ERROR  {msg}")
        raise SystemExit(2)
    # The SAME seam ``_release`` and the per-loop tick resolve through (#3810) —
    # what a claim binds is exactly what the next release matches.
    session_id, principal_pid = loop_principal()
    if not session_id:
        msg = "refusing to claim loop ownership without a Claude session id — run inside a Claude Code session"
        emit({"ok": False, "error": msg}, json_output=json_output, out=out, err=err, human=f"ERROR  {msg}")
        raise SystemExit(2)
    # Record the durable SESSION pid for the ``t3-master`` slot — and for a
    # per-loop ``loop:<name>`` owner (#1834), which is a persistent
    # session-scoped owner of the same kind — so ``evict_stale_owner`` / the
    # pid-anchored liveness check can tell a post-compaction same-process
    # self-reclaim from a genuinely foreign live lease, and a busy owner past
    # its TTL is never hijacked. It MUST be the long-lived owning process,
    # not ``os.getppid()``: ``t3 loop claim`` runs in a Bash-tool shell torn
    # down seconds later, so anchoring on its pid stored a dead pid — the
    # take-over then "only held until the next fresh session" (the new
    # session saw a dead pid + lapsed TTL and stole the loop). It comes from
    # the runner's own principal, else the loop-registry record the
    # SessionStart hook wrote; ``os.getppid()`` is the fallback only for a
    # direct in-session call. Other infra slots (e.g.
    # ``loop-slack-answer-owner``) are per-tick ephemeral and don't need it.
    pid_anchored = slot == T3_MASTER_SLOT or is_per_loop_owner_slot(slot)
    owner_pid = (principal_pid or os.getppid()) if pid_anchored else None
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
    human = (
        f"OK    claimed loop slot {slot!r} for this session ({session_id})."
        if won
        else f"SKIP  loop slot {slot!r} held by session {owner} — pass --take-over to seize it."
    )
    emit(
        {"ok": won, "slot": slot, "owner_session": owner, "driver": resolved_driver, "driverless": driverless},
        json_output=json_output,
        out=out,
        err=err,
        human=human,
    )
    # A successful pid-anchored claim with no driver is a silently-stalled loop —
    # warn loudly (stderr) even though the claim itself succeeded.
    if won and driverless:
        stderr_write(_DRIVERLESS_WARNING.format(slot=slot))


def _owner(command: TyperCommand, slot: str, *, json_output: bool) -> None:
    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.loop.session_identity import loop_principal  # noqa: PLC0415 — deferred: keeps command import light

    # Surface THIS caller's own principal alongside the owner, so it always
    # knows whether IT is the owner — not just who the owner is. Resolved
    # through the SAME seam the claim binds (#3810): a diagnostic that reports a
    # different identity than the one the tick claims under is what made this
    # class of bug so hard to see.
    you, _ = loop_principal()
    status = LoopLease.objects.ownership_status(slot)
    driverless = status.is_live and not status.driver
    human_lines = [f"you are: {you or '(no session id)'}"]
    if status.is_live:
        human_lines.extend(
            (
                f"OWNER {slot}: session {status.owner_session} (live until {status.expires_at.isoformat()}).",
                f"driver: {status.driver or 'DRIVERLESS'}",
            )
        )
    else:
        human_lines.append(f"OWNER {slot}: unclaimed (no live owner).")
    emit(
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
        json_output=json_output,
        out=cast("IO[str]", command.stdout),
        err=cast("IO[str]", command.stderr),
        human="\n".join(human_lines),
    )


def _whoami(command: TyperCommand, *, json_output: bool) -> None:
    """Print this caller's own loop principal — the hand-off ``--to`` target.

    The same seam the claim binds (#3810), so ``whoami`` inside the loop runner
    reports the runner's principal rather than whichever Claude session the loop
    registry happens to name.
    """
    from teatree.loop.driver_detection import detect_driver  # noqa: PLC0415 — deferred
    from teatree.loop.session_identity import loop_principal  # noqa: PLC0415 — deferred: keeps command import light

    session_id, _ = loop_principal()
    driver = detect_driver(session_id)
    if session_id:
        human = f"{session_id}\ndriver: {driver or 'DRIVERLESS'}"
    else:
        human = "(no loop principal — not a loop runner, and not inside a Claude Code session)"
    emit(
        {"session_id": session_id, "driver": driver, "driverless": not driver},
        json_output=json_output,
        out=cast("IO[str]", command.stdout),
        err=cast("IO[str]", command.stderr),
        human=human,
    )


def _release(command: TyperCommand, slot: str, *, force: bool, json_output: bool) -> None:
    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.loop.session_identity import loop_principal  # noqa: PLC0415 — deferred: keeps command import light

    # The SAME seam ``_claim`` binds through (#3810), so a claim and the release
    # that follows it in the same process always agree on who "this" is.
    session_id, _ = loop_principal()
    status = LoopLease.objects.ownership_status(slot)
    released = LoopLease.objects.release_ownership(slot, session_id=session_id, force=force)
    if released:
        held_by = "any holder" if force and session_id != status.owner_session else "this session"
        human = f"OK    released loop slot {slot!r} (was held by {held_by})."
    else:
        # A NOOP must name WHO holds the slot and how to get it back. The silent
        # "nothing released" left an operator with a claimed-but-unreleasable
        # lease and no recovery path at all (#3810).
        holder = status.owner_session or "(nobody)"
        you = session_id or "(no resolvable identity)"
        human = (
            f"NOOP  loop slot {slot!r} is held by {holder}, not by you ({you}) — nothing released.\n"
            f"      Run `t3 loop release --slot {slot} --force` to release it regardless of holder."
        )
    emit(
        {"ok": released, "slot": slot, "owner_session": status.owner_session, "you": session_id, "forced": force},
        json_output=json_output,
        out=cast("IO[str]", command.stdout),
        err=cast("IO[str]", command.stderr),
        human=human,
    )


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
        _owner(self, slot, json_output=json_output)

    @command(name="whoami")
    def whoami(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Print this Claude session's own id."""
        _whoami(self, json_output=json_output)

    @command(name="release")
    def release(
        self,
        *,
        slot: Annotated[str, typer.Option("--slot", help="t3-master slot name (default: t3-master).")] = "t3-master",
        force: Annotated[
            bool,
            typer.Option("--force", help="Release regardless of holder — the operator's recovery path."),
        ] = False,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Release this session's t3-master claim (CAS — non-owner is a no-op unless --force)."""
        _release(self, slot, force=force, json_output=json_output)
