# test-path: cross-cutting
# Spans teatree.config (retired_settings, seed_defaults) and
# teatree.loops.issue_implementer — a genuine multi-package contract test.
"""Retiring ``issue_implementer_cadence_hours`` moved no loop's cadence (#4203).

The #4203 hard constraint: no ``*_cadence_hours`` key may be cut before the
``Loop.delay_seconds`` behind it is correct, because cutting a key that DOES feed a
cadence silently reschedules the loop (``architectural_review_cadence_hours`` would have
turned a weekly AI review into a 3-hourly one, ~56x the cost).

``issue_implementer_cadence_hours`` was the safe member of that family precisely because
nothing read it: the live cadence is the seeded ``Loop`` row, and the mini-loop's own
``default_cadence_seconds`` is a seed hint. The two numbers disagreed (1800 vs 3600) the
whole time the setting claimed 1h, which is what proves the setting fed neither. This
module pins both so the retirement cannot be mistaken for the cadence change it is not.
"""

from teatree.config.retired_settings import REMOVED_SETTING_KEYS
from teatree.config.seed_defaults import shipped_seed_table
from teatree.loops.issue_implementer.loop import MINI_LOOP

#: The cadence the loop actually ran at before the key was cut, and must still run at.
LIVE_CADENCE_SECONDS = 1800

#: The mini-loop's documentation-only seed hint, deliberately left untouched by the cut.
SEED_HINT_SECONDS = 3600


def test_the_retired_key_is_recorded_as_removed() -> None:
    assert "issue_implementer_cadence_hours" in REMOVED_SETTING_KEYS


def test_the_shipped_loop_row_still_carries_the_live_cadence() -> None:
    """The DB ``Loop`` row is the single cadence source, and the cut did not move it."""
    assert shipped_seed_table("loops")["issue_implementer"]["delay_seconds"] == LIVE_CADENCE_SECONDS


def test_the_seed_hint_is_unchanged_and_was_never_the_live_cadence() -> None:
    """A hint that disagrees with the live cadence cannot have been feeding it."""
    assert MINI_LOOP.default_cadence_seconds == SEED_HINT_SECONDS
    assert MINI_LOOP.default_cadence_seconds != LIVE_CADENCE_SECONDS
