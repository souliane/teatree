"""Summary DM — silent-when-idle policy contract."""

from teatree.loops.summary import OrchestratorReport, build_summary_dm


class TestPolicyNever:
    def test_never_returns_none_with_signals(self) -> None:
        report = OrchestratorReport(signals_count=5)
        assert build_summary_dm(report, policy="never", utc_day="2026-05-28") is None

    def test_never_returns_none_with_errors(self) -> None:
        report = OrchestratorReport(errors={"inbox": "boom"})
        assert build_summary_dm(report, policy="never", utc_day="2026-05-28") is None


class TestPolicyErrors:
    def test_quiet_tick_returns_none(self) -> None:
        report = OrchestratorReport(signals_count=0, errors={})
        assert build_summary_dm(report, policy="errors", utc_day="2026-05-28") is None

    def test_signals_alone_returns_none(self) -> None:
        # 0 errors + N signals — policy=errors is for ERRORS, not noise.
        report = OrchestratorReport(signals_count=10, errors={})
        assert build_summary_dm(report, policy="errors", utc_day="2026-05-28") is None

    def test_one_error_emits_named(self) -> None:
        report = OrchestratorReport(errors={"inbox": "auth failed"})
        dm = build_summary_dm(report, policy="errors", utc_day="2026-05-28")
        assert dm is not None
        assert "inbox" in dm.text
        assert "auth failed" in dm.text
        assert dm.idempotency_key == "loops_tick_errors:2026-05-28"

    def test_multiple_errors_emits_count(self) -> None:
        report = OrchestratorReport(errors={"inbox": "boom", "review": "rate limit"})
        dm = build_summary_dm(report, policy="errors", utc_day="2026-05-28")
        assert dm is not None
        assert "2 loops failed" in dm.text
        assert "inbox" in dm.text
        assert "review" in dm.text

    def test_idempotency_key_dedups_within_day(self) -> None:
        report = OrchestratorReport(errors={"inbox": "boom"})
        a = build_summary_dm(report, policy="errors", utc_day="2026-05-28")
        b = build_summary_dm(report, policy="errors", utc_day="2026-05-28")
        assert a is not None
        assert b is not None
        assert a.idempotency_key == b.idempotency_key

    def test_idempotency_key_rolls_over_at_utc_day_change(self) -> None:
        report = OrchestratorReport(errors={"inbox": "boom"})
        a = build_summary_dm(report, policy="errors", utc_day="2026-05-28")
        b = build_summary_dm(report, policy="errors", utc_day="2026-05-29")
        assert a is not None
        assert b is not None
        assert a.idempotency_key != b.idempotency_key


class TestPolicyAlways:
    def test_quiet_tick_still_emits(self) -> None:
        report = OrchestratorReport(signals_count=0, errors={})
        dm = build_summary_dm(report, policy="always", utc_day="2026-05-28")
        assert dm is not None
        assert "0 signals" in dm.text

    def test_busy_tick_emits_counts(self) -> None:
        report = OrchestratorReport(signals_count=5, actions_count=3, dispatched_loops=["inbox", "review"])
        dm = build_summary_dm(report, policy="always", utc_day="2026-05-28")
        assert dm is not None
        assert "5 signals" in dm.text
        assert "3 actions" in dm.text
        assert "inbox" in dm.text


class TestUnknownPolicyFallsBackToErrors:
    def test_unknown_policy_treated_as_errors(self) -> None:
        # Defensive: a typo in the toml should not crash the loop;
        # silently degrade to the safer "errors" policy.
        report = OrchestratorReport(signals_count=0, errors={})
        assert build_summary_dm(report, policy="bogus", utc_day="2026-05-28") is None
        report = OrchestratorReport(errors={"inbox": "boom"})
        dm = build_summary_dm(report, policy="bogus", utc_day="2026-05-28")
        assert dm is not None
