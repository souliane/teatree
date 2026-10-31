"""The self-improve owner-wait parser keeps a usable value and otherwise returns the field's own default."""

from unittest.mock import patch

import pytest

from teatree.config.settings_loop_owned import _SelfImproveLoopSettings, parse_self_improve_owner_wait_minutes


@pytest.mark.parametrize("raw", [1, 30, 1440])
def test_a_value_inside_the_allowed_range_is_kept(raw: int) -> None:
    assert parse_self_improve_owner_wait_minutes(raw) == raw


@pytest.mark.parametrize("raw", ["soon", 0, -3, 1441, True, None, 2.5])
def test_an_unusable_value_falls_back_to_the_fields_own_default(raw: object) -> None:
    with patch.object(_SelfImproveLoopSettings, "self_improve_owner_wait_minutes", 45):
        assert parse_self_improve_owner_wait_minutes(raw) == 45
