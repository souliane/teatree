"""Allowlisted fields and validators shared by OTel export and local reads."""

import math
import re

from teatree.core.modelkit.task_failure_taxonomy import FailureKind

_LIFECYCLE_KINDS = frozenset(
    {
        "task.claimed",
        "task.completed",
        "task.failed",
        "attempt.finished",
        "message.received",
        "message.answered",
        "question.recorded",
        "question.mirrored",
        "question.answered",
        "question.dismissed",
    }
)
_ADMISSION_CAUSES = frozenset(
    {
        "accounts-exhausted",
        "weekly-quota",
        "5h-quota",
        "weekly-pace",
        "load",
        "memory",
        "swap",
        "yield-collapse",
        "metered-lane-parked",
        "metered-spend",
        "unknown",
    }
)
_FACTORY_KINDS = frozenset(
    {
        "boot",
        "dispatch_gap",
        "forgotten_merge",
        "stale_statusline_entry",
        "pressure_incident",
        "task_failed",
        "repair_task_failed",
        "attempt_failure_burst",
        "task_stalled",
        "repair_task_stalled",
        "inbound_unanswered",
        "outbound_unposted",
        "outbound_unanswered",
        "skill_assurance",
        "telemetry_action_gap",
        "cgroup_probe_inert",
        "ram_probe_inert",
        "disk_probe_inert",
        "unknown",
    }
)
_LIFECYCLE_CAUSES = frozenset({"none", "success", "unknown", *(kind.value for kind in FailureKind)})
_FACTORY_CAUSES = (
    _ADMISSION_CAUSES
    | _LIFECYCLE_CAUSES
    | frozenset(
        {
            "expired_lease_no_heartbeat",
            "message_without_confirmed_response",
            "question_delivery_gap",
            "awaiting_user_reply",
            "cgroup-memory-unreadable",
            "ram-unreadable",
            "disk-unreadable",
            "skill-missing",
            "skill-delivery-gap",
            "application-unverified",
            "unactioned-issue",
            "ready",
            "command-failed",
            "setup-failed",
            "missing-skills",
        }
    )
)
_FACTORY_FIELDS = frozenset({"epoch", "kind", "cause", "severity", "count", "incident_id"})
_LIFECYCLE_FIELDS = frozenset({"epoch", "kind", "entity_id", "ticket_id", "task_id", "cause"})
_REASON_NUMBER = r"\d+(?:\.\d+)?(?:e[+-]?\d+)?"
# Only the governor's fixed, numeric templates may leave the process. An
# arbitrary decision.reason must never become a remote OTLP span attribute.
_SAFE_ADMISSION_REASONS = tuple(
    re.compile(pattern)
    for pattern in (
        r"every account is quota-exhausted — retrying into a rate limit is pure burn",
        r"weekly window spent \(\d+%\) — no budget left to admit against",
        r"5h window spent \(\d+%\) — a hard rate limit is imminent",
        rf"weekly burn outruns the reset \(pace {_REASON_NUMBER}\) — pacing to the window",
        rf"load {_REASON_NUMBER} at/over the {_REASON_NUMBER} watermark on \d+ core\(s\)",
        (
            rf"{_REASON_NUMBER} GB available at/under the {_REASON_NUMBER} GB watermark"
            r"(?: \(impossible admission ceiling: the cgroup memory cap is "
            rf"{_REASON_NUMBER} GiB but a braked governor only re-admits above "
            rf"RAM_RESUME_FLOOR_GB={_REASON_NUMBER} GiB, so once braked this lane can NEVER resume\. "
            r"Raise the container mem_limit above the resume floor \(or lower the floor\)\.\))?"
        ),
        r"host swap \d+% at/over the \d+% watermark",
        r"admitting up to \d+ — signals healthy",
        r"yield collapsed \(\d+/\d+ terminal tasks completed\) — the marginal token is buying zero",
        (
            r"metered lane spent \d{1,3}(?:,\d{3})* tokens of the \d{1,3}(?:,\d{3})* ceiling "
            r"over the last \d+h \(~\$\d+\.\d{2} ESTIMATED \(price-table arithmetic, not a billed amount\)\)"
            r"(?: — the figure is a FLOOR: \d+ attempt\(s\) in this window recorded UNKNOWN usage)?"
        ),
        (
            r"the metered lane is parked on a "
            r"(?:subscription_session|subscription_weekly|rate_limit|api_credit|provider_budget) window"
            r"(?: with no recorded reset| until \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
            r"(?:\.\d{1,6})?(?:[+-]\d{2}:\d{2})?) — re-probing it is pure burn"
        ),
    )
)


