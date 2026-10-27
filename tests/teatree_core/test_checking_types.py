from teatree.core import checking
from teatree.core.checking_types import CheckGroup, CheckItem, CheckItemDict, OwnerWaitItem


def test_moved_renderer_preserves_cap_and_link() -> None:
    items = [
        CheckItem(label=f"#55{i}", url=f"https://github.com/acme/widgets/issues/55{i}", title="work") for i in range(6)
    ]
    lines = CheckGroup("Remaining", items, total=6).render()
    assert len(lines) == 7
    assert "[#550 (work)](https://github.com/acme/widgets/issues/550)" in lines[1]
    assert lines[-1] == "  …and 1 more"


def test_checking_keeps_its_public_item_dict_import() -> None:
    assert checking.CheckItemDict is CheckItemDict


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
