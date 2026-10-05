# test-path: cross-cutting — checks the parser registry against the pinned settings field inventory.
"""DB-home parser keys must remain fields in the pinned settings schema."""

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS

from .test_settings_field_golden import GOLDEN_USER_SETTINGS_FIELDS


def test_every_db_home_key_is_a_pinned_user_settings_field() -> None:
    unpinned = set(OVERLAY_OVERRIDABLE_SETTINGS) - GOLDEN_USER_SETTINGS_FIELDS
    assert not unpinned, f"DB-home keys absent from the UserSettings golden: {sorted(unpinned)}"
