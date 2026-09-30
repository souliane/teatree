"""Decision core for the standing-grant sign-off ask gate (#2663 dream gap b79bafc7)."""

import pytest

from teatree.core.gates.standing_grant_ask_gate import (
    StandingGrantAsk,
    deny_reason,
    find_standing_grant_ask,
    grant_ask_ok_reason,
    repo_refs,
)

_GRANT_COVERED_ASKS = [
    (
        "Do you approve merging substrate PR https://github.com/souliane/teatree/pull/4892"
        " with --human-authorize souliane?"
    ),
    "May I CLEAR substrate PR #4899 now that the cold review is MERGE_SAFE?",
    "Confirm I can use --human-authorize souliane for substrate CLEARs going forward?",
    "OK to land the substrate PR?",
    "Should I merge the substrate PR souliane/teatree#4890 now?",
    "Substrate PR #4892 is MERGE_SAFE and CI is green. Do you approve merging it?",
]

_OUT_OF_GRANT_ASKS = [
    "Authorize the expedite waiver for substrate PR #4805 so it can merge on pending checks?",
    "Phase B needs a TypeSafe API key plus your sign-off on adopting the vendor, OK?",
    "Which target branch, main or develop?",
    "Should I investigate or rework the failing substrate PR?",
    "Should I turn off substrate_self_signoff?",
    "Do you approve closing substrate PR #4840 as a duplicate of #4839 instead of merging it?",
    "Do you approve merging substrate PR #4892 despite the HOLD verdict?",
    "Do you approve the substrate design before I start coding? Merging comes later.",
    "",
]


class TestGrantCoveredAsks:
    @pytest.mark.parametrize("question", _GRANT_COVERED_ASKS)
    def test_a_substrate_merge_sign_off_ask_is_a_finding(self, question: str) -> None:
        finding = find_standing_grant_ask([question])

        assert finding == StandingGrantAsk(question=question)

    def test_any_question_of_a_batched_ask_triggers(self) -> None:
        finding = find_standing_grant_ask(["Which target branch, main or develop?", _GRANT_COVERED_ASKS[0]])

        assert finding is not None
        assert finding.question == _GRANT_COVERED_ASKS[0]


class TestOutOfGrantAsks:
    @pytest.mark.parametrize("question", _OUT_OF_GRANT_ASKS)
    def test_an_ask_the_grant_does_not_cover_is_never_a_finding(self, question: str) -> None:
        assert find_standing_grant_ask([question]) is None

    def test_a_sentence_break_separates_the_substrate_mention_from_the_merge_word(self) -> None:
        question = "The substrate docs are updated. Do you approve merging the dashboard PR?"

        assert find_standing_grant_ask([question]) is None


class TestOkToken:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "[grant-ask-ok: the owner asked to be consulted on this one] May I merge?",
                "the owner asked to be consulted on this one",
            ),
            ("prefix [grant-ask-ok:   spaced   ] suffix", "spaced"),
            ("[grant-ask-ok: ]", None),
            ("no token here", None),
            ("", None),
        ],
    )
    def test_token_extraction(self, text: str, expected: str | None) -> None:
        assert grant_ask_ok_reason(text) == expected

    def test_token_past_the_scan_window_is_ignored(self) -> None:
        assert grant_ask_ok_reason("x" * 600 + "[grant-ask-ok: buried]") is None


class TestRepoRefs:
    def test_forge_urls_and_owner_repo_refs_are_extracted(self) -> None:
        text = "Merge https://github.com/souliane/teatree/pull/4892 and souliane/private-skills!68 or acme/app#7?"

        assert repo_refs(text) == (
            "https://github.com/souliane/teatree/pull/4892",
            "souliane/private-skills",
            "acme/app",
        )

    def test_a_question_naming_no_repo_has_no_refs(self) -> None:
        assert repo_refs("OK to land the substrate PR?") == ()


class TestDenyReason:
    def test_self_signoff_reason_names_the_overlay_and_needs_no_authorizer(self) -> None:
        reason = deny_reason(
            StandingGrantAsk(question="OK to land the substrate PR?"), overlay="t3-teatree", delegated_by=""
        )

        assert "t3-teatree" in reason
        assert "substrate_self_signoff" in reason
        assert "ticket clear" in reason
        assert "ticket merge" in reason
        assert "[grant-ask-ok: <reason>]" in reason
        assert "t3 <overlay> gate standing-grant-ask disable" in reason

    def test_delegation_reason_names_the_configured_authorizer(self) -> None:
        reason = deny_reason(
            StandingGrantAsk(question="OK to land the substrate PR?"), overlay="t3-teatree", delegated_by="souliane"
        )

        assert "substrate_auto_merge_authorized_by" in reason
        assert "--human-authorize souliane" in reason
