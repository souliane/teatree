import pytest

from teatree.core.completion_evidence import (
    OUTCOME_CLAIM_KINDS,
    CompletionEvidence,
    CompletionEvidenceError,
    asserts_outcome,
    check_completion_evidence,
    detect_claim_kind,
    evidence_from_note,
    has_resolvable_pointer,
    normalize_artifact_pointers,
)

# Internal-progress notes that merely CONTAIN an outcome verb while describing
# code work. Each must be treated as a non-assertion and never gated (#1280
# cold-review false-positives).
INTERNAL_PROGRESS_NOTES = [
    "merged the two helper functions into one",
    "released the lock after the transaction",
    "deployed config refactored into a dataclass",
    "the posted form was confusing, redesigned",
    "merge conflict resolved in the parser",
    "decided not to merge yet, more work",
]

# Genuine external-outcome claims with NO resolvable pointer — must stay gated.
GENUINE_CLAIMS_WITHOUT_POINTER = [
    "shipped to prod",
    "posted the review comment",
    "deployed to staging",
]

# Spoof notes that assert an outcome but whose "pointer" is vacuous, so the
# gate must REFUSE them (assertion holds, evidence does not).
SPOOF_CLAIMS = [
    "merged a/b",
    "merged the deadbeef branch",
]


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


class TestAssertsOutcome:
    @pytest.mark.parametrize(
        "note",
        [
            "merged via !6219",
            "shipped to prod",
            "posted the review comment",
            "deployed to staging",
            "merged a/b",
            "merged the deadbeef branch",
            "landed via src/teatree/core/task.py",
        ],
    )
    def test_verb_with_context_cue_asserts_outcome(self, note: str) -> None:
        assert asserts_outcome(note) is True

    @pytest.mark.parametrize("note", INTERNAL_PROGRESS_NOTES)
    def test_internal_progress_note_does_not_assert_outcome(self, note: str) -> None:
        assert asserts_outcome(note) is False

    @pytest.mark.parametrize("note", ["", "refactored the parser", "cleaned up helpers", "shipped the feature"])
    def test_no_verb_or_no_cue_does_not_assert(self, note: str) -> None:
        assert asserts_outcome(note) is False

    @pytest.mark.parametrize("note", ["merged", "it's merged", "merged to main", "  deployed", "its shipped"])
    def test_note_initial_bare_outcome_verb_asserts(self, note: str) -> None:
        # Hole C: the canonical phantom shape is an outcome verb at the very
        # start of a short note (optionally after a leading it's/its/it is),
        # with no other cue. It must read as an assertion so the gate can fire.
        assert asserts_outcome(note) is True

    @pytest.mark.parametrize("note", ["the merge was hard", "we discussed shipping later", "a posted note was wrong"])
    def test_non_initial_bare_verb_does_not_assert(self, note: str) -> None:
        # The note-initial carve-out must not fire when the verb is buried in
        # prose with no context cue — that is ordinary internal-progress text.
        assert asserts_outcome(note) is False


class TestHasResolvablePointer:
    @pytest.mark.parametrize(
        "note",
        [
            "see https://example.com/mr/1",
            "merged !6219",
            "closes #42",
            "at sha 1a2b3c4d",
            "commit 1a2b3c4d landed",
            "at @1a2b3c4d",
            "deployed at sha 1a2b3c4d5e",
            "merged via commit 1234567890abcdef",
            "forge note_AbC123 recorded",
            "landed via src/teatree/core/task.py",
            "updated ./scripts/run.sh",
            "see teatree.core.completion_evidence",
        ],
    )
    def test_resolvable_token_is_detected(self, note: str) -> None:
        assert has_resolvable_pointer(note) is True

    @pytest.mark.parametrize(
        "note",
        [
            "done",
            "shipped the feature",
            "",
            "merged a/b",
            "merged the deadbeef branch",
            "released the co/op feature",
            # Hole A: a bare 10+ digit/hex run with no commit cue is not a SHA.
            "build 1234567890",
            "merged abcdef1234 cleanly",
        ],
    )
    def test_spoof_or_empty_is_not_a_pointer(self, note: str) -> None:
        # The tightened shape rules reject vacuous matches that the original
        # regexes let through (bare word/word, hex-like English words, and a
        # bare long hex/digit run with no commit cue).
        assert has_resolvable_pointer(note) is False

    @pytest.mark.parametrize(
        "note",
        [
            "merged via commit 1234567890abcdef",
            "at sha 1a2b3c4d",
            "rebased onto @deadbeef1",
        ],
    )
    def test_cued_sha_still_resolves(self, note: str) -> None:
        # Dropping the bare long-hex path must not drop a legitimately cued SHA.
        assert has_resolvable_pointer(note) is True

    @pytest.mark.parametrize("note", ["fix the.thing.now", "ran the.test.again", "see what.we.did"])
    def test_dotted_prose_is_not_a_module_path(self, note: str) -> None:
        # Hole B: ordinary 3-segment dotted prose is not a module pointer.
        assert has_resolvable_pointer(note) is False

    @pytest.mark.parametrize(
        "note",
        [
            "see teatree.core.completion_evidence",
            "in src.teatree.core.task",
            "see tests.teatree_core.test_task",
            "the module foo.bar.baz.py",
        ],
    )
    def test_genuine_module_path_still_resolves(self, note: str) -> None:
        assert has_resolvable_pointer(note) is True


