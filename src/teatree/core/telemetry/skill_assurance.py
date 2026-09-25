"""Bounded, privacy-safe skill-assurance span and local-row schema."""

import re
import time
from collections.abc import Callable, Mapping, Sequence

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider

SKILL_SPAN_NAME = "teatree.factory.skill_assurance"
SKILL_STATUSES = frozenset({"missing", "injection_gap", "unverified", "declared"})
SKILL_NAME_LIMIT = 16
_SAFE_SKILL_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_:-]{0,79}$")
_ROW_FIELDS = frozenset(
    {
        "epoch",
        "ticket_id",
        "task_id",
        "attempt_id",
        "status",
        "requested_count",
        "missing_count",
        "requested",
        "missing",
    }
)


def safe_skill_names(values: Sequence[object]) -> list[str]:
    return sorted({name for name in values if isinstance(name, str) and _SAFE_SKILL_NAME.fullmatch(name)})


def _valid_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_name_list(names: object, count: object) -> bool:
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return False
    if not isinstance(names, list) or len(names) > SKILL_NAME_LIMIT:
        return False
    if any(not isinstance(name, str) or _SAFE_SKILL_NAME.fullmatch(name) is None for name in names):
        return False
    return names == sorted(set(names)) and count >= len(names)


def valid_skill_row(row: object, *, cutoff: int, current: int) -> bool:
    if not isinstance(row, dict) or row.keys() != _ROW_FIELDS:
        return False
    epoch = row["epoch"]
    return (
        isinstance(epoch, int)
        and not isinstance(epoch, bool)
        and cutoff <= epoch <= current
        and all(_valid_id(row[key]) for key in ("ticket_id", "task_id", "attempt_id"))
        and isinstance(row["status"], str)
        and row["status"] in SKILL_STATUSES
        and _valid_name_list(row["requested"], row["requested_count"])
        and _valid_name_list(row["missing"], row["missing_count"])
    )


def safe_skill_observation(span: ReadableSpan) -> dict | None:
    if span.name != SKILL_SPAN_NAME:
        return None
    attrs = span.attributes or {}
    requested = attrs.get("teatree.skill.requested", ())
    missing = attrs.get("teatree.skill.missing", ())
    if not isinstance(requested, (list, tuple)) or not isinstance(missing, (list, tuple)):
        return None
    if len(requested) > SKILL_NAME_LIMIT or len(missing) > SKILL_NAME_LIMIT:
        return None
    row = {
        "epoch": attrs.get("teatree.observed_epoch"),
        "ticket_id": attrs.get("teatree.skill.ticket_id"),
        "task_id": attrs.get("teatree.skill.task_id"),
        "attempt_id": attrs.get("teatree.skill.attempt_id"),
        "status": attrs.get("teatree.skill.status"),
        "requested_count": attrs.get("teatree.skill.requested_count"),
        "missing_count": attrs.get("teatree.skill.missing_count"),
        "requested": list(requested),
        "missing": list(missing),
    }
    return row if valid_skill_row(row, cutoff=1, current=int(time.time())) else None


def emit_skill_assurance(
    provider_factory: Callable[[], TracerProvider],
    *,
    task_id: int,
    ticket_id: int,
    attempt_id: int,
    assurance: Mapping[str, object],
) -> bool:
    status = assurance.get("status")
    requested = assurance.get("requested")
    missing = assurance.get("missing")
    if (
        not isinstance(status, str)
        or status not in SKILL_STATUSES
        or not all(_valid_id(identifier) for identifier in (task_id, ticket_id, attempt_id))
        or not isinstance(requested, (list, tuple))
        or not isinstance(missing, (list, tuple))
    ):
        return False
    safe_requested = safe_skill_names(requested)
    safe_missing = safe_skill_names(missing)
    with provider_factory().get_tracer(__name__).start_as_current_span(SKILL_SPAN_NAME) as span:
        span.set_attribute("teatree.skill.ticket_id", ticket_id)
        span.set_attribute("teatree.skill.task_id", task_id)
        span.set_attribute("teatree.skill.attempt_id", attempt_id)
        span.set_attribute("teatree.skill.status", status)
        span.set_attribute("teatree.skill.requested_count", len(safe_requested))
        span.set_attribute("teatree.skill.missing_count", len(safe_missing))
        span.set_attribute("teatree.skill.requested", safe_requested[:SKILL_NAME_LIMIT])
        span.set_attribute("teatree.skill.missing", safe_missing[:SKILL_NAME_LIMIT])
        span.set_attribute("teatree.observed_epoch", int(time.time()))
    return True
