"""What each default-OFF gate would OBSERVABLY produce if it were live (#4189).

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

``NONE`` is a real answer, not a placeholder: a refusal-only gate (``require_debt_delta``,
``require_merge_evidence``, ``require_work_group_batch``) blocks or passes and writes no
artifact of its own, so nothing can ever prove it ran. That is worse than inert, and the
report treats it as a standing fault rather than letting it declare its way to quiet.

The intent split is ``seed_inertness``'s severity doctrine, translated: a loop that ships off
and is off is doing exactly what it shipped doing. A gate the owner deliberately staged is a
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
    #: Narrows a shared table to the rows THIS gate writes — two gates both write
    #: ``CriticVerdict``, and without the narrowing either one firing would clear both.
    filters: Mapping[str, object] = field(default_factory=dict)


_REFUSAL_ONLY = "refusal-only gate: it blocks or passes and writes no artifact, so nothing can prove it ran"
_UNDECIDED = "no recorded decision to hold it off; "

#: Every governed gate that SHIPS OFF, and what would prove it is live. Totality over the
#: default-OFF half of ``FEATURE_FLAGS | DURABLE_GATE_SETTINGS`` is pinned by
#: ``tests/conformance/test_gate_evidence_declared.py`` — a new default-OFF gate fails CI here.
#: Each ``shipped`` date is the day the key first appeared in ``src/``, read off git history.
_DECLARATIONS: tuple[GateEvidence, ...] = (
    GateEvidence(
        setting="require_executed_repro",
        off_value=False,
        kind=ObservableKind.MODEL,
        target="core.ReproEvidence",
        shipped=dt.date(2026, 7, 6),
        intent=ActivationIntent.UNDECIDED,
        rationale=f"{_UNDECIDED}producer is `t3 <overlay> repro record`",
    ),
    GateEvidence(
        setting="require_rubric_verification",
        off_value=False,
        kind=ObservableKind.MODEL,
        target="core.Rubric",
        shipped=dt.date(2026, 6, 11),
        intent=ActivationIntent.UNDECIDED,
        rationale=f"{_UNDECIDED}producer is `t3 <overlay> ticket rubric-set` / `rubric-grade`",
    ),
    GateEvidence(
        setting="require_review_context",
        off_value=False,
        kind=ObservableKind.TICKET_EXTRA,
        target="review_context",
        shipped=dt.date(2026, 6, 3),
        intent=ActivationIntent.UNDECIDED,
        rationale=f"{_UNDECIDED}producer is `t3 <overlay> lifecycle record-review-context`",
    ),
    GateEvidence(
        setting="require_merge_quality_verdict",
        off_value=False,
        kind=ObservableKind.MODEL,
        target="core.CriticVerdict",
        shipped=dt.date(2026, 7, 6),
        intent=ActivationIntent.UNDECIDED,
        rationale=f"{_UNDECIDED}the merge critic writes the verdict",
        filters={"transition": "merge"},
    ),
    GateEvidence(
        setting="critic_gate_mode",
        off_value="off",
        kind=ObservableKind.MODEL,
        target="core.CriticVerdict",
        shipped=dt.date(2026, 7, 7),
        intent=ActivationIntent.UNDECIDED,
        rationale=f"{_UNDECIDED}the delivery critic writes the verdict",
        filters={"transition": "mark_delivered"},
    ),
    GateEvidence(
        setting="require_anti_vacuity_attestation",
        off_value=False,
        kind=ObservableKind.TICKET_EXTRA,
        target="anti_vacuity_attestation",
        shipped=dt.date(2026, 6, 5),
        intent=ActivationIntent.UNDECIDED,
        rationale=f"{_UNDECIDED}producer is `t3 <overlay> lifecycle record-anti-vacuity`",
    ),
    GateEvidence(
        setting="require_integration_review",
        off_value=False,
        kind=ObservableKind.MODEL,
        target="core.ReviewEvidence",
        shipped=dt.date(2026, 7, 4),
        intent=ActivationIntent.UNDECIDED,
        rationale=f"{_UNDECIDED}producer is `t3 <overlay> review record-evidence`",
    ),
    GateEvidence(
        setting="require_debt_delta",
        off_value=False,
        kind=ObservableKind.NONE,
        target="",
        shipped=dt.date(2026, 7, 6),
        intent=ActivationIntent.UNDECIDED,
        rationale=_REFUSAL_ONLY,
    ),
    GateEvidence(
        setting="require_merge_evidence",
        off_value=False,
        kind=ObservableKind.NONE,
        target="",
        shipped=dt.date(2026, 7, 5),
        intent=ActivationIntent.UNDECIDED,
        rationale=f"{_REFUSAL_ONLY} — it CONSUMES MergeAudit, which the keystone writes either way",
    ),
    GateEvidence(
        setting="require_spec_coverage",
        off_value=False,
        kind=ObservableKind.TICKET_EXTRA,
        target="spec_coverage",
        shipped=dt.date(2026, 6, 11),
        intent=ActivationIntent.UNDECIDED,
        rationale=f"{_UNDECIDED}producer is `t3 <overlay> ticket record-spec-coverage`",
    ),
    GateEvidence(
        setting="require_plan_adequacy",
        off_value=False,
        kind=ObservableKind.MODEL,
        target="core.PlanArtifact",
        shipped=dt.date(2026, 7, 5),
        intent=ActivationIntent.UNDECIDED,
        rationale="PlanArtifact rows are written and none carries an adequacy manifest — written, never read",
        filters={"adequacy__has_key": "design"},
    ),
    GateEvidence(
        setting="require_work_group_batch",
        off_value=False,
        kind=ObservableKind.NONE,
        target="",
        shipped=dt.date(2026, 8, 2),
        intent=ActivationIntent.UNDECIDED,
        rationale=_REFUSAL_ONLY,
    ),
    GateEvidence(
        setting="require_reviewed_state_for_review_request",
        off_value=False,
        kind=ObservableKind.NONE,
        target="",
        shipped=dt.date(2026, 7, 4),
        intent=ActivationIntent.UNDECIDED,
        rationale=f"{_REFUSAL_ONLY} — the FSM state itself is the satisfier",
    ),
    GateEvidence(
        setting="outer_loop_enabled",
        off_value=False,
        kind=ObservableKind.MODEL,
        target="core.OuterLoopExperiment",
        shipped=dt.date(2026, 7, 5),
        intent=ActivationIntent.STAGED,
        rationale="souliane/teatree#4189 — owner kept it 2026-08-04; unblocked by turning factory_score_enabled on",
    ),
    GateEvidence(
        setting="factory_score_enabled",
        off_value=False,
        kind=ObservableKind.MODEL,
        target="core.FactoryScoreSnapshot",
        shipped=dt.date(2026, 7, 5),
        intent=ActivationIntent.STAGED,
        rationale="souliane/teatree#4189 — owner turned it on 2026-08-04; the shipped default still ships off",
    ),
    GateEvidence(
        setting="send_proxy_mode",
        off_value="warn",
        kind=ObservableKind.MODEL,
        target="core.SendAudit",
        shipped=dt.date(2026, 7, 7),
        intent=ActivationIntent.STAGED,
        rationale="souliane/teatree#117 — ships warn (audit-only) until an overlay seeds the allowlist from a soak",
    ),
    GateEvidence(
        setting="ci_eval_heal_autofix_enabled",
        off_value=False,
        kind=ObservableKind.MODEL,
        target="core.CiEvalHealSession",
        shipped=dt.date(2026, 7, 19),
        intent=ActivationIntent.STAGED,
        rationale="souliane/teatree#3201 — autonomous CI mutation stays observe-only until deliberately armed",
    ),
)

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
