import pytest

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.owner_stop_misfits import OWNER_STOP_MISFITS, MisfitAction, owner_stop_misfit
from tests._owner_channel import owner_stop

ENVIRONMENT = MisfitAction.ENVIRONMENT_FAILURE
REFUSE = MisfitAction.REFUSE_AND_DECIDE
OWN_PUSH = "May I push the finished branch once more?"
OWN_PROCESS = "May I stop the dev server I started for this check?"
DENIED_START = "The sub-agent start was denied by the current route."


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
    @pytest.mark.parametrize(
        ("kind", "question", "facts", "expected"),
        [
            ("credentials", "Can you provide bot access for live proof?", (DENIED_START,), ENVIRONMENT),
            ("credentials", "May I retry?", ("The shell command was rejected by the policy.",), ENVIRONMENT),
            ("credentials", "May I retry?", ("The review start was blocked by the venue.",), ENVIRONMENT),
            ("public_post", OWN_PUSH, (), REFUSE),
            ("public_post", "May I push my own pull-request branch again?", (), REFUSE),
            ("irreversible", OWN_PROCESS, (), REFUSE),
            ("irreversible", "May I kill the job this task started?", (), REFUSE),
        ],
    )
    def test_each_row_matches_its_kind(
        self, kind: str, question: str, facts: tuple[str, ...], expected: MisfitAction
    ) -> None:
        assert _misfit(kind, question, *facts) is expected

    @pytest.mark.parametrize("word", ["secret", "token", "password", "key", "login", "account", "grant", "widen"])
    def test_a_credential_word_leaves_the_denied_command_a_card(self, word: str) -> None:
        assert _misfit("credentials", f"May I use another {word}?", DENIED_START) is None

    @pytest.mark.parametrize("word", ["comment", "message", "reply", "colleague"])
    def test_a_colleague_word_leaves_the_own_branch_push_a_card(self, word: str) -> None:
        assert _misfit("public_post", f"{OWN_PUSH} Then {word} on it.") is None

    @pytest.mark.parametrize("word", ["delete", "deleting", "deletion", "discard", "unpushed", "uncommitted", "lose"])
    def test_a_destruction_word_leaves_the_own_process_stop_a_card(self, word: str) -> None:
        assert _misfit("irreversible", f"{OWN_PROCESS} It would {word} its data.") is None

    @pytest.mark.parametrize(
        ("kind", "question", "facts"),
        [
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
    def test_the_denied_start_wording_means_nothing_for_another_kind(self, kind: str) -> None:
        assert _misfit(kind, "May I spend money on this service?", DENIED_START) is None

    def test_a_stop_without_the_stop_flag_or_a_kind_is_no_misfit(self) -> None:
        stop = owner_stop(OWN_PUSH, "public_post")

        assert owner_stop_misfit(stop | {"needs_user_input": False}) is None
        assert owner_stop_misfit({k: v for k, v in stop.items() if k != "user_input_kind"}) is None
