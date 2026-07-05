"""Typed envelope channels + the phase-evidence gate for shell-denied phases (#9).

A headless scanning_news / answering agent is denied the shell, so it cannot run
the ``t3`` CLI to persist its work. The typed ``article_suggestions`` / ``answer``
channels let it hand the work back through the result envelope, and
``PHASE_REQUIRED_EVIDENCE`` refuses a summary-only run that silently dropped it.
"""

from typing import Any, cast

from teatree.agents.result_schema import RESULT_JSON_SCHEMA, check_evidence, required_evidence_for_phase

_PROPERTIES = cast("dict[str, Any]", RESULT_JSON_SCHEMA["properties"])


class TestEnvelopeChannelSchema:
    def test_article_suggestions_and_answer_are_schema_keys(self) -> None:
        assert "article_suggestions" in _PROPERTIES
        assert "answer" in _PROPERTIES

    def test_article_suggestion_items_carry_the_typed_shape(self) -> None:
        item = _PROPERTIES["article_suggestions"]["items"]
        assert set(item["properties"]) == {"title", "url", "rationale"}
        assert item["required"] == ["url"]

    def test_answer_carries_the_typed_shape(self) -> None:
        answer = _PROPERTIES["answer"]
        assert set(answer["properties"]) == {"text", "thread_ref"}
        assert answer["required"] == ["text"]


class TestShellDeniedPhaseEvidenceGate:
    def test_reactive_phases_require_their_envelope_channel(self) -> None:
        assert required_evidence_for_phase("scanning_news") == ("article_suggestions",)
        assert required_evidence_for_phase("answering") == ("answer",)

    def test_scanning_news_summary_only_is_missing_evidence(self) -> None:
        assert check_evidence({"summary": "nothing"}, "scanning_news")
        satisfied = check_evidence({"summary": "x", "article_suggestions": [{"url": "https://e/a"}]}, "scanning_news")
        assert satisfied == ""

    def test_answering_summary_only_is_missing_evidence(self) -> None:
        assert check_evidence({"summary": "drafted"}, "answering")
        assert check_evidence({"summary": "x", "answer": {"text": "hi"}}, "answering") == ""

    def test_empty_channel_does_not_satisfy_the_gate(self) -> None:
        # An empty list / empty draft is falsy — a run that returned the key but
        # no content is still a dropped scan, refused.
        assert check_evidence({"article_suggestions": []}, "scanning_news")
        assert check_evidence({"answer": {}}, "answering")

    def test_url_less_article_suggestions_do_not_satisfy_the_gate(self) -> None:
        # Nonempty-but-malformed: the recorder skips every url-less item, so this
        # would persist ZERO rows. The gate predicate matches what persists — at
        # least one url-bearing item — so a run that would drop everything is
        # refused, not greened.
        assert check_evidence({"article_suggestions": [{"title": "x"}]}, "scanning_news")
        mixed = check_evidence({"article_suggestions": [{"title": "x"}, {"url": "https://e/a"}]}, "scanning_news")
        assert mixed == ""

    def test_text_less_answer_does_not_satisfy_the_gate(self) -> None:
        # A draft with a thread_ref but no text persists no DeferredQuestion, so
        # the gate refuses it rather than completing over a dropped reply.
        assert check_evidence({"answer": {"thread_ref": "x"}}, "answering")
        assert check_evidence({"answer": {"thread_ref": "x", "text": "hi"}}, "answering") == ""


class TestDirectiveInterpretationEvidenceGate:
    """North-star PR-6: the interpret phase must hand back a real payload, not a summary."""

    def test_directive_interpreting_requires_the_interpretation_envelope(self) -> None:
        assert required_evidence_for_phase("directive_interpreting") == ("directive_interpretation",)

    def test_a_summary_only_interpret_run_is_refused(self) -> None:
        assert check_evidence({"summary": "interpreted it"}, "directive_interpreting")

    def test_a_payloadless_envelope_is_refused(self) -> None:
        # An envelope with only an identity persists nothing — the recorder drops it,
        # so the gate refuses it rather than completing over zero recorded work (#9).
        assert check_evidence({"directive_interpretation": {"interpreter_identity": "x"}}, "directive_interpreting")

    def test_a_sketch_envelope_satisfies_the_gate(self) -> None:
        result = {"directive_interpretation": {"sketch": {"kind": "setting_policy_gate"}}}
        assert check_evidence(result, "directive_interpreting") == ""

    def test_clarifying_questions_satisfy_the_gate(self) -> None:
        result = {"directive_interpretation": {"clarifying_questions": ["open concurrently or ever?"]}}
        assert check_evidence(result, "directive_interpreting") == ""
