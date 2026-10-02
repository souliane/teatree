"""Self-heal detector for a ``worker_quiescing`` gate no deploy can still explain (#3983).

Quiescing is a STEP in a sequence that ends in a restart, never a decision on its own:
``deploy/deploy.sh``'s stage-2 drain sets the gate and its stage-7 ``resume_admission``
clears it on the fresh worker. A deploy killed in between — an SSH session torn down
mid-drain, then SIGKILLed past any trap — leaves it ON, after which the claim path admits
ZERO new work while already-claimed tasks run to completion, loops keep ticking green and
``t3 worker status`` reports RUNNING. The outage is visible only by reading the flag,
which is how one ran for roughly six hours across two failed deploys.

The gate's own age bounds the deploy that could have set it, so aged past that budget
there is nothing left to explain it. The budget is summed from the deploy's own per-stage
timeouts rather than restated, so raising one on the box cannot turn a slower-but-legal
convergence into a false outage.

Reporting that is not enough (#4359): this is the one class here that halts ALL admission,
and it was the only one that waited for a human. So a strand whose convergence is PROVABLY
gone is cleared — the same ``set_worker_quiescing(value=False)`` write ``resume_admission``
would have done, verified by re-read. Deadness is established from liveness
(:mod:`~teatree.cli.doctor.deploy_liveness`), never from age alone: what the venue cannot
establish it refuses, falling back to the hard FAIL. Clearing this gate cannot admit work
onto a mismatched control DB — the two schema directions are separate terms of the same
``claim_admission_block_reason`` composition and are untouched.
"""

import datetime as dt
import os
from itertools import starmap

import typer

from teatree.cli.doctor.deploy_liveness import DeployLiveness, probe_deploy_liveness
from teatree.loop.drain import QUIESCING_SETTING

#: The stage that SETS the gate. Every later one needs ``deploy.sh`` still alive, so once
#: liveness proves it is not, this is the only stage that could have been legitimately in
#: flight — which is what makes its own timeout the floor for a proven-dead repair.
_DRAIN_STAGE: tuple[str, int] = ("TEATREE_DRAIN_TIMEOUT", 1800)
#: Every BOUNDED stage ``deploy/deploy.sh`` runs between the drain that sets the gate
#: and the ``resume_admission`` that clears it, as ``(env var, deploy.sh default)``. The
#: staged convergence (#4214) runs them SERIALLY inside one gate-ON window: the stage-4
#: drain re-dates the gate only when init's clear actually landed, so the honest bound
#: measures from stage 2. Budgeting the drain alone reported a convergence still inside
#: its own init wait as a dead one — and this finding is not deploy-sensitive in
#: ``deploy/watchdog.sh``, so it paged on sight, mid-update.
_DEPLOY_STAGE_BUDGETS: tuple[tuple[str, int], ...] = (
    _DRAIN_STAGE,
    ("TEATREE_INIT_WAIT_TIMEOUT", 1800),
    ("TEATREE_ADMIN_SWAP_BUDGET", 300),
    ("TEATREE_RESUME_TIMEOUT", 300),
)
#: Slack for the convergence's UNTIMED steps — one `up -d --no-deps` per stage, plus any
#: image pull they trigger. Bounded on purpose: the sum has to stay finite, or a genuinely
#: stranded gate never reddens.
_UNTIMED_STAGE_SLACK_SECONDS = 600


def _now() -> dt.datetime:
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    return timezone.now()


