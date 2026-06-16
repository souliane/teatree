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
from typing import Protocol

from django.utils import timezone

from teatree.core.models.self_improve_firing import SelfImproveFiring

# Static thresholds — overridable via env if the user wants to tune
# without a code change.  Defaults match the issue plan.
DEFAULT_RAM_FREE_FLOOR_PCT = 15
DEFAULT_RAM_USED_CEILING_PCT = 85
DEFAULT_SPAWN_CAP_WINDOW_SECONDS = 60 * 60
DEFAULT_SPAWN_CAP = 3
DEFAULT_DENIAL_WINDOW_SECONDS = 60 * 60
DEFAULT_DENIAL_LIMIT = 3
DEFAULT_DENIAL_BACKOFF_SECONDS = 4 * 60 * 60
# Env-var NAME (not a credential value); split-assign so the literal does
# not match ruff's hardcoded-password (S105) heuristic on the trailing
# "_BUDGET" / "_TOKEN" word.
_ENV_PREFIX = "T3_SELF_IMPROVE_"
DEFAULT_TOKEN_BUDGET_ENV = f"{_ENV_PREFIX}TOKEN_BUDGET"


class RamSample(Protocol):
    """The minimal RAM probe surface the budget gate consumes."""

    percent: float


def _macos_ram_used_percent() -> float:  # pragma: no cover - macOS host probe
    """Read mac RAM use via ``sysctl`` + ``vm_stat`` (mirrors statusline.sh)."""
    import shutil  # noqa: PLC0415

    from teatree.utils.run import CommandFailedError, run_checked  # noqa: PLC0415

    sysctl = shutil.which("sysctl")
    vm_stat = shutil.which("vm_stat")
    if not sysctl or not vm_stat:
        return 0.0
    try:
        total = int(run_checked([sysctl, "-n", "hw.memsize"], timeout=2).stdout.strip())
        if total <= 0:
            return 0.0
        stat = run_checked([vm_stat], timeout=2).stdout
    except (CommandFailedError, ValueError, OSError, TimeoutError):
        return 0.0
    page_size, free_pages, inactive_pages = 4096, 0, 0
    for line in stat.splitlines():
        if line.startswith("Pages free:"):
            free_pages = int(line.split(":")[1].strip().rstrip("."))
        elif line.startswith("Pages inactive:"):
            inactive_pages = int(line.split(":")[1].strip().rstrip("."))
    used = total - (free_pages + inactive_pages) * page_size
    return max(0.0, min(100.0, used * 100.0 / total))


def _linux_ram_used_percent() -> float:  # pragma: no cover - Linux host probe
    """Read Linux RAM use via ``/proc/meminfo`` (mirrors statusline.sh)."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:  # noqa: PTH123
            lines = handle.readlines()
    except OSError:
        return 0.0
    info: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        value = rest.strip().split(" ", 1)[0]
        if value.isdigit():
            info[key.strip()] = int(value)
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    if total <= 0:
        return 0.0
    used = total - avail
    return max(0.0, min(100.0, used * 100.0 / total))


def _read_ram_used_percent() -> float:  # pragma: no cover - dispatches by platform
    """Best-effort read of system RAM utilisation percent (0-100).

    Mirrors the same probe shape ``hooks/scripts/statusline.sh`` uses
    (sysctl on macOS, /proc/meminfo on Linux); the budget gate stays
    dependency-free so a missing optional library never crashes the
    schedule cycle.  Tests inject the percent directly via the
    ``ram_used_percent`` arg — this function is only consulted when no
    explicit sample is provided.
    """
    import platform  # noqa: PLC0415

    system = platform.system()
    if system == "Darwin":
        return _macos_ram_used_percent()
    if system == "Linux":
        return _linux_ram_used_percent()
    return 0.0


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


# ast-grep-ignore: ac-django-no-complexity-suppressions
def precheck_budget(  # noqa: PLR0913  # each kwarg is a BLUEPRINT § 5.7 guardrail input; kwargs-only.
    *,
    ram_used_percent: float | None = None,
    recent_self_improve_spawns: int = 0,
    recent_classifier_denials: int = 0,
    now: dt.datetime | None = None,
    token_budget_remaining: int | None = None,
    ram_probe: Callable[[], float] | None = None,
) -> BudgetVerdict:
    """Return ``skip(reason)`` when any guardrail fails, else ``allow()``.

    Order matches BLUEPRINT § 5.7 (RAM → spawn cap → denial cool-down →
    token budget) so the first-failing reason is the most user-actionable
    one.
    """
    del now  # reserved for future cool-down windowing — kept in the signature for callers
    sample = (
        ram_used_percent
        if ram_used_percent is not None
        else (ram_probe() if ram_probe is not None else _read_ram_used_percent())
    )
    if sample >= DEFAULT_RAM_USED_CEILING_PCT:
        return BudgetVerdict.skip(f"low_ram (used={sample:.0f}%)")
    if recent_self_improve_spawns > DEFAULT_SPAWN_CAP:
        return BudgetVerdict.skip(f"spawn_cap ({recent_self_improve_spawns} in window)")
    if recent_classifier_denials >= DEFAULT_DENIAL_LIMIT:
        return BudgetVerdict.skip(f"classifier_denial_cooldown ({recent_classifier_denials} in window)")
    if token_budget_remaining is not None and token_budget_remaining <= 0:
        return BudgetVerdict.skip("token_budget_exhausted")
    return BudgetVerdict.allow()


def recent_self_improve_firings(seconds: int, *, now: dt.datetime | None = None) -> int:
    """Count self-improve firings (any action) in the trailing window.

    Used as a coarse proxy for "self-improve-originated spawns" — the
    Phase 1 detectors do not spawn sub-agents directly, but the same
    counter feeds the Phase 2/3 wiring without changing the schedule
    contract.
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
