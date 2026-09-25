"""OTel pressure spans are durable evidence for cause-based self-improvement."""

import datetime as dt
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from teatree.core.admission_governor import SupplementalAdmissionSignals, YieldSignal, decide_admission, pressure_for
from teatree.core.admission_pressure import (
    UNREAD_QUOTA,
    MachineSignal,
    MeteredSignal,
    QuotaSignal,
    resume_ceiling_conflict,
)
from teatree.core.telemetry.admission import (
    PressureSpanExporter,
    _safe_admission_reason,
    _safe_observation,
    checked_pressure_observations,
    latest_admission_reason,
    recent_pressure_observations,
    record_admission_decision,
    record_factory_issue,
    record_lifecycle_transition,
)
from teatree.loop.self_improve.detectors.base import ActionRung
from teatree.loop.self_improve.detectors.pressure_incident import PressureIncidentDetector


def _emit(provider: TracerProvider, *, cause: str, epoch: int, pressure: float = 1.1) -> None:
    with provider.get_tracer(__name__).start_as_current_span("teatree.admission.decision") as span:
        span.set_attribute("teatree.pressure.cause", cause)
        span.set_attribute("teatree.pressure.value", pressure)
        span.set_attribute("teatree.pressure.band", "halt")
        denied = False
        span.set_attribute("teatree.admission.admit", denied)
        span.set_attribute("teatree.admission.reason", "load 10 at/over the 50 watermark on 10 core(s)")
        span.set_attribute("teatree.admission.lane", "headless")
        span.set_attribute("teatree.observed_epoch", epoch)


def test_span_exporter_persists_bounded_safe_observations(tmp_path) -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter(directory=tmp_path)))
    now = dt.datetime(2026, 9, 23, 12, tzinfo=dt.UTC)
    _emit(provider, cause="load", epoch=int(now.timestamp()))
    provider.shutdown()

    rows = recent_pressure_observations(directory=tmp_path, now=now)
    assert len(rows) == 1
    assert rows[0]["cause"] == "load"
    assert rows[0]["pressure"] == pytest.approx(1.1)
    assert rows[0]["admit"] is False
    assert set(rows[0]) == {"epoch", "cause", "pressure", "band", "admit", "reason", "lane"}
    assert latest_admission_reason(directory=tmp_path, now=now) == "load 10 at/over the 50 watermark on 10 core(s)"


@pytest.mark.parametrize(
    ("metered", "cause"),
    [
        (
            MeteredSignal(
                fresh=True,
                utilization=1.0,
                spend_detail=(
                    "metered lane spent 2,000,000 tokens of the 1,000,000 ceiling over the last 24h "
                    "(~$0.00 ESTIMATED (price-table arithmetic, not a billed amount))"
                ),
            ),
            "metered-spend",
        ),
        (
            MeteredSignal(
                fresh=True,
                parked=True,
                park_detail=(
                    "the metered lane is parked on a rate_limit window with no recorded reset "
                    "— re-probing it is pure burn"
                ),
            ),
            "metered-lane-parked",
        ),
    ],
)
def test_metered_denial_is_exported_with_its_safe_cause_and_reason(
    tmp_path, metered: MeteredSignal, cause: str
) -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter(directory=tmp_path)))
    machine = MachineSignal(cores=8, load1=1.0, ram_available_gb=20.0)
    with (
        patch("teatree.core.admission_governor._quota_brake_enabled", return_value=True),
        patch("teatree.core.admission_governor._shed_at", return_value=0.9),
    ):
        decision = decide_admission(
            quota=UNREAD_QUOTA, machine=machine, signals=SupplementalAdmissionSignals(metered=metered)
        )
        pressure = pressure_for(quota=UNREAD_QUOTA, machine=machine, metered=metered)

    with patch("teatree.core.telemetry.admission._provider", return_value=provider):
        record_admission_decision(decision=decision, pressure=pressure, lane="headless")

    rows = recent_pressure_observations(directory=tmp_path, now=dt.datetime.now(tz=dt.UTC))
    assert decision.admit is False
    assert len(rows) == 1
    assert rows[0]["cause"] == cause
    assert rows[0]["reason"] == pressure.reason


def test_numeric_prefix_quota_cause_is_not_dropped(tmp_path) -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter(directory=tmp_path)))
    now = dt.datetime(2026, 9, 23, 12, tzinfo=dt.UTC)
    _emit(provider, cause="5h-quota", epoch=int(now.timestamp()))

    assert [row["cause"] for row in recent_pressure_observations(directory=tmp_path, now=now)] == ["5h-quota"]
    provider.shutdown()


def test_old_admission_file_cannot_surface_untrusted_reason_or_lane(tmp_path) -> None:
    now = dt.datetime(2026, 9, 23, 12, tzinfo=dt.UTC)
    row = {
        "epoch": int(now.timestamp()),
        "cause": "load",
        "pressure": 1.1,
        "band": "halt",
        "admit": False,
        "reason": "token=supersecret /private/agent-home",
        "lane": "headless",
    }
    path = tmp_path / "admission-2026-09-23.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    result = checked_pressure_observations(directory=tmp_path, now=now)
    assert not result.complete
    assert result.rows == []
    assert latest_admission_reason(directory=tmp_path, now=now) is None

    row["reason"] = "unknown"
    row["lane"] = "token=supersecret"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert not checked_pressure_observations(directory=tmp_path, now=now).complete


