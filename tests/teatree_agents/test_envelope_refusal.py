"""The refusal vocabulary of the result-envelope contract — one owner, both seams.

Two seams produce an envelope refusal and used to name it in two hand-typed
lists that drifted, leaving ``no_result_envelope`` the one class the corrective
retry never fired for. These tests pin each classifier separately, so a future
change that widens one seam cannot silently narrow the other — and each carries
a negative control, because a classifier that answers ``True`` to everything
would pass every positive assertion.
"""

from django.test import SimpleTestCase

from teatree.agents.envelope_refusal import (
    MALFORMED_FIX_RECORD_PREFIX,
    NO_ENVELOPE_ERROR,
    NO_ENVELOPE_PREFIX,
    corrective_instruction,
    is_envelope_refusal,
    is_no_envelope_refusal,
    is_recorder_refusal,
    required_keys_phrase,
)

#: A real recorder-side refusal, as ``result_schema.check_evidence`` phrases it.
_EVIDENCE_REFUSAL = (
    "missing required evidence for phase 'coding': result must include one of "
    "[files_modified] with a non-empty value (codex #1282-6)"
)
#: A genuine defect — the control every classifier must reject.
_REAL_DEFECT = "AssertionError: expected 3 got 4"


class TestNoEnvelopeRefusal(SimpleTestCase):
    def test_the_runners_own_constant_is_classified(self) -> None:
        assert is_no_envelope_refusal(NO_ENVELOPE_ERROR)

    def test_classification_keys_on_the_prefix_not_the_prose_tail(self) -> None:
        # Keyed on the prefix so a future reword of the human-readable tail does
        # not silently drop the refusal out of the corrective-retry path.
        assert is_no_envelope_refusal(f"{NO_ENVELOPE_PREFIX}some entirely different wording")

    def test_a_recorder_refusal_is_not_a_no_envelope_refusal(self) -> None:
        # The two seams stay distinct: they carry different phase scopes in
        # ``transient_requeue._corrective_note``, so conflating them would widen
        # the recorder-side gate by accident.
        assert not is_no_envelope_refusal(_EVIDENCE_REFUSAL)

    def test_a_genuine_defect_is_rejected(self) -> None:
        assert not is_no_envelope_refusal(_REAL_DEFECT)


class TestRecorderRefusal(SimpleTestCase):
    def test_each_recorder_marker_is_classified(self) -> None:
        assert is_recorder_refusal(_EVIDENCE_REFUSAL)
        assert is_recorder_refusal("Agent result contains unexpected keys: bogus")
        assert is_recorder_refusal("result is not valid JSON: Expecting value")
        assert is_recorder_refusal("result must be a JSON object")

    def test_matching_is_case_insensitive(self) -> None:
        assert is_recorder_refusal("RESULT MUST BE A JSON OBJECT")

    def test_a_malformed_fix_record_is_a_recorder_refusal(self) -> None:
        """#4520: it earns the one-shot corrective retry, not a page to a human."""
        assert is_recorder_refusal(f"{MALFORMED_FIX_RECORD_PREFIX}these required field(s) are blank: evidence")
        assert not is_no_envelope_refusal(MALFORMED_FIX_RECORD_PREFIX)

    def test_the_runner_refusal_is_not_a_recorder_refusal(self) -> None:
        assert not is_recorder_refusal(NO_ENVELOPE_ERROR)

    def test_a_genuine_defect_is_rejected(self) -> None:
        assert not is_recorder_refusal(_REAL_DEFECT)
        assert not is_recorder_refusal("")


class TestEnvelopeRefusalUnion(SimpleTestCase):
    def test_it_covers_both_seams_and_rejects_a_real_defect(self) -> None:
        assert is_envelope_refusal(NO_ENVELOPE_ERROR)
        assert is_envelope_refusal(_EVIDENCE_REFUSAL)
        assert not is_envelope_refusal(_REAL_DEFECT)


class TestRequiredKeysPhrase(SimpleTestCase):
    def test_a_phase_with_an_evidence_requirement_names_its_own_key(self) -> None:
        # Derived from PHASE_REQUIRED_EVIDENCE, never a second hand-kept list.
        assert required_keys_phrase("coding") == "`summary` and `files_modified`"

    def test_a_phase_with_several_accepted_fields_offers_them_all(self) -> None:
        assert required_keys_phrase("testing") == "`summary` and `tests_run` or `tests_passed`"

    def test_a_phase_with_no_evidence_requirement_names_summary_alone(self) -> None:
        # ``debugging`` is exactly the phase the no-envelope refusal fires on.
        assert required_keys_phrase("debugging") == "`summary`"

    def test_an_unknown_phase_degrades_to_summary_rather_than_raising(self) -> None:
        assert required_keys_phrase("not-a-real-phase") == "`summary`"

    def test_a_phase_with_a_task_conditional_key_names_it_and_says_when(self) -> None:
        # The corrective re-dispatch is the only place a refused answerer learns what it
        # omitted, so dropping `work_item` here restates the contract without the mandate.
        assert required_keys_phrase("answering") == (
            "`summary` and `answer` (plus `work_item` when the request implies work)"
        )


class TestCorrectiveInstruction(SimpleTestCase):
    def test_it_demands_the_envelope_last_and_unfenced(self) -> None:
        # The note lands in the re-dispatched prompt, so it must restate the
        # placement rule the parser depends on, not merely say "emit it".
        note = corrective_instruction("debugging")
        assert "envelope" in note
        assert "last thing you write" in note
        assert "plain JSON" in note

    def test_it_never_names_a_key_the_phase_does_not_require(self) -> None:
        assert "files_modified" not in corrective_instruction("debugging")
        assert "files_modified" in corrective_instruction("coding")
