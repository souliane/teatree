"""OpenTelemetry admission and factory spans with bounded local readers.

The local exporter is always on: an optional OTLP endpoint is a second sink, not a
prerequisite for detecting a factory that is repeatedly refusing the same cause.
Only allowlisted attributes reach the shared JSONL ledger or optional OTLP sink.
Configured skill names are intentionally emitted as metadata and must not
contain secrets; prompt bodies, credential values, work paths, and account
identities are not emitted from runtime observations.
"""

import datetime as dt
import hashlib
import json
import logging
import math
import os
import re
import time
from functools import lru_cache, partial
from operator import itemgetter
from pathlib import Path
from typing import TYPE_CHECKING, cast

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult

from teatree.core.telemetry.admission_schema import (
    _ADMISSION_CAUSES,
    _FACTORY_CAUSES,
    _FACTORY_KINDS,
    _LIFECYCLE_CAUSES,
    _LIFECYCLE_KINDS,
    _safe_admission_reason,
    _valid_factory_row,
    _valid_identifier,
    _valid_lifecycle_row,
    _valid_observation,
)
from teatree.core.telemetry.observation_read import ObservationRead, ReadPeriod, read_recent_observations
from teatree.core.telemetry.skill_assurance import emit_skill_assurance, safe_skill_observation, valid_skill_row
from teatree.utils.hook_registry import loop_registry_dir

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from teatree.core.admission_governor import AdmissionDecision
    from teatree.core.admission_pressure import AdmissionPressure
    from teatree.loop.self_improve.detectors.base import DetectorReport

logger = logging.getLogger(__name__)

SPAN_NAME = "teatree.admission.decision"
FACTORY_SPAN_NAME = "teatree.factory.issue"
LIFECYCLE_SPAN_NAME = "teatree.factory.lifecycle"
WINDOW = dt.timedelta(minutes=30)
# Two daily files can be needed for one window. The 32 MiB aggregate read
# limit is split into 16 MiB per daily file: over twice a full day of
# 5-second admissions even with the longest safe reason.
# A larger file remains explicitly incomplete rather than proving false recovery.
READ_LIMIT_BYTES = 32 * 1024 * 1024
RETENTION_DAYS = 7


def _directory() -> Path:
    return loop_registry_dir() / "otel"


def _file_for_prefix(directory: Path, epoch: int, *, prefix: str) -> Path:
    day = dt.datetime.fromtimestamp(epoch, tz=dt.UTC).date()
    return directory / f"{prefix}-{day.isoformat()}.jsonl"


_file_for = partial(_file_for_prefix, prefix="admission")
_factory_file_for = partial(_file_for_prefix, prefix="factory")
_lifecycle_file_for = partial(_file_for_prefix, prefix="lifecycle")
_skill_file_for = partial(_file_for_prefix, prefix="skill")


def _safe_observation(span: ReadableSpan) -> dict[str, str | int | float | bool] | None:
    if span.name != SPAN_NAME:
        return None
    attrs = span.attributes or {}
    cause = attrs.get("teatree.pressure.cause")
    band = attrs.get("teatree.pressure.band")
    pressure = attrs.get("teatree.pressure.value")
    admit = attrs.get("teatree.admission.admit")
    reason = attrs.get("teatree.admission.reason")
    lane = attrs.get("teatree.admission.lane")
    epoch = attrs.get("teatree.observed_epoch")
    if not isinstance(cause, str) or cause not in _ADMISSION_CAUSES:
        return None
    if not isinstance(band, str) or band not in {"full", "degraded", "shed", "halt"}:
        return None
    if not isinstance(pressure, (float, int)) or not math.isfinite(pressure):
        return None
    if not isinstance(admit, bool) or not isinstance(epoch, int) or epoch <= 0:
        return None
    return {
        "epoch": epoch,
        "cause": cause,
        "pressure": float(pressure),
        "band": band,
        "admit": admit,
        "reason": _safe_admission_reason(reason),
        "lane": lane if isinstance(lane, str) and lane in {"loop", "headless", "interactive"} else "unknown",
    }


