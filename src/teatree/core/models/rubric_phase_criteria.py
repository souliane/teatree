"""The standing criterion each phase seeds, and which of them a deployment can withhold.

Two of the criteria demand an executed failing run. A deployment that switches the forced-repro
requirement off records none, so seeding them hands it a rubric no verifier can pass; the
switch withholds exactly those two, identified by their TEXT so re-keying a phase changes nothing.
"""

from typing import TYPE_CHECKING

from teatree.core.modelkit.gate_registry import get_resolver

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

_RED_PIN_OBSERVED = "a RED pin exists and was observed failing before the fix"
_FAILS_WITHOUT_CHANGE = "changed behaviour is asserted by a test that fails without the change"

#: The probe-first standard, one gradeable criterion per phase that has one.
#:
#: ``core.Rubric`` shipped with a CLI, a manager, a done-gate and an error type, and zero
#: rows — a checklist nobody writes is one nobody grades, so the gate over it could only ever
#: be armed against an empty table. These are the content it lacked: the standard the factory
#: has to hold itself to WITHOUT the owner in the loop, stated so a verifier can fail it.
#:
#: Each is positive by construction — it names something that must be PRESENT — because a
#: criterion satisfied by inaction certifies nothing, which is what
#: :func:`~teatree.quality.falsifiable_criteria.falsifiability_violation` refuses at
#: population and what a seeded criterion must never be.
PHASE_CRITERIA: dict[str, str] = {
    "planning": "the plan cites at least one measurement of the LIVE system, with its venue named",
    "design": "each claim about existing behaviour names the consumer that was read, not prose inferred from it",
    "investigation": "every empty result is paired with a control proving the probe would have found something",
    "fix": _RED_PIN_OBSERVED,
    "coding": _FAILS_WITHOUT_CHANGE,
}

FAILING_RUN_CRITERIA: frozenset[str] = frozenset({_RED_PIN_OBSERVED, _FAILS_WITHOUT_CHANGE})


def withheld_by_deployment(ticket: "Ticket", text: str) -> bool:
    return text in FAILING_RUN_CRITERIA and not get_resolver("failing_run_required")(ticket)
