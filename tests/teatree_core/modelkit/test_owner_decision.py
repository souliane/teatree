from teatree.core.modelkit.owner_decision import OWNER_QUESTION_ROUTE, OwnerDecision, owner_decision


class TestOwnerQuestionRoute:
    def test_names_the_record_command_with_its_checked_evidence(self) -> None:
        assert "questions record" in OWNER_QUESTION_ROUTE
        assert "--decision <kind>" in OWNER_QUESTION_ROUTE
        assert "--checked" in OWNER_QUESTION_ROUTE

    def test_names_the_card_flags_the_option_shape_and_the_plain_language_rules(self) -> None:
        for fragment in ("--why", "--blocker", "--options", '"recommended": true', "recommended one first"):
            assert fragment in OWNER_QUESTION_ROUTE, fragment
        assert "at most 120 words" in OWNER_QUESTION_ROUTE
        assert "plain English everywhere, quotes included" in OWNER_QUESTION_ROUTE

    def test_names_every_kind_and_files_an_access_grant_under_credentials(self) -> None:
        assert all(kind.value in OWNER_QUESTION_ROUTE for kind in OwnerDecision)
        assert "access or permission grant is credentials" in OWNER_QUESTION_ROUTE


class TestOwnerDecision:
    def test_parses_a_known_kind_and_rejects_anything_else(self) -> None:
        assert owner_decision("architecture") is OwnerDecision.ARCHITECTURE
        assert owner_decision("whim") is None
