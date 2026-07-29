"""The refusal half of the result-envelope contract — one vocabulary, one owner.

:mod:`teatree.agents.envelope_contract` states what every headless brief TEACHES;
this module states what the pipeline REFUSES when an agent ignores that brief, and
how that refusal is corrected. Both halves of one contract, so a new refusal can
never be added on the producing side without the consuming side learning it.

Two seams produce an envelope refusal, and they used to name it in two
hand-maintained vocabularies that drifted:

* the RUNNER (:func:`teatree.agents.headless._record_success`) refuses a run whose
    output carried no JSON object at all — :data:`NO_ENVELOPE_ERROR`;
* the RECORDER (:mod:`teatree.agents.attempt_recorder`, :func:`…result_schema.check_evidence`)
    refuses an envelope that parsed but is unusable — wrong keys, not an object, or
    missing the phase's evidence field.

The consumer is :mod:`teatree.loop.transient_requeue`, whose one-shot corrective
retry reopens such a task with an explicit emit-the-envelope instruction. It
listed only the RECORDER's four strings, so ``no_result_envelope`` — the most
literal omitted-envelope failure there is — was the one class that never earned
the retry: the first prose-only run parked the task and paged a human. The fix is
this shared module, not a fifth hand-typed literal.

The refusal itself stays: a success you cannot parse is not evidence of success.
What this module makes possible is a BOUNDED, satisfiable correction of it.
"""

from teatree.agents.result_schema import required_evidence_for_phase

#: Prefix the headless runner stamps on a run that emitted no JSON object at all.
NO_ENVELOPE_PREFIX = "no_result_envelope: "

#: The runner's full refusal reason. Deliberately a CONSTANT with no run-specific
#: detail: ``TaskAttempt.error_fingerprint`` hashes it and the repair loop halts on
#: two consecutive identical fingerprints, so folding the agent's (always-different)
#: prose into the reason would make every no-envelope run look like a fresh failure
#: and defeat the stall check. The prose is preserved on the failed attempt's
#: ``result`` for diagnosis instead.
NO_ENVELOPE_ERROR = f"{NO_ENVELOPE_PREFIX}agent produced no JSON result envelope; refusing to record success"

#: Substrings identifying a RECORDER-side envelope refusal — an envelope that
#: parsed but is unusable, as opposed to a genuine defect (an assertion, a test
#: failure, a review verdict the reviewer legitimately withheld).
_RECORDER_REFUSAL_MARKERS = (
    "missing required evidence",
    "unexpected keys",
    "result is not valid json",
    "result must be a json object",
)


def is_no_envelope_refusal(error: str) -> bool:
    """Whether *error* is the RUNNER's "no JSON object at all" refusal.

    Keyed on :data:`NO_ENVELOPE_PREFIX` rather than the whole reason, so rewording
    the human-readable tail cannot silently drop the classification.
    """
    return NO_ENVELOPE_PREFIX.strip().casefold() in error.casefold()


def is_recorder_refusal(error: str) -> bool:
    """Whether *error* is a RECORDER-side refusal of a parsed-but-unusable envelope."""
    haystack = error.casefold()
    return any(marker in haystack for marker in _RECORDER_REFUSAL_MARKERS)


def is_envelope_refusal(error: str) -> bool:
    """Whether *error* is an envelope refusal of either kind (never a genuine defect)."""
    return is_no_envelope_refusal(error) or is_recorder_refusal(error)


def required_keys_phrase(phase: str) -> str:
    """The phase's own required envelope keys, rendered for an instruction line.

    Derived from :data:`~teatree.agents.result_schema.PHASE_REQUIRED_EVIDENCE`, never
    a second hand-maintained list — an instruction that names a key the phase does
    not require teaches the re-dispatched agent the wrong contract.
    """
    required = required_evidence_for_phase(phase)
    if not required:
        return "`summary`"
    return "`summary` and " + " or ".join(f"`{field}`" for field in required)


def corrective_instruction(phase: str) -> str:
    """The one-shot corrective note appended to a re-dispatched task's prompt.

    Lands in ``Task.execution_reason``, which ``agents/prompt.py`` renders as the
    brief's ``Reason:`` line — so it is phase-accurate by construction.
    """
    return (
        f"your last run omitted the required trailing JSON result envelope "
        f"({required_keys_phrase(phase)}) — emit it as the last thing you write, "
        "plain JSON, nothing after it."
    )
