from teatree.core import checking
from teatree.core.checking_types import CheckGroup, CheckItem, CheckItemDict


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
