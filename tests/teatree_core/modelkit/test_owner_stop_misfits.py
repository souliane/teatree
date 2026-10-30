import pytest

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.owner_stop_misfits import OWNER_STOP_MISFITS, MisfitAction, owner_stop_misfit
from tests._owner_channel import BENIGN_STOPS, OWNER_KEPT_LOOKALIKES, UNLISTED_WORDING, owner_stop, stop_with_wording

ENVIRONMENT = MisfitAction.ENVIRONMENT_FAILURE
REFUSE = MisfitAction.REFUSE_AND_DECIDE
OWN_PUSH = "May I push the finished branch once more?"
OWN_PROCESS = "May I stop the dev server I started for this check?"
DENIED_START = "The sub-agent start was denied by the current route."
RETRY = "May I start the sub-agent again?"


def _misfit(kind: str, question: str, *facts: str) -> MisfitAction | None:
    return owner_stop_misfit(owner_stop(question, kind, *facts))


class TestTheTableNeverReachesAMoneyOrColleagueDecision:
    def test_only_public_post_and_irreversible_rows_refuse(self) -> None:
        refusing = {row.decision for row in OWNER_STOP_MISFITS if row.action is REFUSE}

        assert refusing == {OwnerDecision.PUBLIC_POST, OwnerDecision.IRREVERSIBLE}

    def test_money_has_no_row_and_credentials_never_refuse(self) -> None:
        assert OwnerDecision.MONEY_OR_PLAN not in {row.decision for row in OWNER_STOP_MISFITS}
        assert [row.action for row in OWNER_STOP_MISFITS if row.decision is OwnerDecision.CREDENTIALS] == [ENVIRONMENT]


