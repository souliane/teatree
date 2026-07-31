"""What a FAILED ``ConfigSetting`` override read resolves to, and where the fault is visible (#3873).

The resolver's DB override tier can fail at runtime — a contended SQLite lock, an
exhausted file handle, a full disk. Returning ``{}`` for that failure is the defect this
module exists to close: ``{}`` is also what a healthy read of an empty table returns, so
"there is no override" and "I could not determine whether there is an override" arrive at
every call site as the same answer, and every gate resolves against a SHIPPED default.

That is not a conservative fallback. Two of the shipped defaults are the MOST permissive
value the setting has — ``autonomy = full`` and ``mode = auto`` — so an operator who
deliberately stored restraint has it silently upgraded to full autonomy by a read that
merely failed. :data:`SAFETY_FAIL_CLOSED_STORED_VALUES` is the answer: while the override
tier is degraded, the gates that restrain autonomous behaviour resolve to their most
restrictive value rather than to whatever the shipped file happens to say.

Deliberately NOT everything. Only the settings whose permissive direction lets the factory
act without a human are pinned; a degraded read must not also stop the box doing harmless
work, or a transient lock becomes an outage. Env (``T3_*``) is process state the failed
read cannot have touched, so an env-supplied value is readable operator intent and keeps
winning — see ``resolution.get_effective_settings``.

The fault RECORD is a file, not a row. The store that failed is the DB, so recording the
degradation there is the one place guaranteed not to work; the marker sits beside the
primary control DB, where ``t3 doctor`` and the operator can both find it without reading
a worker log.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_logger = logging.getLogger("teatree.config")

#: The most restrictive STORED-form value of each setting that gates autonomous action.
#: Stored form (what a ``ConfigSetting`` row holds), not the coerced dataclass form, so it
#: goes through the SAME registry parsers a real row does and cannot drift into a type the
#: resolver would reject.
#:
#: Each entry earns its place by having a permissive direction the factory can act on:
#:
#: * ``autonomy`` — ships ``full``; ``babysit`` is the tier that keeps every approval gate.
#: * ``mode`` — ships ``auto``; ``interactive`` gates publishing on explicit approval.
#: * ``require_human_approval_to_merge`` / ``_to_answer`` — the two named human controls.
#: * ``on_behalf_post_mode`` — ``draft_or_ask`` never posts as the user unprompted.
SAFETY_FAIL_CLOSED_STORED_VALUES: dict[str, Any] = {
    "autonomy": "babysit",
    "mode": "interactive",
    "require_human_approval_to_merge": True,
    "require_human_approval_to_answer": True,
    "on_behalf_post_mode": "draft_or_ask",
}

#: The marker filename, beside the primary control DB.
MARKER_FILENAME = "config-read-degraded.json"

#: A marker older than this is stale — the fault it recorded is not evidence about now.
#: Sized so a doctor run within the hour still surfaces an overnight degradation.
MARKER_TTL_SECONDS = 24 * 60 * 60


class ConfigOverrideReadError(RuntimeError):
    """Raised where "I could not read the override tier" must NOT resolve to a value.

    Most callers want a value and get the fail-closed one. A caller that PERSISTS the
    resolved tiers (a TOML export) is the exception: writing an absence it never verified
    turns a transient read fault into permanent config loss, so it must fail instead.
    """

    def __init__(self, scope: str) -> None:
        super().__init__(
            f"the ConfigSetting override tier for scope {scope or '(global)'!r} could not be read, "
            "so the stored settings are unknown — refusing to write a file that would record "
            "them as absent. Re-run once `t3 doctor check` reports the config tier healthy."
        )
        self.scope = scope


@dataclass(frozen=True, slots=True)
class DegradedReadReport:
    """A live record that the ``ConfigSetting`` override tier failed to resolve."""

    scopes: tuple[str, ...]
    occurrences: int
    first_seen: float
    last_seen: float

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.last_seen)


def marker_path() -> Path:
    """The marker file beside the PRIMARY control DB.

    Resolved at call time (never at import): this module sits under the cold-hook import
    chain, and ``ControlDb`` consults the environment, so a test pointing ``T3_CONFIG_DB``
    elsewhere must be honoured for the read that follows it rather than for the process
    that imported us.
    """
    from teatree.paths import ControlDb  # noqa: PLC0415 — deferred: env-sensitive path resolution at call time

    return ControlDb(os.environ).primary().parent / MARKER_FILENAME


def record_degraded_read(scope: str) -> None:
    """Record that *scope*'s override read failed, merging into any live marker.

    Never raises: the caller is a settings resolution that must still return a value, and
    a marker that cannot be written must not become a second outage. A write failure is
    logged, so the log remains the backstop when the filesystem is the problem too.
    """
    now = time.time()
    try:
        existing = _read_marker()
        scopes = tuple(sorted({*(existing.scopes if existing else ()), scope}))
        payload = {
            "scopes": list(scopes),
            "occurrences": (existing.occurrences if existing else 0) + 1,
            "first_seen": existing.first_seen if existing else now,
            "last_seen": now,
        }
        path = marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        _logger.exception(
            "ConfigSetting override read degraded for scope %r AND the degradation marker "
            "could not be written — this fault is visible only in this log.",
            scope,
        )


def degraded_read_report() -> DegradedReadReport | None:
    """The live degradation record, or ``None`` when there is none (or it is stale).

    Fails open to ``None`` on an unreadable/corrupt marker: an operator-facing health
    check must not itself become a failure, and a marker teatree cannot parse is not
    evidence that the config tier is degraded.
    """
    report = _read_marker()
    if report is None or report.age_seconds > MARKER_TTL_SECONDS:
        return None
    return report


def clear_degraded_read() -> None:
    """Drop the marker — the operator has acknowledged/repaired the fault."""
    try:
        marker_path().unlink(missing_ok=True)
    except OSError:
        _logger.exception("could not clear the ConfigSetting degraded-read marker")


def _read_marker() -> DegradedReadReport | None:
    try:
        raw = json.loads(marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    scopes = raw.get("scopes")
    occurrences = raw.get("occurrences")
    first_seen = raw.get("first_seen")
    last_seen = raw.get("last_seen")
    if not isinstance(scopes, list) or not isinstance(occurrences, int):
        return None
    if not isinstance(first_seen, int | float) or not isinstance(last_seen, int | float):
        return None
    return DegradedReadReport(
        scopes=tuple(str(scope) for scope in scopes),
        occurrences=occurrences,
        first_seen=float(first_seen),
        last_seen=float(last_seen),
    )


__all__ = [
    "MARKER_FILENAME",
    "MARKER_TTL_SECONDS",
    "SAFETY_FAIL_CLOSED_STORED_VALUES",
    "ConfigOverrideReadError",
    "DegradedReadReport",
    "clear_degraded_read",
    "degraded_read_report",
    "marker_path",
    "record_degraded_read",
]