def _safe_factory_observation(span: ReadableSpan) -> dict[str, str | int] | None:
    attrs = span.attributes or {}
    epoch = attrs.get("teatree.observed_epoch")
    kind = attrs.get("teatree.issue.kind")
    cause = attrs.get("teatree.issue.cause")
    severity = attrs.get("teatree.issue.severity")
    count = attrs.get("teatree.issue.count")
    incident_id = attrs.get("teatree.issue.id")
    if span.name != FACTORY_SPAN_NAME or not isinstance(epoch, int) or epoch <= 0:
        return None
    if (
        not isinstance(kind, str)
        or kind not in _FACTORY_KINDS
        or not isinstance(cause, str)
        or cause not in _FACTORY_CAUSES
    ):
        return None
    if not isinstance(severity, str) or not (
        severity in {"warn", "error", "critical"} or (severity == "info" and kind == "boot" and cause == "ready")
    ):
        return None
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return None
    if not isinstance(incident_id, str) or re.fullmatch(r"[0-9a-f]{16}", incident_id) is None:
        return None
    return {
        "epoch": epoch,
        "kind": kind,
        "cause": cause,
        "severity": severity,
        "count": count,
        "incident_id": incident_id,
    }


def _safe_lifecycle_observation(span: ReadableSpan) -> dict[str, str | int] | None:
    attrs = span.attributes or {}
    epoch = attrs.get("teatree.observed_epoch")
    kind = attrs.get("teatree.lifecycle.kind")
    entity_id = attrs.get("teatree.lifecycle.entity_id")
    ticket_id = attrs.get("teatree.lifecycle.ticket_id")
    task_id = attrs.get("teatree.lifecycle.task_id")
    cause = attrs.get("teatree.lifecycle.cause")
    if span.name != LIFECYCLE_SPAN_NAME or not isinstance(epoch, int) or epoch <= 0:
        return None
    if not isinstance(kind, str) or kind not in _LIFECYCLE_KINDS:
        return None
    if not all(
        (
            _valid_identifier(entity_id),
            _valid_identifier(ticket_id, allow_zero=True),
            _valid_identifier(task_id, allow_zero=True),
        )
    ):
        return None
    if not isinstance(cause, str) or cause not in _LIFECYCLE_CAUSES:
        return None
    return {
        "epoch": epoch,
        "kind": kind,
        "entity_id": cast("int", entity_id),
        "ticket_id": cast("int", ticket_id),
        "task_id": cast("int", task_id),
        "cause": cause,
    }