def _safe_admission_reason(value: object) -> str:
    if isinstance(value, str) and any(pattern.fullmatch(value) for pattern in _SAFE_ADMISSION_REASONS):
        return value
    return "unknown"


def _valid_identifier(value: object, *, allow_zero: bool = False) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and (value >= 0 if allow_zero else value > 0)


def _valid_factory_row(row: object, *, cutoff: int, current: int) -> bool:
    if not isinstance(row, dict) or row.keys() != _FACTORY_FIELDS:
        return False
    epoch = row["epoch"]
    count = row["count"]
    return (
        isinstance(epoch, int)
        and not isinstance(epoch, bool)
        and cutoff <= epoch <= current
        and isinstance(row["kind"], str)
        and row["kind"] in _FACTORY_KINDS
        and isinstance(row["cause"], str)
        and row["cause"] in _FACTORY_CAUSES
        and isinstance(row["severity"], str)
        and (
            row["severity"] in {"warn", "error", "critical"}
            or (row["severity"] == "info" and row["kind"] == "boot" and row["cause"] == "ready")
        )
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 1
        and isinstance(row["incident_id"], str)
        and re.fullmatch(r"[0-9a-f]{16}", row["incident_id"]) is not None
    )


def _valid_lifecycle_row(row: object, *, cutoff: int, current: int) -> bool:
    if not isinstance(row, dict) or row.keys() != _LIFECYCLE_FIELDS:
        return False
    epoch = row["epoch"]
    entity_id = row["entity_id"]
    ticket_id = row["ticket_id"]
    task_id = row["task_id"]
    return (
        isinstance(epoch, int)
        and not isinstance(epoch, bool)
        and cutoff <= epoch <= current
        and isinstance(row["kind"], str)
        and row["kind"] in _LIFECYCLE_KINDS
        and _valid_identifier(entity_id)
        and _valid_identifier(ticket_id, allow_zero=True)
        and _valid_identifier(task_id, allow_zero=True)
        and isinstance(row["cause"], str)
        and row["cause"] in _LIFECYCLE_CAUSES
    )


def _valid_observation(row: object, *, cutoff: int, current: int) -> bool:
    if not isinstance(row, dict) or row.keys() != {"epoch", "cause", "pressure", "band", "admit", "reason", "lane"}:
        return False
    epoch = row.get("epoch")
    cause = row.get("cause")
    pressure = row.get("pressure")
    band = row.get("band")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or not cutoff <= epoch <= current:
        return False
    lane = row["lane"]
    reason = row["reason"]
    valid_cause = isinstance(cause, str) and cause in _ADMISSION_CAUSES
    valid_band = isinstance(band, str) and band in {"full", "degraded", "shed", "halt"}
    valid_lane = isinstance(lane, str) and lane in {"loop", "headless", "interactive", "unknown"}
    valid_reason = isinstance(reason, str) and _safe_admission_reason(reason) == reason
    if not all((valid_cause, valid_band, valid_lane, valid_reason)):
        return False
    if not isinstance(pressure, (float, int)) or isinstance(pressure, bool):
        return False
    try:
        return math.isfinite(pressure) and isinstance(row.get("admit"), bool)
    except OverflowError:
        return False
