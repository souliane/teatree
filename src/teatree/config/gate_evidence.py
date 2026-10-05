"""What each default-OFF gate would observably produce if it were live (#4189).

:mod:`teatree.loops.seed_inertness` proved the doctrine for loops: nothing had ever been
deleted, so the guard worth having detects INERTNESS, not absence — and the expectation is
sourced from a DECLARATION (the shipped seed), never from the thing being measured. Features
had no equivalent. Twelve quality gates shipped merged, reviewed, tested and default-OFF, the
factory recorded 562 ``MergeClear`` rows with every one of them off, and their evidence
tables held zero rows. Every surface reported success the whole time.

This module is the declaration half. For each governed gate that SHIPS OFF it records the
observable its being live would generate, so :mod:`teatree.core.factory.feature_inertness`
can ask "has this ever fired?" against something other than the flag's own value. Reading the
flag to decide whether the flag matters is the self-referential defect #3836 names.

Naming the observable answers "has it fired?" and not "what would make it fire?", and the
second question is the one an arming decision needs. Ten gates sat UNDECIDED for up to 67 days
against a report that named no next action (#4375), so every entry also declares a
``satisfier``: the command that writes the observable, or, for a refusal-only gate, the state
that satisfies the refusal. It is a declared field rather than rationale prose because prose
drifts silently — the first version of this registry sent operators at ``t3 <overlay> repro
record``, which is not a command — and ``tests/conformance/test_gate_evidence_declared.py``
resolves every ``t3 …`` citation in it against the live CLI registry.

``NONE`` is a real answer, not a placeholder: a refusal-only gate blocks or passes
and writes no artifact of its own, so nothing can ever prove it ran. The report
treats an undecided one as a standing fault.

The intent split is ``seed_inertness``'s severity doctrine, translated: a gate the owner deliberately staged is a
NOTE; a gate nobody ever decided to leave off is a FAULT. ``STAGED`` therefore costs a
citation — an entry claiming it without one is refused by :func:`declaration_faults`, so the
quiet half of the report cannot be reached by asserting it.
"""

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

#: How long a gate gets to fire before its silence is a finding. A full factory week —
#: shorter would flag a gate merged on Friday, longer would have let all twelve hide for
#: the month they actually hid for.
INERT_AFTER_DAYS = 7

#: An issue reference, or an ISO date pinning when the call was made.
_DECISION_REFERENCE = re.compile(r"#\d+|\d{4}-\d{2}-\d{2}")


class ObservableKind(StrEnum):
    """Where the proof that a gate ran would land."""

    MODEL = "model"
    TICKET_EXTRA = "ticket_extra"
    NONE = "none"


class ActivationIntent(StrEnum):
    """Whether anyone DECIDED this gate should be off — the note-vs-fault discriminator."""

    STAGED = "staged"
    UNDECIDED = "undecided"


@dataclass(frozen=True, slots=True)
class GateEvidence:
    """One gated feature, and the observable that would prove it is not inert.

    ``off_value`` is the value that means "this gate is not enforcing" — ``False`` for a
    positive-sense toggle, the OFF member of a typed mode. It is declared here rather than
    assumed, because an inverted-sense key (``danger_gate_fail_open``, whose ``False`` IS the
    enforcing state) would otherwise read as permanently inert.
    """

    setting: str
    off_value: bool | str
    kind: ObservableKind
    #: ``"<app_label>.<Model>"`` for :attr:`ObservableKind.MODEL`, the ``Ticket.extra`` key
    #: for :attr:`ObservableKind.TICKET_EXTRA`, empty for :attr:`ObservableKind.NONE`.
    target: str
    shipped: dt.date
    intent: ActivationIntent
    #: Why it is staged, or why nothing observable exists. Load-bearing for both.
    rationale: str
    #: What would make this gate PASS — the command that writes the observable, or, for a
    #: refusal-only gate, the state that satisfies the refusal. Required on every entry: it is
    #: the input an arming decision needs, and a report without it names no next action.
    satisfier: str
    #: Narrows a shared table to the rows THIS gate writes.
    filters: Mapping[str, object] = field(default_factory=dict)


