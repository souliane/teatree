"""Bounded OTel lifecycle and gate evidence."""

import datetime as dt
import json
from unittest.mock import patch

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import INVALID_SPAN_CONTEXT

import teatree.core.telemetry.admission as telemetry
from hooks.scripts import hook_router


def test_hook_gate_row_round_trips_through_telemetry_reader(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
    context = ("PreToolUse", {"session_id": "gate-reader"})

    assert hook_router._write_pretooluse_deny("BLOCKED: private", gate_id="plan_gate", context=context)
    assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    rows = telemetry.checked_gate_observations(directory=tmp_path / "otel", now=dt.datetime.now(tz=dt.UTC)).rows
    assert len(rows) == 1
    assert (rows[0]["gate"], rows[0]["rule"], rows[0]["decision"]) == ("plan_gate", "blocked", "deny")


def test_finished_attempt_exports_only_allowlisted_fields_with_its_span_context(tmp_path) -> None:
    provider = TracerProvider()
    remote = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(telemetry.PressureSpanExporter(directory=tmp_path)))
    provider.add_span_processor(SimpleSpanProcessor(remote))
    with patch.object(telemetry, "_provider", return_value=provider):
        telemetry.record_lifecycle_event(
            telemetry.LifecycleEvent(
                kind="attempt.finished",
                entity_id=13,
                ticket_id=2,
                task_id=3,
                cause="unknown",
                iteration=2,
                error_fingerprint="a" * 64,
                session_ref="b" * 16,
            )
        )
    now = dt.datetime.now(tz=dt.UTC)
    row = telemetry.checked_lifecycle_observations(directory=tmp_path, now=now).rows[0]
    span = remote.get_finished_spans()[0]
    context = span.get_span_context()
    assert (row["trace_id"], row["span_id"]) == (f"{context.trace_id:032x}", f"{context.span_id:016x}")
    assert (row["iteration"], row["error_fingerprint"], row["session_ref"]) == (2, "a" * 64, "b" * 16)
    assert set(span.attributes) == {
        "teatree.lifecycle.kind",
        "teatree.lifecycle.entity_id",
        "teatree.lifecycle.ticket_id",
        "teatree.lifecycle.task_id",
        "teatree.lifecycle.cause",
        "teatree.lifecycle.iteration",
        "teatree.lifecycle.error_fingerprint",
        "teatree.lifecycle.session_ref",
        "teatree.observed_epoch",
    }
    path = tmp_path / f"lifecycle-{now.date()}.jsonl"
    path.write_text(json.dumps(row | {"kind": "task.claimed"}) + "\n")
    assert telemetry.checked_lifecycle_observations(directory=tmp_path, now=now).reason == "otel_malformed"
    provider.shutdown()


def _record(tmp_path, event: "telemetry.LifecycleEvent") -> InMemorySpanExporter:
    provider = TracerProvider()
    remote = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(telemetry.PressureSpanExporter(directory=tmp_path)))
    provider.add_span_processor(SimpleSpanProcessor(remote))
    with patch.object(telemetry, "_provider", return_value=provider):
        telemetry.record_lifecycle_event(event)
    provider.shutdown()
    return remote


def test_only_a_finished_attempt_carries_attempt_evidence(tmp_path) -> None:
    event = telemetry.LifecycleEvent(
        kind="task.claimed", entity_id=13, ticket_id=2, task_id=3, iteration=2, error_fingerprint="a" * 64
    )

    span = _record(tmp_path, event).get_finished_spans()[0]

    assert not {
        name for name in span.attributes or {} if name.endswith(("iteration", "error_fingerprint", "session_ref"))
    }


def test_a_span_without_a_valid_context_keeps_its_row_without_ids(tmp_path) -> None:
    event = telemetry.LifecycleEvent(kind="task.failed", entity_id=13, ticket_id=2, task_id=3, cause="unknown")
    recorded = _record(tmp_path / "recorded", event).get_finished_spans()[0]
    span = ReadableSpan(name=recorded.name, context=INVALID_SPAN_CONTEXT, attributes=recorded.attributes)

    telemetry.PressureSpanExporter(directory=tmp_path).export([span])

    rows = telemetry.checked_lifecycle_observations(directory=tmp_path, now=dt.datetime.now(tz=dt.UTC)).rows
    assert [(row["kind"], row["entity_id"], "trace_id" in row) for row in rows] == [("task.failed", 13, False)]


def test_the_exporter_leaves_gate_files_to_the_hook_that_owns_their_retention(tmp_path) -> None:
    old_day = dt.datetime.now(tz=dt.UTC).date() - dt.timedelta(days=30)
    gate_file = tmp_path / f"gate-{old_day.isoformat()}.jsonl"
    lifecycle_file = tmp_path / f"lifecycle-{old_day.isoformat()}.jsonl"
    gate_file.write_text("")
    lifecycle_file.write_text("")

    _record(tmp_path, telemetry.LifecycleEvent(kind="task.failed", entity_id=13, ticket_id=2, task_id=3))

    assert gate_file.exists()
    assert not lifecycle_file.exists()
