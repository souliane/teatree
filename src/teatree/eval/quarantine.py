r"""The known-red QUARANTINE registry — a tracked red stops blocking unrelated PRs (#4173).

Selective-PR selection is section-scoped and precise
(:mod:`teatree.eval.changed_scenarios`), which has one consequence: while a scenario is
red, EVERY PR touching the doctrine section it grades reds its eval lane. A pre-existing
behavioural failure becomes a merge blocker for unrelated prose edits — and the ``_BROAD``
fail-safe (a preamble-only or unreadable diff) widens the blast radius exactly when the
diff is hardest to classify.

This module is the registry that closes that. ``evals/quarantine.yaml`` names each
currently-failing scenario with the issue that will fix it, the date the entry expires,
and a one-line reason; :func:`suppressed_scenario_names` is what the selector drops from
the bounded PR lane.

SCOPE — selection, never a verdict
    Quarantine decides which scenarios the bounded PR lane SELECTS. It never reaches a
    run verdict: a quarantined scenario that executes and fails still reds its run,
    exactly like any other. BLUEPRINT's "no known-red allowance, no shrink-only ratchet"
    holds unchanged, and the weekly/heal lane keeps grading and reporting the red.

THREE WAYS AN ENTRY GOES STALE, all of them detected rather than trusted
    *   EXPIRY is self-enforcing: past ``until`` the entry simply stops suppressing, so
        the scenario re-arms and blocks again exactly as it did before quarantine. A
        permanent skip list — how a suite rots into decoration — is unreachable by
        construction, and the failure mode is the status quo, never a silent pass.
    *   :meth:`Quarantine.escaped` names a quarantined scenario that PASSED in a run. The
        entry has become a lie in the other direction and must be deleted.
    *   :meth:`Quarantine.unknown` names an entry whose scenario the catalog no longer
        defines (a rename, a deletion), which suppresses nothing and hides a typo.

A MISSING file loads as an EMPTY quarantine — the sanctioned degraded state, so an
overlay with no registry of its own selects exactly as it did before. A PRESENT but
malformed file raises :class:`QuarantineError` rather than degrading to "suppress
nothing", because a typo'd entry would silently stop protecting the lane it was added
for.
"""

import dataclasses
import datetime
import re
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

import yaml

#: The committed registry, resolved from this module's path so the eval package stays a
#: leaf (the convention ``discovery.SCENARIOS_DIR`` / ``cost_bounds.COST_BOUNDS_PATH`` follow).
QUARANTINE_PATH = Path(__file__).resolve().parents[3] / "evals" / "quarantine.yaml"

#: The fields one entry carries. Every one is required: an entry with no tracking issue
#: or no expiry is the un-reviewable skip this registry exists to refuse.
_REQUIRED_FIELDS = ("issue", "until", "reason")

#: A tracking reference: a full issue URL, a bare ``#N``, or ``owner/repo#N``.
_ISSUE_REF = re.compile(r"^(https?://\S+|#\d+|[\w.\-]+/[\w.\-]+#\d+)$")


class QuarantineError(ValueError):
    """A malformed ``quarantine.yaml`` — a missing field, a bad date, an unknown key."""


def utc_today() -> datetime.date:
    """The reference date every expiry is judged against — UTC, so no host TZ can shift it."""
    return datetime.datetime.now(tz=datetime.UTC).date()


@dataclasses.dataclass(frozen=True, slots=True)
class QuarantineEntry:
    """One tracked known-red: the scenario, the issue that will fix it, and its expiry."""

    scenario: str
    issue: str
    until: datetime.date
    reason: str

    def is_expired(self, as_of: datetime.date) -> bool:
        return as_of > self.until

    def render(self) -> str:
        return f"{self.scenario} (until {self.until.isoformat()}, {self.issue}): {self.reason}"


