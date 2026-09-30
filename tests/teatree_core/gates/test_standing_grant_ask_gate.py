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
    "Do you approve enabling substrate_self_signoff for private-skills so I can merge its substrate PRs?",
    "Do you approve extending the standing grant to substrate merges on the private-skills repo?",
    "Should I merge the dashboard PR - it does not touch substrate?",
    "Can you confirm whether PR #4892 is substrate before I merge?",
    "Do you approve holding substrate PR #4892 instead of merging it?",
    "Do you approve deferring the substrate merge until after the release?",
    "Which owner id should I pass to --human-authorize?",
    "Can you confirm the substrate docs are clear enough?",
    "",
]

_GRANT_COVERED_ASKS_WITH_STATUS_NOISE = [
    "Substrate PR #4892 is rebased, MERGE_SAFE and CI green. Do you approve merging it?",
    "Substrate PR #4892 is rebased, MERGE_SAFE and CI green. Do you approve merging it? It has no conflicts.",
    "Substrate PR #4892 has no pending checks, OK to merge it?",
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


class TestStatusNoiseStillFlags:
    @pytest.mark.parametrize("question", _GRANT_COVERED_ASKS_WITH_STATUS_NOISE)
    def test_a_status_report_in_the_ask_does_not_disable_the_gate(self, question: str) -> None:
        finding = find_standing_grant_ask([question])

        assert finding == StandingGrantAsk(question=question)


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

    def test_an_empty_token_with_a_later_bracket_does_not_escape(self) -> None:
        text = "[grant-ask-ok: ] Do you approve merging substrate PR [#4892]?"

        assert grant_ask_ok_reason(text) is None


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
        assert "--human-authorized souliane" in reason
        assert "ticket merge <clear_id> --human-authorized souliane" in reason

    def test_neither_grant_asks_for_a_per_pr_authorizer_on_the_clear(self) -> None:
        self_signoff_reason = deny_reason(
            StandingGrantAsk(question="OK to land the substrate PR?"), overlay="t3-teatree", delegated_by=""
        )
        delegation_reason = deny_reason(
            StandingGrantAsk(question="OK to land the substrate PR?"), overlay="t3-teatree", delegated_by="souliane"
        )

        assert "clear … --blast-class substrate --human-authorize" not in self_signoff_reason
        assert "clear … --blast-class substrate --human-authorize" not in delegation_reason
        assert "ticket merge <clear_id>`" in self_signoff_reason

    def test_self_signoff_reason_names_the_mcp_merge_tool_before_the_cli_fallback(self) -> None:
        reason = deny_reason(
            StandingGrantAsk(question="OK to land the substrate PR?"), overlay="t3-teatree", delegated_by=""
        )

        assert "mcp__teatree__pr_merge" in reason
        assert reason.index("mcp__teatree__pr_merge") < reason.index("ticket merge <clear_id>`")

    def test_delegation_reason_keeps_the_merge_on_the_cli(self) -> None:
        reason = deny_reason(
            StandingGrantAsk(question="OK to land the substrate PR?"), overlay="t3-teatree", delegated_by="souliane"
        )

        assert "mcp__teatree__pr_merge" not in reason
        assert "ticket merge <clear_id> --human-authorized souliane" in reason
