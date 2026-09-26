"""The tri-state MANUAL override layer over the preset (A3).

``t3 loop override <name> on|off --reason '<why>'`` writes ``Loop.enabled``, which beats
the preset in both directions while a durable hold still beats everything. Resolution
order: hold > manual override > preset. Nothing expires it — ``expected_lift_at`` is what
the watcher reminds against (A5/A7), and the reason is what makes a proposal possible at
all (A8).
"""

import datetime as dt

import pytest
from django.utils import timezone

from teatree.core.models import Loop
from teatree.loop.loop_state_db import loop_state_admits, manual_override_map

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _loop(name: str) -> Loop:
    loop, _ = Loop.objects.update_or_create(
        name=name, defaults={"script": f"src/teatree/loops/{name}/loop.py", "delay_seconds": 60}
    )
    return loop


class TestAdmissionMatrix:
    def test_hold_beats_a_manual_force_on(self) -> None:
        assert loop_state_admits(held=True, manual=True, preset_state=True) is False

    def test_a_manual_force_on_beats_a_preset_that_says_off(self) -> None:
        assert loop_state_admits(held=False, manual=True, preset_state=False) is True

    def test_a_manual_force_off_beats_a_preset_that_says_on(self) -> None:
        assert loop_state_admits(held=False, manual=False, preset_state=True) is False

    def test_no_override_hands_the_decision_to_the_preset(self) -> None:
        assert loop_state_admits(held=False, manual=None, preset_state=True) is True
        assert loop_state_admits(held=False, manual=None, preset_state=False) is False


class TestTheOverrideCarriesItsReason:
    def test_setting_one_without_a_reason_is_refused(self) -> None:
        _loop("review")

        with pytest.raises(ValueError, match="must carry the reason"):
            Loop.objects.set_manual_override("review", runs=False)

        assert Loop.objects.get(name="review").enabled is None

    def test_the_reason_and_the_expected_lift_are_stored_beside_the_value(self) -> None:
        _loop("review")
        lift_by = timezone.now() + dt.timedelta(hours=2)

        Loop.objects.set_manual_override("review", runs=False, reason="pr:https://x/1", expected_lift_at=lift_by)

        row = Loop.objects.get(name="review")
        assert (row.enabled, row.override_reason, row.override_expected_lift_at) == (False, "pr:https://x/1", lift_by)
        assert row.override_set_at is not None

    def test_clearing_it_drops_the_reason_with_the_value(self) -> None:
        _loop("review")
        Loop.objects.set_manual_override("review", runs=True, reason="incident")

        Loop.objects.set_manual_override("review", runs=None)

        row = Loop.objects.get(name="review")
        assert (row.enabled, row.override_reason, row.override_set_at) == (None, "", None)

    def test_a_past_expected_lift_does_not_expire_the_override(self) -> None:
        """The A5/A7 inversion: an override nobody lifted is worse than one that vanished."""
        _loop("review")
        Loop.objects.set_manual_override(
            "review", runs=False, reason="incident", expected_lift_at=timezone.now() - dt.timedelta(days=30)
        )

        assert manual_override_map()["review"] is False


class TestTheBulkRead:
    def test_only_loops_carrying_an_override_appear(self) -> None:
        _loop("review")
        _loop("ship")
        Loop.objects.set_manual_override("review", runs=True, reason="incident")

        assert manual_override_map() == {"review": True}