class PressureSpanExporter(SpanExporter):
    """Persist decision spans on the runtime's shared volume, one day per file."""

    def __init__(self, *, directory: Path | None = None) -> None:
        self.directory = directory
        self._last_pruned_day: dt.date | None = None

    def export(self, spans: "Sequence[ReadableSpan]") -> SpanExportResult:
        directory = self.directory or _directory()
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            for span in spans:
                row = _safe_observation(span)
                file_for = _file_for
                if row is None:
                    row = _safe_factory_observation(span)
                    file_for = _factory_file_for
                if row is None:
                    row = _safe_lifecycle_observation(span)
                    file_for = _lifecycle_file_for
                if row is None:
                    row = safe_skill_observation(span)
                    file_for = _skill_file_for
                if row is None:
                    continue
                payload = (json.dumps(row, separators=(",", ":")) + "\n").encode()
                fd = os.open(file_for(directory, int(row["epoch"])), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    if os.write(fd, payload) != len(payload):
                        raise OSError
                finally:
                    os.close(fd)
            self._prune(directory)
        except OSError:
            logger.exception("admission telemetry local export failed")
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def _prune(self, directory: Path) -> None:
        today = dt.datetime.now(tz=dt.UTC).date()
        if self._last_pruned_day == today:
            return
        cutoff = today - dt.timedelta(days=RETENTION_DAYS)
        for path in (
            *directory.glob("admission-????-??-??.jsonl"),
            *directory.glob("factory-????-??-??.jsonl"),
            *directory.glob("lifecycle-????-??-??.jsonl"),
            *directory.glob("skill-????-??-??.jsonl"),
        ):
            try:
                day = dt.date.fromisoformat(path.stem.split("-", maxsplit=1)[1])
                if day < cutoff:
                    path.unlink()
            except (OSError, ValueError):
                logger.warning("could not prune stale admission telemetry file %s", path)
        self._last_pruned_day = today


def checked_pressure_observations(
    *, directory: Path | None = None, now: dt.datetime | None = None, window: dt.timedelta = WINDOW
) -> ObservationRead:
    return read_recent_observations(
        directory=directory or _directory(),
        prefix="admission",
        validator=_valid_observation,
        period=ReadPeriod(now or dt.datetime.now(tz=dt.UTC), window),
        tail_limit=READ_LIMIT_BYTES // 2,
    )


def recent_pressure_observations(
    *, directory: Path | None = None, now: dt.datetime | None = None, window: dt.timedelta = WINDOW
) -> list[dict]:
    return checked_pressure_observations(directory=directory, now=now, window=window).rows


def latest_admission_reason(*, directory: Path | None = None, now: dt.datetime | None = None) -> str | None:
    """Explain a stalled queue from the latest local admission span, if recent."""
    rows = recent_pressure_observations(directory=directory, now=now, window=dt.timedelta(days=1))
    decisions = [row for row in rows if row.get("lane") in {"loop", "headless"} and row.get("reason")]
    return str(max(decisions, key=itemgetter("epoch"))["reason"]) if decisions else None


def checked_factory_observations(
    *, directory: Path | None = None, now: dt.datetime | None = None, window: dt.timedelta = WINDOW
) -> ObservationRead:
    return read_recent_observations(
        directory=directory or _directory(),
        prefix="factory",
        validator=_valid_factory_row,
        period=ReadPeriod(now or dt.datetime.now(tz=dt.UTC), window),
        tail_limit=READ_LIMIT_BYTES // 2,
    )


def recent_factory_observations(
    *, directory: Path | None = None, now: dt.datetime | None = None, window: dt.timedelta = WINDOW
) -> list[dict]:
    return checked_factory_observations(directory=directory, now=now, window=window).rows


def checked_lifecycle_observations(
    *, directory: Path | None = None, now: dt.datetime | None = None, window: dt.timedelta = WINDOW
) -> ObservationRead:
    return read_recent_observations(
        directory=directory or _directory(),
        prefix="lifecycle",
        validator=_valid_lifecycle_row,
        period=ReadPeriod(now or dt.datetime.now(tz=dt.UTC), window),
        tail_limit=READ_LIMIT_BYTES // 2,
    )


def recent_lifecycle_observations(
    *, directory: Path | None = None, now: dt.datetime | None = None, window: dt.timedelta = WINDOW
) -> list[dict]:
    return checked_lifecycle_observations(directory=directory, now=now, window=window).rows


def checked_skill_assurance_observations(
    *, directory: Path | None = None, now: dt.datetime | None = None, window: dt.timedelta = WINDOW
) -> ObservationRead:
    return read_recent_observations(
        directory=directory or _directory(),
        prefix="skill",
        validator=valid_skill_row,
        period=ReadPeriod(now or dt.datetime.now(tz=dt.UTC), window),
        tail_limit=READ_LIMIT_BYTES // 2,
    )


def recent_skill_assurance_observations(
    *, directory: Path | None = None, now: dt.datetime | None = None, window: dt.timedelta = WINDOW
) -> list[dict]:
    return checked_skill_assurance_observations(directory=directory, now=now, window=window).rows


@lru_cache(maxsize=1)
def _provider() -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": "teatree-factory"}))
    provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter()))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: PLC0415

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider


