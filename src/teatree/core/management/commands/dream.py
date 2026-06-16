"""``manage.py dream`` — drive the idle-time memory-consolidation cron (#1933).

The command owns the cron mechanics around the (currently stubbed) distillation
engine (:func:`teatree.loops.dream.engine.run_consolidation`):

``run`` is the manual escape hatch: it runs a pass NOW regardless of cadence,
with an optional ``--since`` window bound and a ``--dry-run`` no-write mode.
``tick`` is the cron entry point: it runs a pass only when the ``dream``
cadence has elapsed (``MiniLoopMarker``), bumping the cadence ledger on a fire.

Both acquire the in-flight ``LoopLease`` (``dream-tick``) first so two passes
never overlap — the loser SKIPs (the #786 WS2 CAS, correct on the prod SQLite
backend). On a successful pass the ``DreamRunMarker`` is stamped succeeded
(clearing the staleness alarm); a failed pass bumps only the attempt timestamp,
so staleness keeps firing until a clean run lands.

Anything touching the ORM is a management command (AGENTS.md § "Deciding Where
a New Command Lives"); ``t3 dream`` is the thin Typer wrapper that delegates
here via ``call_command``.
"""

import datetime as dt
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command


class Command(TyperCommand):
    help = "Drive the idle-time memory-consolidation (dreaming) cron (#1933)."

    @command(name="run")
    def run(
        self,
        *,
        since: Annotated[
            str,
            typer.Option("--since", help="ISO-8601 lower bound for the replay window (default: engine lookback)."),
        ] = "",
        dry_run: Annotated[
            bool,
            typer.Option("--dry-run", help="Do everything except writing ConsolidatedMemory rows / the marker."),
        ] = False,
        propose_evals: Annotated[
            bool,
            typer.Option(
                "--propose-evals",
                help="Also derive inert eval candidates from grounded drift clusters (default OFF).",
            ),
        ] = False,
    ) -> None:
        """Run one consolidation pass NOW (manual escape hatch; ignores cadence)."""
        self._run_pass(since=_parse_since(since), dry_run=dry_run, enforce_cadence=False, propose_evals=propose_evals)

    @command(name="tick")
    def tick(self) -> None:
        """Run one consolidation pass IF the dream cadence has elapsed (cron entry).

        The eval-derivation seam is LIVE by default here (#2346): proposals are
        requested unless the ``T3_DREAM_PROPOSE_EVALS`` env / ``[loops.dream]
        propose_evals`` toml kill-switch disables it (see
        :func:`teatree.loops.dream.loop.propose_evals_enabled`).
        """
        from teatree.loops.dream.loop import propose_evals_enabled  # noqa: PLC0415

        self._run_pass(since=None, dry_run=False, enforce_cadence=True, propose_evals=propose_evals_enabled())

    def _run_pass(
        self, *, since: dt.datetime | None, dry_run: bool, enforce_cadence: bool, propose_evals: bool
    ) -> None:
        import os  # noqa: PLC0415

        from django.utils import timezone  # noqa: PLC0415

        from teatree.core.models import DreamRunMarker, LoopLease, MiniLoopMarker  # noqa: PLC0415
        from teatree.loops.config import LoopsConfig  # noqa: PLC0415
        from teatree.loops.dream.loop import DREAM_LEASE_NAME, DREAM_LEASE_SECONDS, MINI_LOOP  # noqa: PLC0415
        from teatree.loops.gating import elapsed_and_enabled  # noqa: PLC0415

        now = timezone.now()
        if enforce_cadence:
            decision = elapsed_and_enabled(LoopsConfig.load(), MINI_LOOP, now)
            if not decision.should_fire:
                self.stdout.write(f"SKIP  dream cadence not elapsed ({decision.skip_reason}).")
                return

        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire(DREAM_LEASE_NAME, owner=owner, lease_seconds=DREAM_LEASE_SECONDS):
            self.stdout.write("SKIP  another dream pass is already running — dream-tick lease held.")
            return

        enabled = propose_evals or _env_propose_evals()
        try:
            succeeded = self._consolidate_and_mark(since=since, dry_run=dry_run, now=now, propose_evals=enabled)
        finally:
            LoopLease.objects.release(DREAM_LEASE_NAME, owner=owner)

        if enforce_cadence and succeeded:
            MiniLoopMarker.objects.mark_fired(MINI_LOOP.name, now)

        # Re-read confirmation so a stamped success can be cited (resilience #7).
        if not dry_run:
            marker = DreamRunMarker.objects.filter(name=DreamRunMarker.NAME).first()
            stamped = marker.last_succeeded_at.isoformat() if marker and marker.last_succeeded_at else "none"
            self.stdout.write(f"      dream marker last_succeeded_at={stamped}")

    def _consolidate_and_mark(
        self, *, since: dt.datetime | None, dry_run: bool, now: dt.datetime, propose_evals: bool
    ) -> bool:
        from teatree.core.models import DreamRunMarker  # noqa: PLC0415
        from teatree.loops.dream import engine  # noqa: PLC0415
        from teatree.loops.dream.eval_proposer import EvalProposalRequest  # noqa: PLC0415

        request = EvalProposalRequest() if propose_evals else None
        try:
            result = engine.run_consolidation(overlay="", since=since, dry_run=dry_run, eval_proposals=request)
        except Exception as exc:  # noqa: BLE001
            if not dry_run:
                DreamRunMarker.objects.mark_attempted(now)
            self.stdout.write(f"FAIL  dream pass raised: {type(exc).__name__}: {exc}")
            return False

        evals = f"; {result.evals_proposed} eval candidate(s)" if result.evals_proposed else ""
        empty = (
            f"; WARN {result.empty_batches} batch(es) returned 0 clusters from non-empty input"
            if result.empty_batches
            else ""
        )
        if dry_run:
            self.stdout.write(
                f"DRY   dream pass — {result.clusters_recorded} cluster(s) would be recorded "
                f"from {result.members_replayed} member(s){evals}{empty}; no rows or marker written.",
            )
            return False

        if result.members_replayed == 0:
            DreamRunMarker.objects.mark_attempted(now)
            self.stdout.write("WARN  dream pass found 0 transcript members — marker NOT stamped succeeded.")
            return False

        DreamRunMarker.objects.mark_succeeded(now)
        promoted = self._promote_candidates(propose_evals=propose_evals, dry_run=dry_run)
        self.stdout.write(
            f"OK    dream pass — {result.clusters_recorded} cluster(s) recorded "
            f"from {result.members_replayed} member(s){evals}{empty}{promoted}.",
        )
        return True

    def _promote_candidates(self, *, propose_evals: bool, dry_run: bool) -> str:
        """Promote the freshly-derived candidates to live scenarios (guarded; never raises).

        Runs only when proposals were requested. Each candidate clears the
        NON-BYPASSABLE anti-vacuity guard (:func:`teatree.loops.dream.promote.guard_can_fail`)
        or is rejected (no file written). A promotion failure is reported in the
        summary line, never crashing the pass that already stamped success.
        """
        if not propose_evals:
            return ""
        try:
            from teatree.loops.dream import promote  # noqa: PLC0415
            from teatree.loops.dream.eval_proposer import _default_proposals_path  # noqa: PLC0415

            outcomes = promote.promote_proposals_file(_default_proposals_path(), dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            return f"; WARN eval promotion raised: {type(exc).__name__}: {exc}"
        promoted = sum(1 for o in outcomes if o.promoted)
        rejected = len(outcomes) - promoted
        if not outcomes:
            return ""
        return f"; promoted {promoted} live eval(s), rejected {rejected} vacuous candidate(s)"


def _env_propose_evals() -> bool:
    """Read the ``T3_DREAM_PROPOSE_EVALS`` opt-in env for the manual ``run`` path.

    The manual ``run`` enables the eval phase when ``--propose-evals`` is given OR
    this env is truthy (``1``/``true``/``yes``, case-insensitive). The cadence-
    driven ``tick`` path does NOT route through here — it resolves the seam (LIVE
    by default, env/toml kill-switch) via
    :func:`teatree.loops.dream.loop.propose_evals_enabled`.
    """
    import os  # noqa: PLC0415

    return os.environ.get("T3_DREAM_PROPOSE_EVALS", "").strip().lower() in {"1", "true", "yes"}


def _parse_since(raw: str) -> dt.datetime | None:
    """Parse the ``--since`` ISO-8601 string; empty → ``None`` (engine default).

    A naive value (``--since 2026-06-01``) is normalized to the current
    timezone so the ``USE_TZ`` engine never compares naive against aware. A
    malformed value raises ``CommandError`` instead of a raw traceback.
    """
    from django.core.management.base import CommandError  # noqa: PLC0415
    from django.utils import timezone  # noqa: PLC0415

    value = raw.strip()
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"--since is not a valid ISO-8601 datetime: {value!r}"
        raise CommandError(msg) from exc
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed)
    return parsed
