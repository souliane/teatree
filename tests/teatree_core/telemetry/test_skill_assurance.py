"""Skill assurance telemetry keeps only bounded, allowlisted attempt facts."""

import datetime as dt
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import teatree.core.telemetry.admission as telemetry
from teatree.core.telemetry.admission import PressureSpanExporter
from teatree.core.telemetry.skill_assurance import SKILL_SPAN_NAME


def test_skill_assurance_emits_safe_span_and_bounded_local_row() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        remote = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter(directory=root)))
        provider.add_span_processor(SimpleSpanProcessor(remote))
        with patch.object(telemetry, "_provider", return_value=provider):
            telemetry.record_skill_assurance(
                task_id=31,
                ticket_id=12,
                attempt_id=44,
                assurance={
                    "status": "missing",
                    "requested": ["t3:rules", "cold-review", "unsafe/path/private", *[f"skill-{i}" for i in range(20)]],
                    "missing": ["cold-review", "unsafe/path/private"],
                    "evidence": "private transcript that must never reach telemetry",
                },
            )

        rows = telemetry.recent_skill_assurance_observations(directory=root, now=dt.datetime.now(tz=dt.UTC))
        assert len(rows) == 1
        row = rows[0]
        assert (row["ticket_id"], row["task_id"], row["attempt_id"], row["status"]) == (12, 31, 44, "missing")
        assert row["requested_count"] == 22
        assert row["missing_count"] == 1
        assert len(row["requested"]) == 16
        assert row["missing"] == ["cold-review"]
        assert "unsafe/path/private" not in str(row)
        assert "unsafe/path/private" not in str(remote.get_finished_spans()[0].attributes)
        assert list(root.glob("skill-*.jsonl"))
        provider.shutdown()


def test_skill_assurance_reader_rejects_extra_and_unsafe_fields() -> None:
    now = dt.datetime.now(tz=dt.UTC)
    with TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / f"skill-{now.date().isoformat()}.jsonl"
        valid = {
            "epoch": int(now.timestamp()),
            "ticket_id": 12,
            "task_id": 31,
            "attempt_id": 44,
            "status": "unverified",
            "requested_count": 1,
            "missing_count": 0,
            "requested": ["t3:rules"],
            "missing": [],
        }
        path.write_text(
            "\n".join(
                json.dumps(row)
                for row in (
                    {**valid, "private": "secret"},
                    {**valid, "missing": ["/private/path"]},
                    valid,
                )
            )
            + "\n"
        )

        assert telemetry.recent_skill_assurance_observations(directory=root, now=now) == [valid]


def test_skill_assurance_reader_ignores_mixed_type_names_and_keeps_following_row() -> None:
    now = dt.datetime.now(tz=dt.UTC)
    with TemporaryDirectory() as directory:
        root = Path(directory)
        valid = {
            "epoch": int(now.timestamp()),
            "ticket_id": 12,
            "task_id": 31,
            "attempt_id": 44,
            "status": "declared",
            "requested_count": 1,
            "missing_count": 0,
            "requested": ["t3:rules"],
            "missing": [],
        }
        (root / f"skill-{now.date().isoformat()}.jsonl").write_text(
            json.dumps({**valid, "requested": ["t3:rules", 42]}) + "\n" + json.dumps(valid) + "\n"
        )

        assert telemetry.recent_skill_assurance_observations(directory=root, now=now) == [valid]


def test_malformed_assurance_does_not_emit_a_span() -> None:
    remote = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(remote))
    with patch.object(telemetry, "_provider", return_value=provider):
        telemetry.record_skill_assurance(
            task_id=31,
            ticket_id=12,
            attempt_id=44,
            assurance={"status": ["missing"], "requested": ["t3:rules"], "missing": []},
        )
    assert remote.get_finished_spans() == ()
    provider.shutdown()


def test_malformed_skill_span_does_not_break_local_exporter() -> None:
    with TemporaryDirectory() as directory:
        remote = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(remote))
        with provider.get_tracer(__name__).start_as_current_span(SKILL_SPAN_NAME) as span:
            span.set_attribute("teatree.skill.requested", 42)

        with patch.object(telemetry, "_provider", return_value=provider):
            telemetry.record_skill_assurance(
                task_id=31,
                ticket_id=12,
                attempt_id=44,
                assurance={"status": "declared", "requested": ["t3:rules"], "missing": []},
            )
        outcome = PressureSpanExporter(directory=Path(directory)).export(remote.get_finished_spans())
        assert outcome is SpanExportResult.SUCCESS
        assert len(telemetry.recent_skill_assurance_observations(directory=Path(directory))) == 1
        provider.shutdown()
