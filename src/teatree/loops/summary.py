"""Silent-when-idle summary DM builder (#1432).

The orchestrator emits at most one summary DM per tick. The policy
(set via ``[loops] summary_dm = "never" | "errors" | "always"``)
decides whether the DM goes out, what's in it, and which idempotency
key dedups it across repeated quiet ticks.

``never`` never DMs regardless of state. ``errors`` (default) DMs only
when at least one error occurred this tick; sustained errors share a
daily idempotency key so a broken scanner spams once per day, not once
per tick. ``always`` DMs every tick (useful for debugging; not the
default).
"""

from dataclasses import dataclass, field

_VALID_POLICIES: frozenset[str] = frozenset({"never", "errors", "always"})


@dataclass(frozen=True, slots=True)
class OrchestratorReport:
    """One tick's outcome — the surface the summary builder reads."""

    signals_count: int = 0
    actions_count: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    dispatched_loops: list[str] = field(default_factory=list)
    skipped_loops: dict[str, str] = field(default_factory=dict)

    @property
    def signal_count(self) -> int:
        """Backward-compat alias for callers that expect TickReport's name."""
        return self.signals_count


@dataclass(frozen=True, slots=True)
class SummaryDM:
    """Drafted DM payload — text + idempotency key the caller passes through."""

    text: str
    idempotency_key: str


def build_summary_dm(
    report: OrchestratorReport,
    *,
    policy: str,
    utc_day: str,
    tick_id: str = "",
) -> SummaryDM | None:
    """Build a ``SummaryDM`` (or ``None``) honouring *policy*.

    *utc_day* is the YYYY-MM-DD string the caller computes once per
    tick — passing it in keeps :func:`build_summary_dm` pure.

    *tick_id* is a per-tick identifier (the caller's tick timestamp).
    Under ``policy="always"`` it is folded into the idempotency key so
    every tick can send. The ``errors`` path keeps a day-granular key on
    purpose: a broken scanner spams once per day, not once per tick.
    """
    if policy not in _VALID_POLICIES:
        policy = "errors"
    if policy == "never":
        return None
    has_errors = bool(report.errors)
    if policy == "errors" and not has_errors:
        return None
    if policy != "always" and report.signal_count == 0 and not has_errors:
        return None

    if has_errors:
        return SummaryDM(
            text=_format_error_dm(report),
            idempotency_key=f"loops_tick_errors:{utc_day}",
        )
    suffix = f":{tick_id}" if (policy == "always" and tick_id) else ""
    return SummaryDM(
        text=_format_signals_dm(report),
        idempotency_key=f"loops_tick_summary:{utc_day}{suffix}",
    )


def _format_error_dm(report: OrchestratorReport) -> str:
    """Render the 2-line error DM body."""
    label = "loop tick errors"
    if len(report.errors) == 1:
        only = next(iter(report.errors))
        detail = report.errors[only]
        return f":warning: {label}: {only}\n_{detail}_"
    joined = ", ".join(sorted(report.errors))
    return f":warning: {label}: {len(report.errors)} loops failed\n_{joined}_"


def _format_signals_dm(report: OrchestratorReport) -> str:
    """Render the 2-line all-good DM body (policy=always path)."""
    dispatched = ", ".join(report.dispatched_loops) if report.dispatched_loops else "none"
    return (
        f":robot_face: loop tick: {report.signal_count} signals, "
        f"{report.actions_count} actions\n_loops fired: {dispatched}_"
    )
