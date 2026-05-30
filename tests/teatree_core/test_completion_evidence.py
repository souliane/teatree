import pytest

from teatree.core.completion_evidence import (
    OUTCOME_CLAIM_KINDS,
    CompletionEvidence,
    CompletionEvidenceError,
    check_completion_evidence,
    detect_claim_kind,
    evidence_from_note,
    has_resolvable_pointer,
)


class TestDetectClaimKind:
    @pytest.mark.parametrize(
        ("note", "expected"),
        [
            ("merged via !6219", "merged"),
            ("work landed on main", "merged"),
            ("posted the review note", "posted"),
            ("published the comment", "posted"),
            ("shipped the feature", "shipped"),
            ("released to prod", "shipped"),
            ("deployed to dev", "deployed"),
            ("", ""),
            ("refactored the parser and split the module", ""),
            ("cleaned up internal helpers", ""),
        ],
    )
    def test_maps_note_to_canonical_kind(self, note: str, expected: str) -> None:
        assert detect_claim_kind(note) == expected

    @pytest.mark.parametrize("note", ["merged X", "posted Y", "shipped Z", "deployed W"])
    def test_every_detected_kind_is_a_known_outcome_kind(self, note: str) -> None:
        # Every non-empty kind the detector emits must be a gated outcome kind,
        # so an asserted claim can never slip past as "not an outcome".
        assert detect_claim_kind(note) in OUTCOME_CLAIM_KINDS


class TestHasResolvablePointer:
    @pytest.mark.parametrize(
        "note",
        [
            "see https://example.com/mr/1",
            "merged !6219",
            "closes #42",
            "at sha 1a2b3c4d",
            "forge note_AbC123 recorded",
            "landed via src/teatree/core/task.py",
        ],
    )
    def test_resolvable_token_is_detected(self, note: str) -> None:
        assert has_resolvable_pointer(note) is True

    @pytest.mark.parametrize("note", ["done", "shipped the feature", ""])
    def test_note_without_pointer_is_not_detected(self, note: str) -> None:
        assert has_resolvable_pointer(note) is False


class TestEvidenceFromNote:
    def test_outcome_note_with_pointer_is_resolvable(self) -> None:
        evidence = evidence_from_note("shipped via https://example.com/mr/77")
        assert evidence.claim_kind == "shipped"
        assert evidence.asserts_outcome is True
        assert evidence.has_resolvable_pointer is True

    def test_outcome_note_without_pointer_is_not_resolvable(self) -> None:
        evidence = evidence_from_note("shipped the feature")
        assert evidence.asserts_outcome is True
        assert evidence.has_resolvable_pointer is False

    def test_internal_note_does_not_assert_outcome(self) -> None:
        evidence = evidence_from_note("refactored the parser")
        assert evidence.claim_kind == ""
        assert evidence.asserts_outcome is False

    def test_empty_note_yields_empty_evidence(self) -> None:
        evidence = evidence_from_note("")
        assert evidence == CompletionEvidence(claim_kind="", artifact_pointer="")


class TestCheckCompletionEvidence:
    @pytest.mark.parametrize(
        "note",
        [
            "shipped the feature",
            "merged the branch",
            "posted the review",
            "deployed to dev",
        ],
    )
    def test_outcome_claim_without_pointer_is_refused(self, note: str) -> None:
        with pytest.raises(CompletionEvidenceError, match="no resolvable artifact pointer"):
            check_completion_evidence(note)

    @pytest.mark.parametrize(
        "note",
        [
            "shipped via https://example.com/mr/77",
            "merged !6219",
            "posted note_AbC123",
            "deployed at sha 1a2b3c4d5e",
            "landed via src/teatree/core/task.py",
        ],
    )
    def test_outcome_claim_with_pointer_passes(self, note: str) -> None:
        check_completion_evidence(note)  # does not raise

    @pytest.mark.parametrize(
        "note",
        [
            "",
            "refactored the parser and split the module",
            "cleaned up internal helpers",
        ],
    )
    def test_no_outcome_claim_passes(self, note: str) -> None:
        check_completion_evidence(note)  # does not raise

    def test_error_message_names_the_claim_kind_and_pointer_kinds(self) -> None:
        with pytest.raises(CompletionEvidenceError) as exc:
            check_completion_evidence("shipped the feature")
        message = str(exc.value)
        assert "'shipped'" in message
        assert "URL" in message
        assert "git SHA" in message