def test_admission_span_with_array_attributes_is_rejected_or_sanitized() -> None:
    attrs = {
        "teatree.observed_epoch": 1790164800,
        "teatree.pressure.cause": "load",
        "teatree.pressure.value": 1.1,
        "teatree.pressure.band": "halt",
        "teatree.admission.admit": False,
        "teatree.admission.reason": "unknown",
        "teatree.admission.lane": ["secret"],
    }
    span = SimpleNamespace(name="teatree.admission.decision", attributes=attrs)
    observation = _safe_observation(span)
    assert observation is not None
    assert observation["lane"] == "unknown"

    attrs["teatree.pressure.band"] = ["halt"]
    assert _safe_observation(span) is None


def test_detector_clusters_repeated_denials_by_cause(tmp_path) -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter(directory=tmp_path)))
    now = dt.datetime(2026, 9, 23, 12, tzinfo=dt.UTC)
    epoch = int(now.timestamp())
    for _ in range(2):
        _emit(provider, cause="load", epoch=epoch)
    _emit(provider, cause="weekly-quota", epoch=epoch)
    detector = PressureIncidentDetector(directory=tmp_path, now=lambda: now)
    assert detector.detect() == []

    _emit(provider, cause="load", epoch=epoch)
    reports = detector.detect()
    assert len(reports) == 1
    assert reports[0].dedup_key.endswith("load")
    assert reports[0].payload["observations"] == 3
    assert reports[0].requested_rung == ActionRung.STATUSLINE
    assert reports[0].max_rung == ActionRung.STATUSLINE
    initial_hash = reports[0].state_hash

    _emit(provider, cause="load", epoch=epoch)
    assert detector.detect()[0].state_hash == initial_hash
    for _ in range(6):
        _emit(provider, cause="load", epoch=epoch)
    assert detector.detect()[0].state_hash != initial_hash
    assert detector.detect()[0].requested_rung == ActionRung.SLACK
    assert detector.detect()[0].max_rung == ActionRung.SLACK
    for _ in range(20):
        _emit(provider, cause="load", epoch=epoch)
    assert detector.detect()[0].requested_rung == ActionRung.TICKET
    assert detector.detect()[0].max_rung == ActionRung.TICKET
    provider.shutdown()


def test_stale_spans_do_not_make_a_live_incident(tmp_path) -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter(directory=tmp_path)))
    now = dt.datetime(2026, 9, 23, 12, tzinfo=dt.UTC)
    for _ in range(3):
        _emit(provider, cause="memory", epoch=int((now - dt.timedelta(hours=1)).timestamp()))
    assert PressureIncidentDetector(directory=tmp_path, now=lambda: now).detect() == []
    provider.shutdown()


def test_inert_cgroup_probe_is_a_direct_owner_alert_not_a_pressure_span(tmp_path) -> None:
    with patch("teatree.loop.self_improve.detectors.pressure_incident.cgroup_memory_probe_inert", return_value=True):
        reports = PressureIncidentDetector(directory=tmp_path).detect()

    assert len(reports) == 1
    assert reports[0].payload["kind"] == "cgroup_probe_inert"
    assert reports[0].requested_rung == ActionRung.SLACK
    assert reports[0].payload["requires_delivery"] is True


def test_intentionally_unlimited_cgroup_has_no_alert(tmp_path) -> None:
    with patch("teatree.loop.self_improve.detectors.pressure_incident.cgroup_memory_probe_inert", return_value=False):
        assert PressureIncidentDetector(directory=tmp_path).detect() == []


def test_telemetry_failure_never_blocks_admission() -> None:
    decision = SimpleNamespace(admit=False, ceiling=0, reason="load over watermark")
    pressure = SimpleNamespace(dominant=None, value=1.2, band=SimpleNamespace(value="halt"))
    with patch("teatree.core.telemetry.admission._provider", side_effect=RuntimeError("collector unavailable")):
        record_admission_decision(decision=decision, pressure=pressure, lane="loop")


def test_remote_span_cannot_contain_untrusted_admission_reason() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    decision = SimpleNamespace(
        admit=False,
        ceiling=0,
        cause="deadbeefdeadbeefdeadbeefdeadbeef",
        reason="token=supersecret /private/agent-home",
    )
    pressure = SimpleNamespace(dominant=None, value=1.2, band=SimpleNamespace(value="halt"))

    with patch("teatree.core.telemetry.admission._provider", return_value=provider):
        record_admission_decision(decision=decision, pressure=pressure, lane="secret /private/agent-home")

    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs is not None
    assert "supersecret" not in str(attrs)
    assert "/private/agent-home" not in str(attrs)
    assert "deadbeef" not in str(attrs)
    assert attrs["teatree.admission.reason"] == "unknown"
    assert attrs["teatree.pressure.cause"] == "unknown"
    assert attrs["teatree.admission.lane"] == "unknown"
    provider.shutdown()


