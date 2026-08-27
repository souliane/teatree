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

Beside the control DB is not always WRITABLE, though, and that gap made the record useless
in the venue that needs it most (#4041). The canonical path is inside the container's
control-DB volume, so a HOST process — a hook, a statusline's ``t3`` call — observes the
fault and then cannot create ``/var/lib/teatree`` to record it. The recorder caught the
``OSError`` and logged that the fault "is visible only in this log": a health marker
conceding it cannot do its job in the exact place the job exists. :func:`marker_paths`
closes that — when the canonical directory is not writable HERE, a per-user location under
this venue's own data dir is offered as well, so the record lands where the fault was seen.
It is offered ONLY on that condition, because an unconditional second location would let a
stale per-user marker outvote a healthy canonical venue.
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

#: How many DISTINCT calling contexts the marker keeps. A degraded read repeats at whatever rate
#: its caller runs at, so the record has to be bounded; the callers are what identifies the fault,
#: and a handful is already more than one fix needs.
MAX_RECORDED_CALLERS = 5


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
    """A live record that the ``ConfigSetting`` override tier failed to resolve.

    ``callers`` names the call sites the failing reads came from (#3980). The traceback the
    reader captures holds only the ORM frames, which are identical for every fault; the caller
    is the one fact that makes the record actionable, so it travels with it. Empty for a marker
    written before the field existed.

    ``path`` is the file this record was actually read from, so an operator told to delete the
    marker is told the one that exists rather than the canonical path a fallback venue never
    wrote to. ``None`` for a report built without reading a file.
    """

    scopes: tuple[str, ...]
    occurrences: int
    first_seen: float
    last_seen: float
    callers: tuple[str, ...] = ()
    path: Path | None = None

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


def fallback_marker_path() -> Path:
    """Where the marker goes for a venue that cannot write beside the control DB.

    This venue's OWN data dir — the same root the host projection is published into, so a
    host that can read teatree's data can read teatree's health record. Deliberately reuses
    ``data_dir_root`` rather than minting a second root: it already resolves ``T3_DATA_DIR``
    then the XDG data home per call, which is exactly "somewhere this process can write".
    """
    from teatree.paths import data_dir_root  # noqa: PLC0415 — deferred: env-sensitive path resolution at call time

    return data_dir_root() / MARKER_FILENAME


def _dir_is_writable(directory: Path) -> bool:
    """Whether a marker could be written under *directory* — WITHOUT creating anything.

    The nearest existing ancestor answers it, because that is what a later
    ``mkdir(parents=True)`` would need. Asking by ATTEMPTING the directory made
    :func:`marker_paths` — and so ``degraded_read_report``, ``clear_degraded_read`` and the
    doctor check — materialise a directory tree on a pure read (#4205).
    """
    for candidate in (directory, *directory.parents):
        if candidate.exists():
            return candidate.is_dir() and os.access(candidate, os.W_OK)
    return False


def marker_paths() -> tuple[Path, ...]:
    """Every location this venue may find the marker in, canonical first.

    The fallback is appended ONLY when the canonical directory cannot be written here. A
    venue that can write the canonical marker has one answer and must keep having one:
    offering the per-user path unconditionally would let a stale host marker outvote a
    healthy container record, which is the same "unverifiable signal read as evidence"
    defect one layer up.
    """
    canonical = marker_path()
    if _dir_is_writable(canonical.parent):
        return (canonical,)
    fallback = fallback_marker_path()
    return (canonical,) if fallback == canonical else (canonical, fallback)


def record_degraded_read(scope: str, *, caller: str = "") -> None:
    """Record that *scope*'s override read failed, merging into any live marker.

    *caller* is the calling context the reader identified (#3980); it merges the same way the
    scopes do, capped at :data:`MAX_RECORDED_CALLERS` so a caller in a hot loop cannot grow the
    file without bound.

    Written to the first of :func:`marker_paths` this venue can actually write, so a host
    process that cannot reach the container's control-DB volume still leaves a record where
    it observed the fault instead of only a log line.

    Never raises: the caller is a settings resolution that must still return a value, and
    a marker that cannot be written must not become a second outage. Exhausting every
    candidate is reported as a single WARNING line and no traceback — this function runs
    under the statusline and ``t3 loop status``, whose output must stay quiet and
    crash-proof, and the frames of an already-handled ``OSError`` name nothing the operator
    can act on that the path and message do not already say.
    """
    now = time.time()
    existing = _read_marker()
    scopes = tuple(sorted({*(existing.scopes if existing else ()), scope}))
    seen_callers = {*(existing.callers if existing else ()), *([caller] if caller else [])}
    blob = json.dumps(
        {
            "scopes": list(scopes),
            "callers": sorted(seen_callers)[:MAX_RECORDED_CALLERS],
            "occurrences": (existing.occurrences if existing else 0) + 1,
            "first_seen": existing.first_seen if existing else now,
            "last_seen": now,
        }
    )

    refusals: list[str] = []
    for candidate in marker_paths():
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(blob, encoding="utf-8")
        except OSError as exc:
            refusals.append(f"{candidate}: {exc}")
            continue
        return

    _logger.warning(
        "ConfigSetting override read degraded for scope %r AND no degradation marker could be "
        "written (%s) — this fault is visible only in this log.",
        scope,
        "; ".join(refusals) or "no candidate path",
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


def degraded_marker_unreadable() -> bool:
    """The marker EXISTS but does not parse — the tier's health is UNKNOWN, not healthy.

    The distinction :func:`degraded_read_report` deliberately collapses: it fails open to
    ``None`` for an absent marker AND for a corrupt one, so a resolver still gets a value.
    A health check reading that collapsed answer as "no fault" would report a tier it never
    established anything about, which is the state this whole module exists to end.
    """
    return marker_path().is_file() and _read_marker() is None


def clear_degraded_read() -> None:
    """Drop every marker this venue can see — the operator acknowledged/repaired the fault.

    All candidates, not just the canonical one: acknowledging a fault that was recorded in
    the fallback location has to clear THAT file, or the doctor keeps reporting a fault the
    operator already dismissed.
    """
    for candidate in marker_paths():
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            _logger.warning("could not clear the ConfigSetting degraded-read marker %s: %s", candidate, exc)


def _read_marker() -> DegradedReadReport | None:
    """The FRESHEST record across every candidate location.

    Freshest rather than canonical-first: the candidates are venue-local files that never
    see each other's writes, so the newest ``last_seen`` is the only ordering that answers
    "is the tier degraded now?" rather than "did it ever degrade in this one directory?".
    """
    found = [report for report in (_read_marker_at(path) for path in marker_paths()) if report is not None]
    return max(found, key=lambda report: report.last_seen) if found else None


def _read_marker_at(path: Path) -> DegradedReadReport | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    scopes = raw.get("scopes")
    occurrences = raw.get("occurrences")
    first_seen = raw.get("first_seen")
    last_seen = raw.get("last_seen")
    callers = raw.get("callers")
    if not isinstance(scopes, list) or not isinstance(occurrences, int):
        return None
    if not isinstance(first_seen, int | float) or not isinstance(last_seen, int | float):
        return None
    return DegradedReadReport(
        scopes=tuple(str(scope) for scope in scopes),
        occurrences=occurrences,
        first_seen=float(first_seen),
        last_seen=float(last_seen),
        callers=tuple(str(entry) for entry in callers) if isinstance(callers, list) else (),
        path=path,
    )


__all__ = [
    "MARKER_FILENAME",
    "MARKER_TTL_SECONDS",
    "MAX_RECORDED_CALLERS",
    "SAFETY_FAIL_CLOSED_STORED_VALUES",
    "ConfigOverrideReadError",
    "DegradedReadReport",
    "clear_degraded_read",
    "degraded_marker_unreadable",
    "degraded_read_report",
    "fallback_marker_path",
    "marker_path",
    "marker_paths",
    "record_degraded_read",
]
