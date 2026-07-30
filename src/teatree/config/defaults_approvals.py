"""The divergence gate over the hand-editable ``defaults.toml``.

``defaults.toml`` is the last tier of every resolution chain, so a value that differs
from its in-code ``UserSettings`` default MOVES that effective default. Editing it is
allowed — that is the point of a hand-editable shipped-defaults file — but the move must
be a REVIEWED decision, not a side effect of a snapshot run or a stray edit.

``defaults_approvals.toml`` is that review record: one entry per diverging key, carrying
the approved VALUE, who approved it, and the deferred question they answered. The gate
(:func:`audit_shipped_defaults`, asserted by ``tests/config/test_defaults_approvals.py``)
fails four ways and passes only when the two files agree:

* **unapproved** — the file diverges and the ledger has no entry for the key;
* **mismatched** — the ledger approves a DIFFERENT value than the file ships, so the
    approval does not authorize what is actually shipped;
* **forbidden** — a safety-posture key or a dark feature-flag diverges. These can never
    move through ANY path, approval or not: a write to one is an authorization, so it is
    pinned fail-closed at its in-code value;
* **stale** — the ledger still approves a key that no longer diverges. An approval is
    consumed by the divergence it authorizes; a leftover entry would silently
    pre-authorize a future re-divergence, so it is removed in the same change.

The divergence is computed on the RESOLVED value (``effective_default`` and the two
structured parsers), never on the raw TOML scalar — a ``"draft_or_ask"`` string and a
``OnBehalfPostMode.DRAFT_OR_ASK`` enum are the same default, and comparing raw would
report every enum-valued key as diverged.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from teatree.config import cold_defaults
from teatree.config.defaults_snapshot import pinned_fail_closed_keys
from teatree.config.mr_reminder import mr_reminder_from_table
from teatree.config.resolution import effective_default
from teatree.config.settings import UserSettings
from teatree.config.speak import speak_from_subtable

APPROVALS_TOML = Path(__file__).with_name("defaults_approvals.toml")

_STRUCTURED_KEYS = ("mr_reminder", "speak")
_ENTRY_TABLE = "approved"

_LEDGER_HEADER = """\
# Recorded owner approvals for each shipped default that DIVERGES from its in-code
# `UserSettings` default.
#
# `defaults.toml` is hand-editable and the resolver reads it, so a diverging value moves
# an effective default. That move is a reviewed decision, and this file is the record of
# it — one entry per diverging key, carrying the exact approved value. CI refuses a
# divergence with no entry here, an entry whose value differs from what is shipped, and
# an entry left behind after the divergence was reverted.
#
# `manage.py snapshot_settings_defaults --apply` appends entries here after the owner
# answers `approve` on the proposal's deferred question. A maintainer hand-editing
# `defaults.toml` adds the matching entry by hand in the same commit — the PR diff IS
# the review.
#
# Safety-posture keys and dark feature-flags are NOT approvable: they stay pinned to
# their fail-closed/off in-code value, and an entry for one does not authorize anything.
"""


@dataclass(frozen=True, slots=True)
class ApprovedDivergence:
    """One recorded approval — the value it authorizes and who authorized it."""

    key: str
    value: object
    approver: str
    question_id: int
    recorded_at: str


@dataclass(frozen=True, slots=True)
class Divergence:
    """A shipped default that does not equal its in-code default."""

    key: str
    shipped: object
    in_code: object


@dataclass(frozen=True, slots=True)
class DefaultsAudit:
    """The gate verdict — empty on every axis means the two files agree."""

    unapproved: tuple[Divergence, ...] = ()
    mismatched: tuple[Divergence, ...] = ()
    forbidden: tuple[Divergence, ...] = ()
    stale: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not (self.unapproved or self.mismatched or self.forbidden or self.stale)

    def report(self) -> str:
        """A human-readable verdict naming every offending key and the remedy."""
        lines = [
            *(f"  UNAPPROVED {d.key}: ships {d.shipped!r}, in-code {d.in_code!r}" for d in self.unapproved),
            *(f"  MISMATCHED {d.key}: ships {d.shipped!r} — the ledger approves another" for d in self.mismatched),
            *(f"  FORBIDDEN  {d.key}: ships {d.shipped!r} — safety-posture/dark stay pinned" for d in self.forbidden),
            *(f"  STALE      {key}: approved but no longer diverging — drop the entry" for key in self.stale),
        ]
        if not lines:
            return "defaults.toml agrees with defaults_approvals.toml"
        return "defaults.toml / defaults_approvals.toml disagree:\n" + "\n".join(lines)


def resolved_shipped_value(key: str, code: UserSettings) -> object:
    """The value the resolver derives for *key* from the shipped file alone.

    The two structured fields arrive as sub-tables, so they resolve through the same
    parsers the resolver rebuilds them with rather than through ``effective_default``
    (which reports them in stored dict form on purpose).
    """
    if key not in _STRUCTURED_KEYS:
        return effective_default(key)
    table = cold_defaults.shipped_defaults_table()[key]
    return mr_reminder_from_table(table) if key == "mr_reminder" else speak_from_subtable(table, base=code.speak)


def shipped_divergences() -> dict[str, Divergence]:
    """Every shipped key whose resolved value differs from its ``UserSettings`` default."""
    code = UserSettings()
    divergences = {}
    for key in cold_defaults.shipped_defaults_table():
        if not hasattr(code, key):
            continue
        shipped = resolved_shipped_value(key, code)
        in_code = getattr(code, key)
        if shipped != in_code:
            divergences[key] = Divergence(key=key, shipped=shipped, in_code=in_code)
    return divergences


def audit_shipped_defaults(*, approvals: dict[str, ApprovedDivergence] | None = None) -> DefaultsAudit:
    """Judge the shipped file against the approval ledger — the CI gate's verdict."""
    recorded = read_approvals() if approvals is None else approvals
    divergences = shipped_divergences()
    pinned = pinned_fail_closed_keys()

    forbidden = tuple(d for key, d in sorted(divergences.items()) if key in pinned)
    approvable = {key: d for key, d in divergences.items() if key not in pinned}
    unapproved = tuple(d for key, d in sorted(approvable.items()) if key not in recorded)
    mismatched = tuple(
        d for key, d in sorted(approvable.items()) if key in recorded and recorded[key].value != d.shipped
    )
    # A pinned key's entry is reported once, as FORBIDDEN — never doubled as stale.
    return DefaultsAudit(
        unapproved=unapproved,
        mismatched=mismatched,
        forbidden=forbidden,
        stale=tuple(sorted(set(recorded) - set(approvable) - {d.key for d in forbidden})),
    )


