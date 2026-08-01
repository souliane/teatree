"""`work_group_generic_scopes` reaches the grouper as the frozenset it takes."""

from unittest.mock import patch

from teatree.config import UserSettings
from teatree.core.review.work_group import group_members
from teatree.core.review.work_group_settings import generic_scopes_from_settings


class TestGenericScopesFromSettings:
    def test_the_configured_scopes_are_the_resolved_frozenset(self) -> None:
        with patch(
            "teatree.core.review.work_group_settings.get_effective_settings",
            return_value=UserSettings(work_group_generic_scopes=["ci", "chore"]),
        ):
            assert generic_scopes_from_settings("acme") == frozenset({"ci", "chore"})

    def test_a_configured_scope_stops_grouping_on_it(self) -> None:
        items = [("mr/1", "feat(pipeline): a"), ("mr/2", "fix(pipeline): b")]
        with patch(
            "teatree.core.review.work_group_settings.get_effective_settings",
            return_value=UserSettings(work_group_generic_scopes=["pipeline"]),
        ):
            scopes = generic_scopes_from_settings()
        assert set(group_members(items, generic_scopes=scopes).values()) == {
            frozenset({"mr/1"}),
            frozenset({"mr/2"}),
        }