class TestARowFiresOnlyOnItsOwnKindAndWords:
    def test_automatic_review_refusal_is_environment_only_when_facts_name_the_refused_test(self) -> None:
        question = "Can I have one-time approval to rerun the focused regression tests?"
        refused = "The automatic approval review refused the focused regression tests as foreground heavy work."
        credential = f"{refused} An admin token is required."

        assert _misfit("credentials", question, refused) is MisfitAction.BACKGROUND_RETRY
        assert _misfit("credentials", question, credential) is None

    def test_automatic_review_classifies_a_new_wording_but_keeps_real_access_decisions(self) -> None:
        fact = "The automatic approval review rejected the test command as foreground heavy work."
        routine = owner_stop("Can I rerun the tests in the background?", "credentials", fact)
        credential = owner_stop("Can I use an admin token to run the tests?", "credentials", fact)
        altered = owner_stop("Can I rerun the tests in the background?", "credentials", fact)
        card = altered["user_input_card"]
        assert isinstance(card, dict)
        card["why"] = "A deployment token is missing."

        assert owner_stop_misfit(routine) is MisfitAction.BACKGROUND_RETRY
        assert owner_stop_misfit(credential) is None
        assert owner_stop_misfit(altered) is None

    @pytest.mark.parametrize(
        ("field", "wording"),
        [
            ("why", "This spends five thousand dollars on a paid plan."),
            ("blocker", "The proposed message posts to the whole team in the owner's name."),
            ("label", "Delete every customer record permanently"),
            ("description", "The production database would be permanently deleted."),
            ("fact", "The automatic approval review refused the test command; deleting production data follows."),
        ],
    )
    def test_automatic_review_keeps_unrelated_owner_decisions(self, field: str, wording: str) -> None:
        result = owner_stop(
            "Can I rerun the tests in the background?",
            "credentials",
            "The automatic approval review rejected the test command as foreground heavy work.",
        )
        card = result["user_input_card"]
        assert isinstance(card, dict)
        if field == "fact":
            result["user_input_checked"] = [wording]
        elif field in {"why", "blocker"}:
            card[field] = wording
        else:
            card["options"][0][field] = wording

        assert owner_stop_misfit(result) is None

    @pytest.mark.parametrize(
        ("kind", "question", "facts", "expected"),
        [
            ("credentials", RETRY, (DENIED_START,), ENVIRONMENT),
            ("credentials", "May I retry the denied command?", (), ENVIRONMENT),
            ("credentials", "May I retry?", ("The shell command was rejected by the policy.",), ENVIRONMENT),
            ("credentials", "May I retry?", ("The review start was blocked by the venue.",), ENVIRONMENT),
            ("public_post", OWN_PUSH, (), REFUSE),
            ("public_post", "May I push my own pull-request branch again?", (), REFUSE),
            ("irreversible", OWN_PROCESS, (), REFUSE),
            ("irreversible", "May I kill the job this task started?", (), REFUSE),
            ("irreversible", "May I restart the worker I started?", (), REFUSE),
            ("irreversible", "May I terminate the container I launched for this run?", (), REFUSE),
        ],
    )
    def test_each_row_matches_its_kind(
        self, kind: str, question: str, facts: tuple[str, ...], expected: MisfitAction
    ) -> None:
        assert _misfit(kind, question, *facts) is expected

    @pytest.mark.parametrize(("kind", "question", "facts"), OWNER_KEPT_LOOKALIKES)
    def test_a_stop_outside_the_closed_forms_is_a_card(self, kind: str, question: str, facts: tuple[str, ...]) -> None:
        assert _misfit(kind, question, *facts) is None

    @pytest.mark.parametrize("question", ["May I stop my own server?", "May I stop the server of my own?"])
    def test_the_word_own_alone_does_not_qualify_a_process_stop(self, question: str) -> None:
        assert _misfit("irreversible", question) is None

    @pytest.mark.parametrize(("kind", "field", "wording"), UNLISTED_WORDING)
    def test_one_unlisted_wording_in_any_field_of_a_benign_stop_is_a_card(
        self, kind: str, field: str, wording: str
    ) -> None:
        question, facts = BENIGN_STOPS[kind]

        assert owner_stop_misfit(owner_stop(question, kind, *facts)) is not None
        assert owner_stop_misfit(stop_with_wording(kind, field, wording)) is None

    @pytest.mark.parametrize(
        ("kind", "question", "facts"),
        [
            ("credentials", RETRY, ()),
            ("credentials", "May I retry?", ("The command ran fine.",)),
            ("credentials", "May I retry?", ("The request returned a 403 after a timeout.",)),
            ("credentials", "May I retry?", ("Access was denied.",)),
            ("public_post", "May I push to a colleague's branch?", ()),
            ("public_post", "May I push the branch?", ()),
            ("irreversible", "May I stop the production server?", ()),
            ("irreversible", "May I delete work that exists on no remote?", ()),
        ],
    )
    def test_a_missing_marker_is_a_card(self, kind: str, question: str, facts: tuple[str, ...]) -> None:
        assert _misfit(kind, question, *facts) is None

    @pytest.mark.parametrize("kind", ["money_or_plan", "irreversible", "public_post", "product_scope", "architecture"])
    def test_the_denied_start_retry_means_nothing_for_another_kind(self, kind: str) -> None:
        assert _misfit(kind, RETRY, DENIED_START) is None

    @pytest.mark.parametrize("kind", ["credentials", "money_or_plan", "product_scope", "architecture"])
    def test_the_own_branch_push_and_own_process_stop_mean_nothing_for_another_kind(self, kind: str) -> None:
        assert _misfit(kind, OWN_PUSH) is None
        assert _misfit(kind, OWN_PROCESS) is None

    def test_a_stop_without_the_stop_flag_or_a_kind_is_no_misfit(self) -> None:
        stop = owner_stop(OWN_PUSH, "public_post")

        assert owner_stop_misfit(stop | {"needs_user_input": False}) is None
        assert owner_stop_misfit({k: v for k, v in stop.items() if k != "user_input_kind"}) is None
