"""The ``pydantic_ai`` per-run request cap is a REAL turn budget, not a 5-turn floor.

Lane B measured ~16.4 model requests per task, so a default of 5 refused mid-task.
The default is a generous budget well above that reality; a positive caller
``max_turns`` (an ``OneShotSpec`` cap / an eval override) still wins over it.
"""

from teatree.agents.harness_options import HarnessOptions
from teatree.config import UserSettings

# The measured mean turn count Lane B refused below. The default must clear it with headroom.
_MEASURED_TURN_REALITY = 16


class TestRequestLimitDefault:
    def test_default_is_a_real_turn_budget_above_the_measured_reality(self) -> None:
        assert UserSettings().pydantic_ai_request_limit > _MEASURED_TURN_REALITY

    def test_positive_caller_max_turns_still_wins_over_the_lane_default(self) -> None:
        # The harness prefers a positive OneShotSpec/eval max_turns over the lane cap;
        # 0 (a headless dispatch coercing SDK-None) keeps the lane default.
        assert HarnessOptions(max_turns=3).max_turns == 3
        assert HarnessOptions(max_turns=0).max_turns == 0