class LocalSpanRecorder:
    """Group the four bounded factory emission paths without changing callers' API."""

    @staticmethod
    def record_admission_decision(*, decision: "AdmissionDecision", pressure: "AdmissionPressure", lane: str) -> None:
        """Emit exactly the pressure scalar and outcome the admission decision used."""
        try:
            raw_cause = getattr(decision, "cause", "") or (pressure.dominant.name if pressure.dominant else "unknown")
            cause = raw_cause if isinstance(raw_cause, str) and raw_cause in _ADMISSION_CAUSES else "unknown"
            safe_lane = lane if lane in {"loop", "headless", "interactive"} else "unknown"
            value = pressure.value
            band = pressure.band.value
            invalid_pressure = (
                not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
            )
            invalid_decision = (
                band not in {"full", "degraded", "shed", "halt"}
                or not isinstance(decision.admit, bool)
                or not isinstance(decision.ceiling, int)
                or isinstance(decision.ceiling, bool)
                or decision.ceiling < 0
            )
            if invalid_pressure or invalid_decision:
                logger.warning("admission telemetry rejected invalid decision fields")
                return
            with _provider().get_tracer(__name__).start_as_current_span(SPAN_NAME) as span:
                span.set_attribute("teatree.pressure.cause", cause)
                span.set_attribute("teatree.pressure.value", value)
                span.set_attribute("teatree.pressure.band", band)
                span.set_attribute("teatree.admission.admit", decision.admit)
                span.set_attribute("teatree.admission.ceiling", decision.ceiling)
                span.set_attribute("teatree.admission.lane", safe_lane)
                span.set_attribute("teatree.admission.reason", _safe_admission_reason(decision.reason))
                span.set_attribute("teatree.observed_epoch", int(time.time()))
        except Exception as exc:  # noqa: BLE001 — telemetry cannot change the admission verdict
            logger.warning("admission telemetry emission failed (%s); decision unaffected", type(exc).__name__)

    @staticmethod
    def record_factory_issue(report: "DetectorReport") -> None:
        try:
            raw_kind = report.payload.get("kind") or report.detector
            raw_cause = report.payload.get("cause") or "unknown"
            kind = raw_kind if isinstance(raw_kind, str) and raw_kind in _FACTORY_KINDS else "unknown"
            cause = raw_cause if isinstance(raw_cause, str) and raw_cause in _FACTORY_CAUSES else "unknown"
            count = report.payload.get("count", 1)
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                count = 1
            severity = report.severity if report.severity in {"warn", "error", "critical"} else "warn"
            with _provider().get_tracer(__name__).start_as_current_span(FACTORY_SPAN_NAME) as span:
                span.set_attribute("teatree.issue.kind", kind)
                span.set_attribute("teatree.issue.cause", cause)
                span.set_attribute("teatree.issue.severity", severity)
                span.set_attribute("teatree.issue.count", count)
                span.set_attribute("teatree.issue.id", hashlib.sha256(report.dedup_key.encode()).hexdigest()[:16])
                span.set_attribute("teatree.observed_epoch", int(time.time()))
        except Exception as exc:  # noqa: BLE001 — monitoring must not prevent the incident action
            logger.warning("factory issue telemetry emission failed (%s); action unaffected", type(exc).__name__)

    @staticmethod
    def record_lifecycle_transition(
        *, kind: str, entity_id: int, ticket_id: int = 0, task_id: int = 0, cause: str = "none"
    ) -> None:
        if kind not in _LIFECYCLE_KINDS or not all(
            (
                _valid_identifier(entity_id),
                _valid_identifier(ticket_id, allow_zero=True),
                _valid_identifier(task_id, allow_zero=True),
            )
        ):
            logger.warning("factory lifecycle telemetry rejected an invalid event")
            return
        try:
            with _provider().get_tracer(__name__).start_as_current_span(LIFECYCLE_SPAN_NAME) as span:
                span.set_attribute("teatree.lifecycle.kind", kind)
                span.set_attribute("teatree.lifecycle.entity_id", entity_id)
                span.set_attribute("teatree.lifecycle.ticket_id", ticket_id)
                span.set_attribute("teatree.lifecycle.task_id", task_id)
                safe_cause = cause if isinstance(cause, str) and cause in _LIFECYCLE_CAUSES else "unknown"
                span.set_attribute("teatree.lifecycle.cause", safe_cause)
                span.set_attribute("teatree.observed_epoch", int(time.time()))
        except Exception as exc:  # noqa: BLE001 — observability cannot change a committed lifecycle transition
            logger.warning(
                "factory lifecycle telemetry emission failed (%s); transition unaffected", type(exc).__name__
            )

    @staticmethod
    def record_skill_assurance(
        *, task_id: int, ticket_id: int, attempt_id: int, assurance: "Mapping[str, object]"
    ) -> None:
        try:
            if not emit_skill_assurance(
                _provider, task_id=task_id, ticket_id=ticket_id, attempt_id=attempt_id, assurance=assurance
            ):
                logger.warning("factory skill assurance telemetry rejected invalid identifiers or status")
        except Exception as exc:  # noqa: BLE001 — telemetry cannot change a recorded attempt
            logger.warning(
                "factory skill assurance telemetry emission failed (%s); attempt unaffected", type(exc).__name__
            )


record_admission_decision = LocalSpanRecorder.record_admission_decision
record_factory_issue = LocalSpanRecorder.record_factory_issue
record_lifecycle_transition = LocalSpanRecorder.record_lifecycle_transition
record_skill_assurance = LocalSpanRecorder.record_skill_assurance


__all__ = [
    "PressureSpanExporter",
    "checked_factory_observations",
    "checked_lifecycle_observations",
    "checked_pressure_observations",
    "checked_skill_assurance_observations",
    "latest_admission_reason",
    "recent_factory_observations",
    "recent_lifecycle_observations",
    "recent_pressure_observations",
    "recent_skill_assurance_observations",
    "record_admission_decision",
    "record_factory_issue",
    "record_lifecycle_transition",
    "record_skill_assurance",
]
