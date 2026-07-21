"""The extracted loop-watchdog concern, tested against the sibling module directly.

``LoopWatchdog`` / ``TaskUsage`` live in ``teatree.agents.headless_watchdog`` and
are re-exported from ``teatree.agents.headless`` for back-compat. This mirror
names the new module's public symbols directly so the per-diff coverage sees the
seam that owns them; the DB-backed evaluation stays in ``test_headless.py``.
"""

from teatree.agents.headless_watchdog import LoopWatchdog, TaskUsage
from teatree.core.models import Task


class TestBreachReasonWithExplicitUsage:
    """``breach_reason`` is pure when handed a pre-sampled ``TaskUsage`` — no DB read.

    An unsaved ``Task()`` is enough: the ``task`` arg is only consulted to sample
    usage when ``usage`` is omitted, so an explicit snapshot never touches the DB.
    """

    def test_all_ceilings_disabled_never_breaches(self) -> None:
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)
        usage = TaskUsage(turns=10_000, cost_usd=999.0)
        assert watchdog.breach_reason(Task(), elapsed_seconds=1e9, usage=usage) is None

    def test_runtime_ceiling(self) -> None:
        watchdog = LoopWatchdog(max_runtime_seconds=30, max_turns=0, max_cost_usd=0.0)
        reason = watchdog.breach_reason(Task(), elapsed_seconds=31, usage=TaskUsage(0, 0.0))
        assert reason is not None
        assert "runtime" in reason

    def test_turns_ceiling_reads_explicit_usage(self) -> None:
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=200, max_cost_usd=0.0)
        reason = watchdog.breach_reason(Task(), elapsed_seconds=1, usage=TaskUsage(turns=260, cost_usd=0.0))
        assert reason is not None
        assert "turns" in reason
        assert "260" in reason

    def test_cost_ceiling_reads_explicit_usage(self) -> None:
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=5.0)
        reason = watchdog.breach_reason(Task(), elapsed_seconds=1, usage=TaskUsage(turns=0, cost_usd=7.5))
        assert reason is not None
        assert "cost" in reason

    def test_under_thresholds_no_breach(self) -> None:
        watchdog = LoopWatchdog(max_runtime_seconds=600, max_turns=200, max_cost_usd=5.0)
        assert watchdog.breach_reason(Task(), elapsed_seconds=60, usage=TaskUsage(turns=10, cost_usd=0.5)) is None