def _stage_budget(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else default


def quiescing_deploy_budget_seconds() -> int:
    """The widest window in which a set ``worker_quiescing`` is still a live deploy."""
    staged = sum(starmap(_stage_budget, _DEPLOY_STAGE_BUDGETS))
    return staged + _UNTIMED_STAGE_SLACK_SECONDS


def quiescing_repair_floor_seconds() -> int:
    """The age past which a PROVABLY dead convergence authorises the clear.

    Shorter than the full budget, because waiting out stages of a deploy that is not
    running keeps the factory stalled for nothing. Not zero, because this is also the
    window a deliberate operator pause gets before the doctor undoes it.
    """
    return _stage_budget(*_DRAIN_STAGE) + _UNTIMED_STAGE_SLACK_SECONDS


def _gate_age_seconds() -> float | None:
    """Seconds since the newest ON ``worker_quiescing`` row was written, or ``None``.

    ``None`` means no DB row carries the gate — it resolves ON from env or file, which
    no deploy could have written, so nothing can date it.
    """
    from teatree.core.models import ConfigSetting  # noqa: PLC0415 — deferred: ORM import needs the app registry

    written = [
        row.updated_at
        for row in ConfigSetting.objects.filter(key=QUIESCING_SETTING)
        if row.value is True  # a JSONField holds any shape; only a literal ON dates the gate
    ]
    return (_now() - max(written)).total_seconds() if written else None


def _is_stranded(age: float | None, liveness: DeployLiveness) -> bool:
    """True when nothing left on this box can explain the gate still being ON."""
    if age is None or age >= quiescing_deploy_budget_seconds():
        return True
    return liveness is DeployLiveness.GONE and age >= quiescing_repair_floor_seconds()


def _repair_authorised(liveness: DeployLiveness) -> bool:
    """Only proven deadness authorises the clear — an unprobeable venue reports instead.

    Called only once :func:`_is_stranded` already holds, and every path there already
    satisfies ``age is None or age >= quiescing_repair_floor_seconds()`` (the budget it
    compares against is bounded below by the floor by construction) — so once liveness
    proves GONE, authorisation follows immediately; the age argument that check needed is
    not needed here.
    """
    return liveness is DeployLiveness.GONE


def _clear_the_gate() -> str:
    """Resume admission; ``""`` once the clear is confirmed, else why it did not take.

    A failed heal is a FAIL to report, never a crashed doctor — and never a claimed heal:
    an env or file layer outranks the config store, so the write can land and change
    nothing while the factory stays stalled.
    """
    from teatree.config.resolution import worker_is_quiescing  # noqa: PLC0415 — deferred: heavy config import
    from teatree.loop.drain import set_worker_quiescing  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        set_worker_quiescing(value=False)
        still_on = worker_is_quiescing()
    except Exception as exc:  # noqa: BLE001 — the strand outlives the failure, so it must still report
        return f"no deploy can still explain it, but clearing it raised {exc.__class__.__name__}: {exc}"
    if still_on:
        return "no deploy can still explain it and clearing it did not take — an env or file layer outranks it"
    return ""


def _why_not_cleared(liveness: DeployLiveness) -> str:
    if liveness is DeployLiveness.LIVE:
        return (
            "past the window any deploy could explain, yet a convergence still looks live here, so it was NOT cleared"
        )
    return (
        "no deploy can still explain it, but this venue cannot see whether one is still running, so it was NOT cleared"
    )


def check_stranded_quiescing_gate() -> bool:
    """AUTO-REPAIR (not just FAIL) a ``worker_quiescing`` gate no deploy explains (#3983, #4359)."""
    try:
        from teatree.config.resolution import worker_is_quiescing  # noqa: PLC0415 — deferred: heavy config import

        if not worker_is_quiescing():
            return True
        age = _gate_age_seconds()
        liveness = probe_deploy_liveness(record_max_age=quiescing_deploy_budget_seconds())
        if not _is_stranded(age, liveness):
            return True
        blocked = _clear_the_gate() if _repair_authorised(liveness) else _why_not_cleared(liveness)
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Stranded-quiescing check crashed: {exc.__class__.__name__}: {exc}")
        return True
    since = f"set {age / 60:.0f} min ago" if age is not None else "set outside the config store, so undateable"
    if not blocked:
        typer.echo(
            f"WARN  Auto-cleared worker_quiescing ({since}) — the convergence that set it is "
            f"provably gone, and the claim path was admitting ZERO new work. Admission resumed."
        )
        return True
    typer.echo(
        f"FAIL  worker_quiescing is ON and {since} — {blocked} automatically, and the claim path is "
        f"admitting ZERO new work. Resume admission with "
        f"`t3 <overlay> config_setting set worker_quiescing false`."
    )
    return False


__all__ = [
    "check_stranded_quiescing_gate",
    "quiescing_deploy_budget_seconds",
    "quiescing_repair_floor_seconds",
]
