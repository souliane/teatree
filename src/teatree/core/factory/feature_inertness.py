"""Which gated features shipped and then never ran (#4189) — the feature half of #3842.

:func:`~teatree.loops.seed_inertness.shipped_inertness` answers this for loops, presets and
schedules. This answers it for FEATURES, and by the same construction: the expectation comes
from :mod:`teatree.config.gate_evidence` (a declaration), and the measurement comes from the
observable that declaration names — never from the flag's own value, which would only ever
say what shipping it already said.

The method is the hand audit that found the twelve, automated: for each gate that is off
everywhere, query the observable it would populate; zero rows means it has never fired. Two
things keep it honest in the other direction — a populated observable clears a gate even
while its flag is off (the evidence proves it ran, whatever the flag says), and a gate merged
less than :data:`~teatree.config.gate_evidence.INERT_AFTER_DAYS` ago is too young to judge.

Severity is :mod:`~teatree.loops.seed_inertness`'s doctrine translated: a gate the owner
deliberately staged is a NOTE, a gate nobody ever decided to leave off is a FAULT. Without
that split the report is twenty lines every hour and becomes one nobody reads — which is how
the twelve stayed invisible while every surface reported success.
"""

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from teatree.config import get_effective_settings
from teatree.config.gate_evidence import GATE_EVIDENCE, INERT_AFTER_DAYS, ActivationIntent, GateEvidence, ObservableKind
from teatree.core.models import ConfigSetting

KIND_NEVER_FIRED = "never_fired"
KIND_UNOBSERVABLE = "unobservable"

#: The loud prefix a fault carries in the rendered report — a gate nobody decided to leave off.
FAULT_BANNER = "NOBODY DECIDED"

#: Separates what is not happening from what would make it happen (#4375).
SATISFIER_MARKER = " | satisfy it with: "

__all__ = [
    "FAULT_BANNER",
    "KIND_NEVER_FIRED",
    "KIND_UNOBSERVABLE",
    "SATISFIER_MARKER",
    "InertFeature",
    "feature_inertness",
    "observed_rows",
    "render_inertness_report",
]


@dataclass(frozen=True, slots=True)
class InertFeature:
    """One gated feature that shipping was supposed to buy something, and has not."""

    setting: str
    kind: str
    #: What is NOT happening, in the operator's terms — never a bare restatement of *kind*.
    detail: str
    #: A deliberate owner decision is reported but never failed on; see the module docstring.
    is_fault: bool

    @property
    def label(self) -> str:
        return f"{self.setting}: {self.detail}"

    def as_json(self) -> dict[str, Any]:
        return {"setting": self.setting, "kind": self.kind, "detail": self.detail, "is_fault": self.is_fault}


def feature_inertness(
    registry: Mapping[str, GateEvidence] | None = None,
    *,
    now: dt.date | None = None,
) -> tuple[InertFeature, ...]:
    """Every declared gate that is off everywhere and has produced no evidence of ever running.

    *registry* re-points the declaration away from the live one, which is what lets a test
    declare a gate that has deliberately never shipped — the only way to prove the expected
    set is read from the declaration rather than from the settings being measured.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import needs settings

    today = now or timezone.now().date()
    entries = GATE_EVIDENCE if registry is None else registry
    findings = [_finding(entry, today) for _, entry in sorted(entries.items())]
    return tuple(finding for finding in findings if finding is not None)


def _finding(entry: GateEvidence, today: dt.date) -> InertFeature | None:
    if enabled_anywhere(entry):
        return None
    age = (today - entry.shipped).days
    if age < INERT_AFTER_DAYS:
        return None
    fault = entry.intent is ActivationIntent.UNDECIDED
    if entry.kind is ObservableKind.NONE:
        return InertFeature(
            setting=entry.setting,
            kind=KIND_UNOBSERVABLE,
            detail=(
                f"off for {age}d and nothing can ever prove it ran — {entry.rationale} "
                f"({_intent_clause(entry)}){_satisfier_clause(entry)}"
            ),
            is_fault=fault,
        )
    if observed_rows(entry):
        return None
    return InertFeature(
        setting=entry.setting,
        kind=KIND_NEVER_FIRED,
        detail=(
            f"off for {age}d and {_observable_label(entry)} is empty — it has never fired "
            f"({_intent_clause(entry)}){_satisfier_clause(entry)}"
        ),
        is_fault=fault,
    )


def _satisfier_clause(entry: GateEvidence) -> str:
    """What would make *entry* pass — the next action, without which the line is unactionable."""
    return f"{SATISFIER_MARKER}{entry.satisfier}" if entry.satisfier.strip() else ""


def _intent_clause(entry: GateEvidence) -> str:
    if entry.intent is ActivationIntent.STAGED:
        return f"deliberately staged: {entry.rationale}"
    return "nobody decided to leave it off"


def _observable_label(entry: GateEvidence) -> str:
    if entry.kind is ObservableKind.TICKET_EXTRA:
        return f"Ticket.extra[{entry.target!r}]"
    return entry.target


def enabled_anywhere(entry: GateEvidence) -> bool:
    """Whether *entry*'s setting resolves to anything but its off value in ANY live scope.

    A gate enabled for one overlay is doing its job, so reading only the global scope would
    report ``require_merge_evidence`` — enabled for the teatree overlay precisely so it bites
    real teatree tickets — as inert. The scopes are the ones that actually carry a row, so no
    overlay enumeration is needed, and each is resolved through the real chain rather than by
    re-coercing the stored string.
    """
    scopes = set(ConfigSetting.objects.filter(key=entry.setting).values_list("scope", flat=True))
    return any(
        getattr(get_effective_settings(scope or None), entry.setting) != entry.off_value for scope in {"", *scopes}
    )


def render_inertness_report(findings: tuple[InertFeature, ...]) -> str:
    """Render *findings* as the operator-facing report, faults surfaced LOUD.

    Pure over its argument, like :func:`~teatree.config.feature_flags.render_flags_audit`, so
    both halves of the split are proven from a fixture rather than from the live registry.
    """
    if not findings:
        return "  (no gated feature is inert)"
    return "\n".join(
        f"  {f.setting}: {f.detail}" + (f"  <<< {FAULT_BANNER} >>>" if f.is_fault else "")
        for f in sorted(findings, key=lambda f: (not f.is_fault, f.setting))
    )


def observed_rows(entry: GateEvidence) -> int:
    """How many rows of *entry*'s declared observable exist — ``0`` for an unobservable gate."""
    from django.apps import apps  # noqa: PLC0415 — deferred: Django import needs settings

    if entry.kind is ObservableKind.NONE:
        return 0
    if entry.kind is ObservableKind.TICKET_EXTRA:
        ticket = apps.get_model("core", "Ticket")
        return ticket.objects.filter(extra__has_key=entry.target).count()
    app_label, model_name = entry.target.split(".")
    return apps.get_model(app_label, model_name).objects.filter(**entry.filters).count()