def read_approvals(path: Path = APPROVALS_TOML) -> dict[str, ApprovedDivergence]:
    """The ledger keyed by setting name; a missing file reads as no approvals."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    entries = tomllib.loads(raw).get(_ENTRY_TABLE, [])
    return {
        entry["key"]: ApprovedDivergence(
            key=entry["key"],
            value=entry["value"],
            approver=entry.get("approver", ""),
            question_id=int(entry.get("question_id", 0)),
            recorded_at=entry.get("recorded_at", ""),
        )
        for entry in entries
    }


def render_approvals(approvals: dict[str, ApprovedDivergence]) -> str:
    """The ledger's canonical text — key-sorted, so a re-render is byte-stable."""
    document = tomlkit.document()
    array = tomlkit.aot()
    for key in sorted(approvals):
        entry = approvals[key]
        table = tomlkit.table()
        table["key"] = entry.key
        table["value"] = entry.value
        table["approver"] = entry.approver
        table["question_id"] = entry.question_id
        table["recorded_at"] = entry.recorded_at
        array.append(table)
    document[_ENTRY_TABLE] = array
    # One trailing newline whether or not there are entries, so an empty ledger is a
    # fixed point of both this renderer and the end-of-file-fixer hook.
    return (_LEDGER_HEADER + "\n" + tomlkit.dumps(document)).rstrip("\n") + "\n"


__all__ = [
    "APPROVALS_TOML",
    "ApprovedDivergence",
    "DefaultsAudit",
    "Divergence",
    "audit_shipped_defaults",
    "read_approvals",
    "render_approvals",
    "resolved_shipped_value",
    "shipped_divergences",
]
