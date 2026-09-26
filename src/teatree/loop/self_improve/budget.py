"""Pre-cycle budget gate for the self-improve monitor.

A single ``precheck_budget`` function consulted at the top of every
schedule cycle.  Returns a verdict with an ``ok`` flag and a structured
``reason`` so the schedule module can emit a dim one-line statusline
note (per BLUEPRINT § 5.7) when it skips a cycle — never a Slack DM.

The verdict is intentionally a value object: tests inject the underlying
samples (RAM%, recent spawn count, recent denial count) rather than
mocking ``psutil``, so the budget logic is fully deterministic.
"""

import datetime as dt
import os
from collections.abc import Callable
from dataclasses import dataclass

from django.utils import timezone

from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.utils.disk_probe import disk_crit_percent, read_disk_used_percent
from teatree.utils.ram_probe import read_ram_used_percent as _read_ram_used_percent

DEFAULT_RAM_USED_CEILING_PCT = 85
DEFAULT_SPAWN_CAP_WINDOW_SECONDS = 60 * 60
DEFAULT_SPAWN_CAP = 3
# Env-var NAME (not a credential value); split-assign so the literal does
# not match ruff's hardcoded-password (S105) heuristic on the trailing
# "_BUDGET" / "_TOKEN" word.
_ENV_PREFIX = "T3_SELF_IMPROVE_"
DEFAULT_TOKEN_BUDGET_ENV = f"{_ENV_PREFIX}TOKEN_BUDGET"


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """Outcome of one pre-cycle budget check."""

    ok: bool
    reason: str = ""

    @classmethod
    def skip(cls, reason: str) -> "BudgetVerdict":
        return cls(ok=False, reason=reason)

    @classmethod
    def allow(cls) -> "BudgetVerdict":
        return cls(ok=True, reason="")


def precheck_budget(
    *,
    ram_used_percent: float | None = None,
    disk_used_percent: float | None = None,
    recent_self_improve_spawns: int = 0,
    token_budget_remaining: int | None = None,
    ram_probe: Callable[[], float] | None = None,
) -> BudgetVerdict:
    """Return ``skip(reason)`` when any guardrail fails, else ``allow()``.

    Order is disk → RAM → spawn cap → token budget so the
    first-failing reason is the most user-actionable one.
    """
    disk_sample = disk_used_percent if disk_used_percent is not None else read_disk_used_percent()
    if disk_sample is not None and disk_sample >= disk_crit_percent():
        return BudgetVerdict.skip(f"low_disk (used={disk_sample:.0f}%)")
    sample = (
        ram_used_percent
        if ram_used_percent is not None
        else (ram_probe() if ram_probe is not None else _read_ram_used_percent())
    )
    if sample >= DEFAULT_RAM_USED_CEILING_PCT:
        return BudgetVerdict.skip(f"low_ram (used={sample:.0f}%)")
    if recent_self_improve_spawns >= DEFAULT_SPAWN_CAP:
        return BudgetVerdict.skip(f"spawn_cap ({recent_self_improve_spawns} in window)")
    if token_budget_remaining is not None and token_budget_remaining <= 0:
        return BudgetVerdict.skip("token_budget_exhausted")
    return BudgetVerdict.allow()


def recent_self_improve_firings(seconds: int, *, now: dt.datetime | None = None) -> int:
    """Count self-improve firings (any action) in the trailing window.

    The spawn-cap sample: a firing is what a cycle produces, so a box already
    at the cap has done its share of self-improve work for the window and the
    next cycle waits for these to age out.
    """
    moment = now or timezone.now()
    cutoff = moment - dt.timedelta(seconds=seconds)
    return SelfImproveFiring.objects.filter(last_fired_at__gte=cutoff).count()


def token_budget_from_env() -> int | None:
    """Return the configured token budget (``None`` when unset).

    Phase 1 detectors are mechanical (no LLM judgment); the env knob is
    documented now so Phase 3 detectors plug into the same gate without
    a schema change.
    """
    raw = os.environ.get(DEFAULT_TOKEN_BUDGET_ENV, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
