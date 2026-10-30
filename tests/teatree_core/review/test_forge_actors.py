import pytest

from teatree.core.review.forge_actors import ForgeActors


@pytest.mark.parametrize(
    ("login", "user_type", "system", "expected"),
    [
        ("FACTORY-BOT", "", False, True),
        ("Project_42_BOT_A1", "", False, True),
        ("Dependabot[Bot]", "", False, True),
        ("worker", "Bot", False, True),
        ("", "", False, True),
        ("bob", "", True, True),
        ("bob", "User", False, False),
        ("chatbot_x", "", False, False),
    ],
)
def test_forge_actors_classifies_automation(*, login: str, user_type: str, system: bool, expected: bool) -> None:
    actors = ForgeActors(system_logins=frozenset({"factory-bot"}))
    assert actors.is_automation(login, user_type=user_type, system=system) is expected
