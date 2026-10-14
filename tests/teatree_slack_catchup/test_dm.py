"""Only the owner's reactions on DM messages become replay events."""

from teatree.slack_catchup.dm import owner_reactions


def test_owner_reaction_keeps_the_original_dm_identity() -> None:
    message = {
        "ts": "1700000000.000100",
        "reactions": [
            {"name": "+1", "users": ["U_OWNER", "U_OTHER"]},
            {"name": "eyes", "users": ["U_OTHER"]},
        ],
    }

    assert owner_reactions(message, owner="U_OWNER", channel="D_OWNER") == [
        {
            "type": "reaction_added",
            "user": "U_OWNER",
            "reaction": "+1",
            "item": {"type": "message", "channel": "D_OWNER", "ts": "1700000000.000100"},
        }
    ]


def test_missing_owner_or_reaction_list_yields_no_events() -> None:
    assert owner_reactions({"reactions": [{"name": "+1", "users": ["U_OWNER"]}]}, owner="", channel="D") == []
    assert owner_reactions({"reactions": "broken"}, owner="U_OWNER", channel="D") == []
