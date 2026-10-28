"""The refusal half of the result-envelope contract — one vocabulary, one owner.

:mod:`teatree.agents.envelope_contract` states what every headless brief teaches
and how a refused envelope is corrected. This module classifies what the pipeline
refuses when an agent ignores that brief.

Two seams produce an envelope refusal, and they used to name it in two
hand-maintained vocabularies that drifted:

* the RUNNER (:func:`teatree.agents.runner_outcomes.record_success`) refuses a run whose
    output carried no JSON object or an unparsable envelope — :data:`NO_ENVELOPE_ERROR`
    or :data:`UNPARSEABLE_ENVELOPE_PREFIX`;
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

from collections.abc import Mapping, Sequence
from typing import cast

from teatree.agents.result_schema import RESULT_JSON_SCHEMA, JSONSchema
from teatree.core.modelkit.task_failure_taxonomy import (
    COMMENT_CHECK_PREFIX,
    MALFORMED_FIX_RECORD_PREFIX,
    MALFORMED_RUBRIC_GRADES_PREFIX,
    MALFORMED_TICKET_SWEEP_PREFIX,
    OWNER_STOP_REFUSAL_PREFIX,
)

#: Prefix the agent runner stamps on a run that emitted no JSON object at all.
NO_ENVELOPE_PREFIX = "no_result_envelope: "

#: The runner's full refusal reason. Deliberately a CONSTANT with no run-specific detail,
#: so the reason CLASSIFIES stably (:func:`is_no_envelope_refusal`, ``classify_failure``)
#: instead of varying with whatever prose the agent happened to emit; that prose is
#: preserved on the failed attempt's ``result`` for diagnosis. The constant does NOT feed
#: the repair loop's stall check: ``no_result_envelope`` is the absence of a cause, so
#: ``task_failure_taxonomy.is_causeless`` drops it from the identical-failure comparison
#: (souliane/teatree#4075) — otherwise the corrective retry below manufactured the second
#: identical fingerprint and halted a phase that was never doomed.
NO_ENVELOPE_ERROR = f"{NO_ENVELOPE_PREFIX}agent produced no JSON result envelope; refusing to record success"

UNPARSEABLE_ENVELOPE_PREFIX = "result is not valid JSON: "

#: Prefixes on review context blocks. They retain the recording-refused failure kind.
REVIEW_CONTEXT_REFUSAL_PREFIX = "review context recording refused: "
ANTI_VACUITY_REFUSAL_PREFIX = "anti-vacuity recording refused: "
MALFORMED_REVIEW_VERDICT_PREFIX = "malformed review_verdict: "


#: Substrings identifying a RECORDER-side envelope refusal — an envelope that
#: parsed but is unusable, as opposed to a genuine defect (an assertion, a test
#: failure, a review verdict the reviewer legitimately withheld).
_RECORDER_REFUSAL_MARKERS = (
    "missing required evidence",
    "unexpected keys",
    UNPARSEABLE_ENVELOPE_PREFIX.strip().casefold(),
    "result must be a json object",
    MALFORMED_FIX_RECORD_PREFIX.strip().casefold(),
    MALFORMED_RUBRIC_GRADES_PREFIX.strip().casefold(),
    MALFORMED_REVIEW_VERDICT_PREFIX.strip().casefold(),
    MALFORMED_TICKET_SWEEP_PREFIX.strip().casefold(),
    REVIEW_CONTEXT_REFUSAL_PREFIX.strip().casefold(),
    ANTI_VACUITY_REFUSAL_PREFIX.strip().casefold(),
    OWNER_STOP_REFUSAL_PREFIX.strip().casefold(),
)

#: The recorder refusals a REVIEWER fixes by re-emitting its envelope. ``missing required
#: evidence`` is left out: a reviewing run that returned no verdict withheld a judgement.
_REVIEW_SHAPE_REFUSAL_MARKERS = (
    "unexpected keys",
    MALFORMED_REVIEW_VERDICT_PREFIX.strip().casefold(),
    REVIEW_CONTEXT_REFUSAL_PREFIX.strip().casefold(),
    ANTI_VACUITY_REFUSAL_PREFIX.strip().casefold(),
)


def is_no_envelope_refusal(error: str) -> bool:
    """Whether *error* is the RUNNER's "no JSON object at all" refusal.

    Keyed on :data:`NO_ENVELOPE_PREFIX` rather than the whole reason, so rewording
    the human-readable tail cannot silently drop the classification.
    """
    return NO_ENVELOPE_PREFIX.strip().casefold() in error.casefold()


def is_unparseable_envelope_refusal(error: str) -> bool:
    return error.casefold().startswith(UNPARSEABLE_ENVELOPE_PREFIX.casefold())


def is_recorder_refusal(error: str) -> bool:
    """Whether *error* is a RECORDER-side refusal of a parsed-but-unusable envelope."""
    haystack = error.casefold()
    return any(marker in haystack for marker in _RECORDER_REFUSAL_MARKERS)


def is_comment_check_refusal(error: str) -> bool:
    return COMMENT_CHECK_PREFIX.casefold() in error.casefold()


def is_review_shape_refusal(error: str) -> bool:
    """Whether *error* refused a reviewer's envelope for a misnamed, misplaced or malformed key."""
    haystack = error.casefold()
    return any(marker in haystack for marker in _REVIEW_SHAPE_REFUSAL_MARKERS)


def is_owner_stop_refusal(error: str) -> bool:
    return error.casefold().startswith(OWNER_STOP_REFUSAL_PREFIX.casefold())


def is_rubric_grades_refusal(error: str) -> bool:
    return error.casefold().startswith(MALFORMED_RUBRIC_GRADES_PREFIX.casefold())


def returned_block_refusal(prefix: str, block: str, raw: Mapping[str, object], problems: Sequence[str]) -> str:
    """The refusal of a returned *block*: its *problems*, plus any key the schema's *block* does not take."""
    properties = cast("JSONSchema", RESULT_JSON_SCHEMA["properties"])
    expected = list(cast("JSONSchema", cast("JSONSchema", properties[block])["properties"]))
    named = list(problems)
    if unknown := sorted(set(raw) - set(expected)):
        named.append(f"unknown keys {', '.join(unknown)} — {block} takes only {', '.join(expected)}")
    return prefix + "; ".join(named)
