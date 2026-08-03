"""Self-heal detector for a ``worker_quiescing`` gate no deploy can still explain (#3983).

Quiescing is a STEP in a sequence that ends in a restart, never a decision on its own:
``deploy/deploy.sh``'s drain sets the gate and the fresh worker's init clears it. A
deploy killed in between — an SSH session torn down mid-drain, then SIGKILLed past any
trap — leaves it ON, after which the claim path admits ZERO new work while already-
claimed tasks run to completion, loops keep ticking green and ``t3 worker status``
reports RUNNING. The outage is visible only by reading the flag, which is how one ran
for roughly six hours across two failed deploys.

The gate's own age bounds the deploy that could have set it, so aged past that budget
there is nothing left to explain it and the finding is a hard FAIL.
"""

import datetime as dt
import os

import typer

from teatree.loop.drain import QUIESCING_SETTING

#: The drain grace ``deploy/deploy.sh`` allows (its ``TEATREE_DRAIN_TIMEOUT`` default).
_DEFAULT_DRAIN_TIMEOUT_SECONDS = 1800
#: What the deploy still has to do after the drain — build, `up -d`, and the 60x10s
#: admin health wait — before the fresh worker's init clears the gate.
_DEPLOY_SWAP_BUDGET_SECONDS = 1200


def _now() -> dt.datetime:
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    return timezone.now()


def quiescing_deploy_budget_seconds() -> int:
    """The widest window in which a set ``worker_quiescing`` is still a live deploy."""
    raw = os.environ.get("TEATREE_DRAIN_TIMEOUT", "").strip()
    drain = int(raw) if raw.isdigit() else _DEFAULT_DRAIN_TIMEOUT_SECONDS
    return drain + _DEPLOY_SWAP_BUDGET_SECONDS


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


def check_stranded_quiescing_gate() -> bool:
    """FAIL when ``worker_quiescing`` outlives any deploy that could explain it (#3983)."""
    try:
        from teatree.config.resolution import worker_is_quiescing  # noqa: PLC0415 — deferred: heavy config import

        if not worker_is_quiescing():
            return True
        age = _gate_age_seconds()
        if age is not None and age < quiescing_deploy_budget_seconds():
            return True
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Stranded-quiescing check crashed: {exc.__class__.__name__}: {exc}")
        return True
    since = f"set {age / 60:.0f} min ago" if age is not None else "set outside the config store, so undateable"
    typer.echo(
        f"FAIL  worker_quiescing is ON and {since} — no deploy can still explain it, and the claim "
        f"path is admitting ZERO new work. Resume admission with "
        f"`t3 <overlay> config_setting set worker_quiescing false`."
    )
    return False


__all__ = ["check_stranded_quiescing_gate", "quiescing_deploy_budget_seconds"]
