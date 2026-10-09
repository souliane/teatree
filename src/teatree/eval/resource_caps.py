"""The eval lane's env-configurable resource caps and their resolvers.

Every cap here is GENEROUS by design. A truncated run measures the cap, not the
behaviour under test — the first full metered run lost ~18 scenarios to cap
truncation, all false negatives — so each default is set high enough that a
slow-but-correct trajectory finishes inside it, and each is overridable by an
env var for the run that genuinely needs more.

Split out of ``api_runner`` as its own concern: these are read by the SDK
runner, the pydantic-ai runner, the judge, and the eval CLI alike, none of which
needs the runner itself.
"""

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.eval.model_variant import EffortLevel

#: Env var names for the metered lane's GENEROUS, configurable resource caps.
_WATCHDOG_ENV_VAR = "T3_EVAL_WATCHDOG_SECONDS"
_MAX_TURNS_ENV_VAR = "T3_EVAL_MAX_TURNS"
_METERED_BUDGET_ENV_VAR = "T3_EVAL_MAX_BUDGET_USD"
_METERED_EFFORT_ENV_VAR = "T3_EVAL_EFFORT"

#: GENEROUS per-scenario wall-clock watchdog (seconds). 120s was too tight for
#: sub-agent-spawning scenarios (an orchestrator that delegates an investigation
#: timed out before it finished), so the default is raised. Override via
#: ``T3_EVAL_WATCHDOG_SECONDS``.
#:
#: In the EVAL lane, COST ($) and TURNS are the meaningful gates — a
#: behaviourally-correct trajectory bounded by its ``max_budget_usd`` /
#: ``max_turns`` must NOT be falsely red'd by latency alone (#2192: a
#: ``timeout`` cap-taints the pass@k aggregate exactly like a budget/turn cap,
#: so a slow-but-correct trial reds a scenario whose other trial passed). The
#: wall-clock watchdog is therefore only a GENEROUS hang-backstop: high enough
#: that a slow-but-correct fan-out/delegation trajectory finishes inside it,
#: yet FINITE so a true hang (one that burns neither cost nor turns) is still
#: caught. It is deliberately NOT a latency gate. Provisioning / E2E / workspace
#: timeouts are unaffected — those legitimately catch I/O waste and live
#: elsewhere; this constant scopes strictly to the eval lane.
DEFAULT_WATCHDOG_SECONDS = 900

#: GENEROUS default per-run budget for the metered ``t3 eval run --backend api``
#: lane — distinct from the cheap-lane ``api_runner.MAX_BUDGET_USD`` runner floor
#: (0.10), which truncated finishing scenarios. ~10x the cheap floor, below the
#: benchmark's 2.0; override via ``T3_EVAL_MAX_BUDGET_USD``.
METERED_DEFAULT_BUDGET_USD = 1.0

#: The metered lane's representative reasoning effort. The lane otherwise runs at
#: the model's DEFAULT effort, while real usage is high effort — so a default-effort
#: pass-rate is pessimistic. A scenario's own ``@effort`` still wins. Override via
#: ``T3_EVAL_EFFORT``.
METERED_DEFAULT_EFFORT: "EffortLevel" = "high"


def env_float(name: str, *, default: float) -> float:
    """Resolve a positive ``float`` from env *name*, falling back to *default*.

    A missing, empty, unparsable, or non-positive value yields the generous
    *default* — a fat-fingered override never silently tightens the cap to an
    accidental 0.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def resolve_watchdog_seconds() -> float:
    """The generous per-scenario watchdog, ``T3_EVAL_WATCHDOG_SECONDS`` overriding the default."""
    return env_float(_WATCHDOG_ENV_VAR, default=float(DEFAULT_WATCHDOG_SECONDS))


def resolve_max_turns_override(explicit: int | None = None) -> int | None:
    """An *explicit* override wins; else the ``T3_EVAL_MAX_TURNS`` knob; else ``None`` to defer to spec.

    Defers to each scenario's own ``max_turns`` (the per-scenario turn budget, mirroring
    per-scenario cost) when neither is set; a missing/empty/unparsable/non-positive env value
    yields ``None`` — never a silent global turn cap.
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get(_MAX_TURNS_ENV_VAR, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def resolve_metered_budget_usd() -> float:
    """The generous metered-lane budget, ``T3_EVAL_MAX_BUDGET_USD`` overriding the default."""
    return env_float(_METERED_BUDGET_ENV_VAR, default=METERED_DEFAULT_BUDGET_USD)


def resolve_metered_effort() -> "EffortLevel":
    """The representative metered-lane effort, ``T3_EVAL_EFFORT`` overriding the default.

    An invalid/unknown override falls back to the representative default rather
    than passing a bad level through to the SDK.
    """
    from teatree.eval.model_variant import EFFORT_LEVELS  # noqa: PLC0415 — avoid an import cycle at module load.

    raw = os.environ.get(_METERED_EFFORT_ENV_VAR, "").strip()
    return raw if raw in EFFORT_LEVELS else METERED_DEFAULT_EFFORT  # type: ignore[return-value]