class TestEvidenceFromNote:
    def test_outcome_note_with_pointer_is_resolvable(self) -> None:
        evidence = evidence_from_note("shipped via https://example.com/mr/77")
        assert evidence.claim_kind == "shipped"
        assert evidence.asserts_outcome is True
        assert evidence.has_resolvable_pointer is True

    def test_outcome_note_without_pointer_asserts_but_is_not_resolvable(self) -> None:
        evidence = evidence_from_note("shipped to prod")
        assert evidence.asserts_outcome is True
        assert evidence.has_resolvable_pointer is False

    def test_internal_note_does_not_assert_outcome(self) -> None:
        evidence = evidence_from_note("merged the two helper functions into one")
        assert evidence.claim_kind == ""
        assert evidence.asserts_outcome is False

    def test_empty_note_yields_empty_evidence(self) -> None:
        evidence = evidence_from_note("")
        assert evidence == CompletionEvidence(claim_kind="", artifact_pointer="")


class TestCheckCompletionEvidence:
    @pytest.mark.parametrize("note", [*GENUINE_CLAIMS_WITHOUT_POINTER, *SPOOF_CLAIMS])
    def test_outcome_claim_without_real_pointer_is_refused(self, note: str) -> None:
        with pytest.raises(CompletionEvidenceError, match="no resolvable artifact pointer"):
            check_completion_evidence(note)

    @pytest.mark.parametrize(
        "note",
        [
            "shipped via https://example.com/mr/77",
            "merged !6219",
            "posted note_AbC123",
            "deployed to staging at sha 1a2b3c4d5e",
            "landed via src/teatree/core/task.py",
        ],
    )
    def test_outcome_claim_with_pointer_passes(self, note: str) -> None:
        check_completion_evidence(note)  # does not raise

    @pytest.mark.parametrize("note", ["", *INTERNAL_PROGRESS_NOTES, "shipped the feature"])
    def test_no_outcome_assertion_passes(self, note: str) -> None:
        check_completion_evidence(note)  # does not raise

    @pytest.mark.parametrize("note", ["merged", "it's merged", "merged to main"])
    def test_note_initial_phantom_claim_without_pointer_is_refused(self, note: str) -> None:
        # Hole C: a terse note that is just an outcome verb (no pointer) is the
        # canonical phantom completion and must now be gated.
        with pytest.raises(CompletionEvidenceError, match="no resolvable artifact pointer"):
            check_completion_evidence(note)

    @pytest.mark.parametrize(
        "note",
        [
            "merged via commit 1234567890abcdef",
            "merged (see https://example.com/mr/9)",
            "merged !6219",
        ],
    )
    def test_note_initial_outcome_verb_with_pointer_still_passes(self, note: str) -> None:
        # The Hole C carve-out only bites when there is no pointer; a real
        # pointer alongside the leading verb must keep the completion passing.
        check_completion_evidence(note)  # does not raise

    def test_error_message_names_the_claim_kind_and_pointer_kinds(self) -> None:
        with pytest.raises(CompletionEvidenceError) as exc:
            check_completion_evidence("shipped to prod")
        message = str(exc.value)
        assert "'shipped'" in message
        assert "URL" in message
        assert "git SHA" in message


class TestSlackPointer:
    """An answerer records its post as a Slack ts; the gate must accept it.

    Two answerer agents hit the gate rejecting a bare Slack ts and hand-built
    archive permalinks as a workaround. The gate now recognises the
    ``slack:<channel>:<ts>``, bare ``<channel>:<ts>``, and bare ``<ts>`` forms.
    """

    @pytest.mark.parametrize(
        "note",
        [
            "posted slack:C0B36P8LU86:1717603200.123456",
            "posted C0B36P8LU86:1717603200.123456",
            "posted the answer at 1717603200.123456",
        ],
    )
    def test_slack_ts_forms_are_resolvable_pointers(self, note: str) -> None:
        assert has_resolvable_pointer(note) is True

    @pytest.mark.parametrize(
        "note",
        [
            "posted slack:C0B36P8LU86:1717603200.123456",
            "posted C0B36P8LU86:1717603200.123456",
            "posted the answer at 1717603200.123456",
        ],
    )
    def test_answerer_shaped_completion_passes(self, note: str) -> None:
        check_completion_evidence(note)  # does not raise

    @pytest.mark.parametrize(
        ("note", "expected"),
        [
            (
                "posted slack:C0B36P8LU86:1717603200.123456",
                "posted https://slack.com/archives/C0B36P8LU86/p1717603200123456",
            ),
            (
                "answer at C0B36P8LU86:1717603200.123456",
                "answer at https://slack.com/archives/C0B36P8LU86/p1717603200123456",
            ),
        ],
    )
    def test_channel_bearing_forms_normalize_to_permalink(self, note: str, expected: str) -> None:
        assert normalize_artifact_pointers(note) == expected

    def test_bare_ts_is_left_as_is(self) -> None:
        # No channel to build a permalink from — the bare ts is still a pointer
        # but cannot be rewritten to an archives URL.
        note = "posted at 1717603200.123456"
        assert normalize_artifact_pointers(note) == note

    def test_normalize_leaves_a_real_url_untouched(self) -> None:
        note = "shipped via https://example.com/mr/77"
        assert normalize_artifact_pointers(note) == note

    def test_a_plain_decimal_is_not_a_slack_ts(self) -> None:
        # A short or non-Slack-shaped decimal must not masquerade as a ts pointer.
        assert has_resolvable_pointer("merged 3.14 release") is False