#: Every governed gate that SHIPS OFF, and what would prove it is live. Totality over the
#: default-OFF half of ``FEATURE_FLAGS | DURABLE_GATE_SETTINGS`` is pinned by
#: ``tests/conformance/test_gate_evidence_declared.py`` — a new default-OFF gate fails CI here.
#: Each ``shipped`` date is the day the key first appeared in ``src/``, read off git history.
_DECLARATIONS: tuple[GateEvidence, ...] = ()

#: Keyed off each entry's own ``setting``, so a hand-written key can never name a different
#: gate than the entry beside it — the drift a separate key column would have to be checked for.
GATE_EVIDENCE: dict[str, GateEvidence] = {entry.setting: entry for entry in _DECLARATIONS}


#: A shipped default that means "this gate is not enforcing". A governed key defaulting to one
#: of these ships OFF, so it owes a declaration. An inverted-sense key whose ``False`` IS the
#: enforcing state is judged by its own declared ``off_value`` instead — declaring always wins.
_OFF_SHAPED_DEFAULTS = frozenset({"off", "warn", "disabled"})


def ships_off(default: object) -> bool:
    """Whether *default* is the shape of a gate that ships not enforcing."""
    return default is False or (isinstance(default, str) and default.lower() in _OFF_SHAPED_DEFAULTS)


def undeclared_gates(
    shipped: Mapping[str, object],
    governed: Iterable[str],
    registry: Mapping[str, GateEvidence] | None = None,
) -> tuple[str, ...]:
    """Every *governed* gate that ships off per *shipped* and declares no evidence observable.

    The CI refusal criterion 3 of #4189 asks for, and the reason it is cheap: it fires only on
    a key nobody has classified yet, so a new default-OFF gate is stopped at the PR that mints
    it without the existing surface being retro-fitted.
    """
    entries = GATE_EVIDENCE if registry is None else registry
    return tuple(sorted(key for key in governed if key not in entries and ships_off(shipped.get(key))))


def declaration_faults(registry: Mapping[str, GateEvidence] | None = None) -> tuple[str, ...]:
    """Every malformed entry in *registry*, one message each — empty when it is well-formed.

    Pure over its argument (like :func:`~teatree.config.feature_flags.dark_flags`) so the
    refusals are proven from a fixture rather than from the live registry's composition.
    """
    entries = GATE_EVIDENCE if registry is None else registry
    faults: list[str] = []
    for key, entry in sorted(entries.items()):
        if not entry.rationale.strip():
            faults.append(f"{key}: rationale is empty — say why it is staged, or why nothing is observable")
        if not entry.satisfier.strip():
            faults.append(f"{key}: satisfier is empty — name what would make this gate pass, or nobody can arm it")
        if entry.intent is ActivationIntent.STAGED and not _cites_a_decision(entry.rationale):
            faults.append(f"{key}: STAGED needs a decision reference (an issue ref or a dated owner decision)")
        faults.extend(_shape_faults(key, entry))
    return tuple(faults)


def _shape_faults(key: str, entry: GateEvidence) -> list[str]:
    """Whether *entry*'s ``target``/``filters`` match the ``kind`` it declares."""
    if entry.kind is ObservableKind.NONE:
        return [f"{key}: kind=none must declare no target"] if entry.target else []
    if not entry.target:
        return [f"{key}: kind={entry.kind.value} needs a target"]
    if entry.kind is ObservableKind.MODEL and entry.target.count(".") != 1:
        return [f"{key}: model target {entry.target!r} is not '<app_label>.<Model>'"]
    return []


def _cites_a_decision(rationale: str) -> bool:
    """Whether *rationale* points at something lookup-able rather than only reading like it does.

    The same distinction :func:`~teatree.config.feature_flags.tracking_reference` draws for a
    flag's tracking prose: an issue reference, or an ISO date pinning when the call was made.
    """
    return bool(_DECISION_REFERENCE.search(rationale))
