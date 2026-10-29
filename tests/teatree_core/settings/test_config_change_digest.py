"""Setting-change rows rendered for the owner digest."""

from datetime import UTC, datetime, timedelta

from teatree.core.models import ConfigSettingChange
from teatree.core.settings.config_change_digest import render_change_rows


def test_digest_rows_show_previous_value_writer_and_age_without_internal_state() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    change = ConfigSettingChange(
        key="agent_max_turns",
        scope="",
        action="set",
        old_value=250,
        new_value=1000,
        written_by="review-loop",
        changed_at=now - timedelta(hours=2),
    )
    internal = ConfigSettingChange(
        key="host_projection_generation",
        scope="",
        action="set",
        old_value=1,
        new_value=2,
        written_by="internal",
        changed_at=now,
    )
    rows = render_change_rows([change, internal], now=now)
    assert rows == [["agent_max_turns", "global", "250 → 1000", "review-loop", "2h ago"]]
