"""Public report item serialization and rendering."""

from teatree.core.checking_types import CheckGroup, CheckItem, OwnerWaitItem


def test_owner_wait_metadata_survives_group_serialization_without_changing_other_items() -> None:
    owner = OwnerWaitItem(
        label="Q4: May I proceed?",
        url="https://example.com/pull/4",
        kind="question",
        age_days=7,
        row_id=4,
        owner_only=True,
    )
    ordinary = CheckItem(label="work", url="https://example.com/work")

    owner_payload = owner.to_dict()
    payload = CheckGroup(title="Needs you", items=[owner, ordinary], total=2).to_dict()

    assert owner_payload["kind"] == "question"
    assert owner_payload["age_days"] == 7
    assert owner_payload["row_id"] == 4
    assert owner_payload["owner_only"] is True
    assert payload["items"][0] == owner_payload
    assert payload["items"][1] == {"label": "work", "url": "https://example.com/work", "detail": "", "title": ""}
