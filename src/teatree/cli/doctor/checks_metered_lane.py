"""``check_*`` probes for the three metered-lane unknowns the governor cannot resolve (#4816).

The governor now reads a metered budget, but three facts it needs are the operator's to
supply or the deployment's to settle, and being silently wrong about any of them is what
the whole ticket is about. So each is REPORTED rather than guessed:

- the box rides the metered lane with no ceiling set, so that dimension is inert and the
    lane is as unbounded as it was when four runs walked into a provider cycle limit;
- attempts in the window recorded UNKNOWN usage, so the measured spend is a FLOOR and a
    reader told only the number would take a lower bound for a measurement;
- registered overlays disagree on ``agent_harness``, so the lane differs per overlay while
    ``agent_admission_verdict`` probes the box ONCE per drain — deliberately, since N
    per-overlay verdicts would each carry their own headroom and double-admit.

All three are advisory, like every sibling in :mod:`~teatree.cli.doctor.checks_admission_pressure`:
an unset budget is an operator's choice, not a fault, and reddening the run on one would
train the operator to ignore a red run. Every probe is crash-proof and degrades to silence.
"""

import typer


def check_metered_lane_ceiling() -> bool:
    """WARN when this box dispatches on the metered lane with no token ceiling set.

    The ceiling ships at ``0`` = UNSET because teatree cannot read a provider-side cycle
    limit and must not invent a budget — which leaves the metered dimension contributing
    nothing until an operator sets one. That is correct and it is also exactly the
    unbounded state, so it is said out loud rather than left to be discovered by a 403.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.core.dispatch_lane import configured_dispatch_lane  # noqa: PLC0415 — deferred: same
    from teatree.core.models.task_attempt import TaskAttempt  # noqa: PLC0415 — ORM import needs the app registry

    try:
        if configured_dispatch_lane() != TaskAttempt.Lane.METERED:
            return True
        ceiling = int(get_effective_settings().metered_token_ceiling)
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Metered-ceiling check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if ceiling > 0:
        return True
    typer.echo(
        "WARN  The metered lane has no spend ceiling — the dispatch lane is metered while "
        "`metered_token_ceiling` is 0 (UNSET), so nothing bounds this lane's spend. Set it with "
        "`t3 <overlay> config_setting set metered_token_ceiling <tokens>` (#4816).",
    )
    return True


def check_metered_usage_unknown() -> bool:
    """WARN when attempts in the spend window recorded UNKNOWN usage.

    The ledger sums what the provider reported. An attempt that ran turns and whose spend
    could not be read contributes ZERO to that sum, so the total under-reads by an amount
    nobody can bound — the ceiling is being judged against a floor.
    """
    from teatree.core.metered_spend import read_metered_spend  # noqa: PLC0415 — deferred: ORM read at call time

    try:
        spend = read_metered_spend()
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Metered unknown-usage check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not spend.fresh or not spend.unknown_attempts:
        return True
    typer.echo(
        f"WARN  Metered spend is a FLOOR — {spend.unknown_attempts} attempt(s) in the last "
        f"{spend.window_hours}h recorded UNKNOWN usage, contributing nothing to the "
        f"{spend.tokens:,} tokens measured against the {spend.ceiling:,} ceiling (#4816).",
    )
    return True


def check_overlay_harness_agreement() -> bool:
    """WARN when registered overlays disagree on ``agent_harness``.

    ``agent_admission_verdict`` is ONE probe per drain and holds the seat bookkeeping, so
    a per-overlay lane is out of scope by design — N verdicts would each carry their own
    headroom and double-admit. What is NOT acceptable is being quietly wrong about it, so
    a split fleet is named: the whole-box budget then describes only whichever lane the
    global provider pin resolves to.
    """
    try:
        by_overlay = _harness_by_overlay()
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Overlay-harness agreement check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if len(set(by_overlay.values())) <= 1:
        return True
    split = ", ".join(f"{overlay}={harness}" for overlay, harness in sorted(by_overlay.items()))
    typer.echo(
        f"WARN  Registered overlays disagree on their agent harness ({split}) — the admission "
        "governor resolves ONE dispatch lane per probe, so its token budget describes only the "
        "lane the global `agent_harness_provider` pin names (#4816).",
    )
    return True


def _harness_by_overlay() -> dict[str, str]:
    """``{overlay: agent_harness}`` for every registered overlay — the split's own evidence."""
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: entry-point discovery

    return {name: str(get_effective_settings(name).agent_harness) for name in get_all_overlays()}


__all__ = [
    "check_metered_lane_ceiling",
    "check_metered_usage_unknown",
    "check_overlay_harness_agreement",
]
