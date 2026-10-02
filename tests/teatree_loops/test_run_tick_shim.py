"""``teatree.loop.tick.run_tick`` remains wire-compatible (§5.6, #1432).

The mini-loop refactor must not break the existing ``/loop`` dynamic
mode entry point. ``run_tick``'s signature, ``TickReport`` shape, and
statusline target stay identical — the orchestrator is wired underneath
but the legacy path keeps working.
"""

import datetime as dt
import inspect

from django.test import TestCase

from teatree.loop.tick import TickReport, TickRequest, run_tick


class RunTickShimSignatureTests(TestCase):
    def test_run_tick_signature_unchanged(self) -> None:
        sig = inspect.signature(run_tick)
        params = list(sig.parameters)
        # First positional is the optional request; rest are keyword-only.
        assert params[0] == "request"
        assert "statusline_path" in sig.parameters
        assert "colorize" in sig.parameters
        assert "now" in sig.parameters

    def test_tick_report_carries_signals_and_actions(self) -> None:
        report = TickReport(started_at=dt.datetime(2026, 5, 28, tzinfo=dt.UTC))
        assert hasattr(report, "signals")
        assert hasattr(report, "actions")
        assert hasattr(report, "statusline_path")
        assert hasattr(report, "errors")

    def test_run_tick_returns_tick_report_shape(self) -> None:
        # Empty request, no backends, scanners=[] forces the empty-jobs
        # branch so we don't hit the real Task table.
        report = run_tick(TickRequest(scanners=[]))
        assert isinstance(report, TickReport)
        assert report.signal_count == 0
        assert report.action_count == 0