@dataclasses.dataclass(frozen=True, slots=True)
class Quarantine:
    """The whole registry — one :class:`QuarantineEntry` per tracked known-red scenario."""

    entries: tuple[QuarantineEntry, ...]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(entry.scenario for entry in self.entries)

    def entry_for(self, scenario: str) -> QuarantineEntry | None:
        return next((entry for entry in self.entries if entry.scenario == scenario), None)

    def suppressed(self, *, as_of: datetime.date | None = None) -> frozenset[str]:
        """The scenario names the bounded PR lane drops — every entry not past its expiry."""
        today = as_of or utc_today()
        return frozenset(entry.scenario for entry in self.entries if not entry.is_expired(today))

    def expired(self, *, as_of: datetime.date | None = None) -> tuple[QuarantineEntry, ...]:
        """Entries past their date — they no longer suppress; fix the scenario or re-date them."""
        today = as_of or utc_today()
        return tuple(entry for entry in self.entries if entry.is_expired(today))

    def unknown(self, *, catalog: Collection[str]) -> tuple[QuarantineEntry, ...]:
        """Entries naming a scenario *catalog* does not define — a rename or a typo."""
        return tuple(entry for entry in self.entries if entry.scenario not in catalog)

    def escaped(self, *, passing: Collection[str]) -> tuple[QuarantineEntry, ...]:
        """Quarantined scenarios that PASSED — the entry is now a lie and must be deleted."""
        return tuple(entry for entry in self.entries if entry.scenario in passing)

    def still_red(self, *, failing: Collection[str]) -> tuple[QuarantineEntry, ...]:
        """Quarantined scenarios that are still red — reported, expected, tracked."""
        return tuple(entry for entry in self.entries if entry.scenario in failing)

    def absent(self, *, ran: Collection[str]) -> tuple[QuarantineEntry, ...]:
        """Quarantined scenarios the run never carried, so it says nothing about them."""
        return tuple(entry for entry in self.entries if entry.scenario not in ran)


def load_quarantine(path: Path | None = None) -> Quarantine:
    """Parse the registry into a typed :class:`Quarantine`.

    A missing file is an EMPTY quarantine (the sanctioned degraded state). A malformed
    one raises :class:`QuarantineError`.
    """
    registry_path = path or QUARANTINE_PATH
    if not registry_path.is_file():
        return Quarantine(entries=())
    loaded = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    if loaded is None:
        return Quarantine(entries=())
    if not isinstance(loaded, Mapping):
        msg = f"{registry_path}: expected a top-level mapping"
        raise QuarantineError(msg)
    raw = loaded.get("scenarios") or {}
    if not isinstance(raw, Mapping):
        msg = f"{registry_path}: 'scenarios' must be a mapping of scenario name -> entry"
        raise QuarantineError(msg)
    entries = tuple(_parse_entry(str(name), entry, path=registry_path) for name, entry in sorted(raw.items()))
    return Quarantine(entries=entries)


def quarantine_path_for(scenarios_dir: Path | None) -> Path:
    """The registry that governs a catalog — it sits beside its scenarios dir.

    Teatree's own layout IS the convention (``evals/scenarios`` → ``evals/quarantine.yaml``),
    so a consuming overlay that passes its own ``--scenarios-dir`` gets its own registry with
    no extra flag, and its missing one is simply an empty quarantine.
    """
    return QUARANTINE_PATH if scenarios_dir is None else scenarios_dir.parent / QUARANTINE_PATH.name


def suppressed_scenario_names(*, path: Path | None = None, as_of: datetime.date | None = None) -> frozenset[str]:
    """The names the selective-PR selector suppresses — the registry's single consumer seam."""
    return load_quarantine(path).suppressed(as_of=as_of)


def _parse_entry(name: str, entry: object, *, path: Path) -> QuarantineEntry:
    if not isinstance(entry, Mapping):
        msg = f"{path}: scenario {name!r} must map to an {{issue, until, reason}} mapping"
        raise QuarantineError(msg)
    fields: Mapping[str, Any] = {str(key): value for key, value in entry.items()}
    if extra := sorted(set(fields) - set(_REQUIRED_FIELDS)):
        msg = f"{path}: scenario {name!r} carries unknown key(s) {extra} — expected {list(_REQUIRED_FIELDS)}"
        raise QuarantineError(msg)
    for required in _REQUIRED_FIELDS:
        if required not in fields:
            msg = f"{path}: scenario {name!r} is missing required {required!r}"
            raise QuarantineError(msg)
    return QuarantineEntry(
        scenario=name,
        issue=_issue(fields["issue"], name=name, path=path),
        until=_until(fields["until"], name=name, path=path),
        reason=_reason(fields["reason"], name=name, path=path),
    )


def _issue(value: object, *, name: str, path: Path) -> str:
    if not isinstance(value, str) or not _ISSUE_REF.match(value.strip()):
        msg = (
            f"{path}: scenario {name!r} 'issue' must be a tracking reference "
            f"(an issue URL, '#N', or 'owner/repo#N'), got {value!r}"
        )
        raise QuarantineError(msg)
    return value.strip()


def _until(value: object, *, name: str, path: Path) -> datetime.date:
    # PyYAML parses an unquoted ISO date to `datetime.date`; anything else is a typo.
    if isinstance(value, datetime.datetime) or not isinstance(value, datetime.date):
        msg = f"{path}: scenario {name!r} 'until' must be an ISO date (YYYY-MM-DD), got {value!r}"
        raise QuarantineError(msg)
    return value


def _reason(value: object, *, name: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"{path}: scenario {name!r} 'reason' must be a non-empty one-line string"
        raise QuarantineError(msg)
    return value.strip()
