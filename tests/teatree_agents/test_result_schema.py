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