def test_remote_factory_and_lifecycle_spans_reject_token_shaped_causes() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    token = "deadbeefdeadbeefdeadbeefdeadbeef"
    report = SimpleNamespace(
        payload={"kind": token, "cause": token},
        detector="lifecycle_incident",
        severity="error",
        dedup_key="test-safe-dedup-key",
    )

    with patch("teatree.core.telemetry.admission._provider", return_value=provider):
        record_factory_issue(report)
        record_lifecycle_transition(kind="task.failed", entity_id=1, cause=token)

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert token not in str([span.attributes for span in spans])
    factory_attrs = spans[0].attributes
    lifecycle_attrs = spans[1].attributes
    assert factory_attrs is not None
    assert lifecycle_attrs is not None
    assert factory_attrs["teatree.issue.kind"] == "unknown"
    assert factory_attrs["teatree.issue.cause"] == "unknown"
    assert lifecycle_attrs["teatree.lifecycle.cause"] == "unknown"
    provider.shutdown()


def test_stale_statusline_issue_keeps_its_detector_category() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    report = SimpleNamespace(
        payload={"cause": "unknown"},
        detector="stale_statusline_entry",
        severity="warn",
        dedup_key="stale-statusline-test",
    )

    with patch("teatree.core.telemetry.admission._provider", return_value=provider):
        record_factory_issue(report)

    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs is not None
    assert attrs["teatree.issue.kind"] == "stale_statusline_entry"
    provider.shutdown()


def test_invalid_admission_fields_do_not_reach_remote_exporter() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    decision = SimpleNamespace(admit="token=secret", ceiling="token=secret", reason="unknown", cause="load")
    pressure = SimpleNamespace(dominant=None, value="token=secret", band=SimpleNamespace(value="token=secret"))

    with patch("teatree.core.telemetry.admission._provider", return_value=provider):
        record_admission_decision(decision=decision, pressure=pressure, lane="loop")

    assert exporter.get_finished_spans() == ()
    provider.shutdown()


@pytest.mark.parametrize(
    "reason",
    [
        "every account is quota-exhausted — retrying into a rate limit is pure burn",
        "weekly window spent (95%) — no budget left to admit against",
        "5h window spent (95%) — a hard rate limit is imminent",
        "weekly burn outruns the reset (pace 0.42) — pacing to the window",
        "load 10 at/over the 50 watermark on 10 core(s)",
        "host swap 25% at/over the 25% watermark",
        "admitting up to 2 — signals healthy",
        "yield collapsed (0/5 terminal tasks completed) — the marginal token is buying zero",
        (f"5.2 GB available at/under the 6 GB watermark ({resume_ceiling_conflict(5.2)})"),
    ],
)
def test_admission_reason_allowlist_preserves_governor_templates(reason: str) -> None:
    assert _safe_admission_reason(reason) == reason


def test_admission_reason_allowlist_fails_closed_on_new_free_text() -> None:
    assert _safe_admission_reason("load 10 at/over the 50 watermark on 10 core(s); secret=abc") == "unknown"


def test_metered_reason_allowlist_accepts_reset_time_but_rejects_extra_text() -> None:
    reason = (
        "the metered lane is parked on a rate_limit window until 2026-09-25T12:34:56.123456+00:00 "
        "— re-probing it is pure burn"
    )
    assert _safe_admission_reason(reason) == reason
    assert _safe_admission_reason(reason + " secret=abc") == "unknown"


class YieldCauseTelemetryTests(TestCase):
    def test_yield_collapse_denial_is_grouped_under_yield_not_healthy_load(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter(directory=root)))
            quota = QuotaSignal(
                fresh=True,
                all_accounts_exhausted=False,
                weekly_utilization=0.1,
                short_utilization=0.1,
                seconds_to_weekly_reset=3 * 24 * 3600,
            )
            machine = MachineSignal(cores=8, load1=1.0, ram_available_gb=20.0)
            decision = decide_admission(
                quota=quota,
                machine=machine,
                signals=SupplementalAdmissionSignals(yield_signal=YieldSignal(completed=0, failed=5)),
            )
            pressure = pressure_for(quota=quota, machine=machine)
            assert not decision.admit
            assert pressure.band.value == "full"

            with patch("teatree.core.telemetry.admission._provider", return_value=provider):
                for _ in range(3):
                    record_admission_decision(decision=decision, pressure=pressure, lane="headless")

            now = dt.datetime.now(tz=dt.UTC)
            rows = recent_pressure_observations(directory=root, now=now)
            assert {row["cause"] for row in rows} == {"yield-collapse"}
            reports = PressureIncidentDetector(directory=root, now=lambda: now).detect()
            assert len(reports) == 1
            assert reports[0].payload["cause"] == "yield-collapse"
            provider.shutdown()
