"""Owner-facing rows for recent runtime settings changes."""

from collections.abc import Iterable
from datetime import datetime

from teatree.config.stored_row_health import INTERNAL_STATE_KEYS
from teatree.core.config_display import masked_display
from teatree.core.models.config_setting import scope_label
from teatree.core.models.config_setting_change import ConfigSettingChange

_INTERNAL_KEYS = frozenset(entry.key for entry in INTERNAL_STATE_KEYS)


def render_change_rows(changes: Iterable[ConfigSettingChange], *, now: datetime) -> list[list[str]]:
    rows: list[list[str]] = []
    for change in changes:
        if change.key in _INTERNAL_KEYS:
            continue
        old = masked_display(change.key, change.old_value)
        new = "cleared" if change.action == "clear" else masked_display(change.key, change.new_value)
        age = max(0, int((now - change.changed_at).total_seconds() // 3600))
        rows.append([change.key, scope_label(change.scope), f"{old} → {new}", change.written_by, f"{age}h ago"])
    return rows
