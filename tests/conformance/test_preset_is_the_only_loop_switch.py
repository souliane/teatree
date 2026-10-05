"""A loop's EXISTENCE is the preset's opinion and nothing else's (D6).

A preset holds a true/false opinion on every live loop, so a setting that also decides
whether a loop RUNS is a second answer to a settled question — and the two can disagree,
which from outside reads as "the loop is on but nothing happens". These nine each gated
the whole of one loop's only job.

The criterion is existence, not behaviour, and the control below is what keeps it from
becoming a sweep of every ``*_enabled`` key: a toggle naming a JOB a running loop does is
not expressible as a preset opinion and stays.
"""

import pytest

from teatree.config.schema import TeatreeSettingsSchema
from teatree.config.settings import UserSettings

#: The nine scalars, each the sole gate on one loop's only scanner.
EXISTENCE_SCALARS = (
    "backlog_sweep_disabled",
    "db_backup_disabled",
    "dogfood_smoke_disabled",
    "eval_local_disabled",
    "idle_stack_reaper_disabled",
    "local_stack_queue_disabled",
    "resource_pressure_disabled",
    "scanning_news_disabled",
    "snapshot_warmer_disabled",
)


@pytest.mark.parametrize("key", EXISTENCE_SCALARS)
def test_no_loop_existence_scalar_survives_as_a_setting(key: str) -> None:
    assert key not in TeatreeSettingsSchema.model_fields
    assert not hasattr(UserSettings(), key)


class TestAJobToggleInsideARunningLoopSurvives:
    """The resource-pressure loop builds its safe jobs without an extra switch."""

    def test_the_intake_job_has_no_toggle(self) -> None:
        assert "adaptive_intake_concurrency_enabled" not in TeatreeSettingsSchema.model_fields

    def test_the_intake_scanner_runs_while_the_loop_runs(self) -> None:
        from teatree.loop import global_scanner_factories  # noqa: PLC0415 — test-local: heavy scanner imports

        settings = UserSettings()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(global_scanner_factories, "get_effective_settings", lambda: settings)
            assert global_scanner_factories._intake_concurrency_scanner() is not None
            assert global_scanner_factories._resource_pressure_scanner() is not None

    def test_the_third_job_carries_no_toggle_at_all(self) -> None:
        """The artifact sweep (#4244) is unconditional — a proof, not a switch.

        Every deletion
        there is proved reconstructible and anything unprovable is kept, so a flag in front
        of it adds no safety and an off-by-default one only guarantees it never runs.
        """
        from teatree.loop import global_scanner_factories  # noqa: PLC0415 — test-local: heavy scanner imports

        assert "artifact_eviction_enabled" not in TeatreeSettingsSchema.model_fields
        assert global_scanner_factories._artifact_eviction_scanner() is not None
