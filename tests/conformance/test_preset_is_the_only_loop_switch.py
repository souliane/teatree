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

from teatree.config.retired_settings import removed_setting
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


@pytest.mark.parametrize("key", EXISTENCE_SCALARS)
def test_a_stale_stored_row_answers_for_itself_rather_than_reverting_in_silence(key: str) -> None:
    entry = removed_setting(key)
    assert entry is not None, f"{key} must be recorded as retired (#3527)"
    assert "preset" in entry.reason


class TestAJobToggleInsideARunningLoopSurvives:
    """The control: ``resource_pressure`` builds THREE scanners, and only one is the loop.

    ``adaptive_intake_concurrency_enabled`` names the second job, which the preset cannot
    address — a preset admits the loop or it does not. A sweep of every ``*_enabled`` key
    would take it too, so this is what separates the criterion from a keyword match.
    """

    def test_the_job_toggle_is_still_a_setting(self) -> None:
        assert "adaptive_intake_concurrency_enabled" in TeatreeSettingsSchema.model_fields

    def test_it_still_suppresses_its_own_scanner_while_the_loop_runs(self) -> None:
        from teatree.loop import global_scanner_factories  # noqa: PLC0415 — test-local: heavy scanner imports

        settings = UserSettings(adaptive_intake_concurrency_enabled=False)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(global_scanner_factories, "get_effective_settings", lambda: settings)
            assert global_scanner_factories._intake_concurrency_scanner() is None
            assert global_scanner_factories._resource_pressure_scanner() is not None

    def test_the_third_job_carries_no_toggle_at_all(self) -> None:
        """The artifact sweep (#4244) is unconditional — a proof, not a switch.

        The counterpart to the intake toggle above: a job may be a setting when the preset
        cannot address it, but it may not be one merely because it deletes. Every deletion
        there is proved reconstructible and anything unprovable is kept, so a flag in front
        of it adds no safety and an off-by-default one only guarantees it never runs.
        """
        from teatree.loop import global_scanner_factories  # noqa: PLC0415 — test-local: heavy scanner imports

        assert "artifact_eviction_enabled" not in TeatreeSettingsSchema.model_fields
        assert global_scanner_factories._artifact_eviction_scanner() is not None
