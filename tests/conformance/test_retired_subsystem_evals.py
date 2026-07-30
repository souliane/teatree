"""Retired-subsystem ↔ eval-catalog conformance — the two ledgers must agree.

souliane/teatree#3734 retired the agent-teams pane layer and recorded it in
``config/retired_settings.py``. Its two eval scenarios were not retired with it,
so the metered lane kept selecting, grading and paying for behaviour that no
longer exists: ``team_mate_spawned_opus_never_sonnet`` exhausted its whole
``max_budget_usd: 4.0`` cap and failed, every time it was measured
(souliane/teatree#3839). Nothing connected the removal ledger to the catalog, so
the omission could only be discovered by spending money on it.

This lane is that connection. A retirement entry that names a *subsystem* claims
the behaviour is gone; a scenario named for that subsystem claims it is still
gradeable. Both cannot be true, so the next retirement carries its evals with it
or this test turns the PR red on the push path.

Matching is at word level over the scenario name (``team_mate_…`` splits to
``team``, ``mate``, …), never substring: ``legitimate_…`` contains "mate" and
must not be flagged. A retirement whose subsystem is still live declares
``subsystem=None`` — ``branch_prefix`` retired the setting while branch prefixes
kept resolving, and ``subagent_prompt_drift_branch_prefix`` grades that live
behaviour.
"""

from teatree.config.retired_settings import RETIRED_SUBSYSTEMS
from teatree.eval.discovery import discover_specs


def _name_words(scenario_name: str) -> set[str]:
    return {word.removesuffix("s") for word in scenario_name.split("_")}


def _retired_subsystems_exercised(scenario_name: str, subsystems: frozenset[str]) -> set[str]:
    words = _name_words(scenario_name)
    return {subsystem for subsystem in subsystems if subsystem.removesuffix("s") in words}


class TestNoScenarioGradesARetiredSubsystem:
    def test_live_catalog_grades_no_retired_subsystem(self) -> None:
        offenders = {
            spec.name: sorted(_retired_subsystems_exercised(spec.name, RETIRED_SUBSYSTEMS))
            for spec in discover_specs()
            if _retired_subsystems_exercised(spec.name, RETIRED_SUBSYSTEMS)
        }
        assert not offenders, (
            f"eval scenarios grade subsystems retired in config/retired_settings.py: {offenders}. "
            "The behaviour no longer exists, so the scenario can only burn its budget and fail — "
            "delete the scenario, its fixtures and its replay test, or drop the retirement's "
            "`subsystem` claim if the behaviour is in fact still live."
        )

    def test_a_scenario_named_for_a_retired_subsystem_is_flagged(self) -> None:
        assert _retired_subsystems_exercised("team_mate_spawned_opus_never_sonnet", frozenset({"team"})) == {"team"}
        assert _retired_subsystems_exercised("team_mode_delegates_to_fixed_roster", frozenset({"team"})) == {"team"}

    def test_plural_and_singular_forms_are_the_same_subsystem(self) -> None:
        assert _retired_subsystems_exercised("teams_roster_is_fixed", frozenset({"team"})) == {"team"}
        assert _retired_subsystems_exercised("team_roster_is_fixed", frozenset({"teams"})) == {"teams"}

    def test_an_unrelated_scenario_name_is_not_flagged(self) -> None:
        assert _retired_subsystems_exercised("full_speed_fans_out_parallel_workers", frozenset({"team"})) == set()

    def test_a_substring_collision_is_not_flagged(self) -> None:
        assert _retired_subsystems_exercised("legitimate_missing_fact_question", frozenset({"mate"})) == set()
        assert _retired_subsystems_exercised("teamwork_under_load", frozenset({"team"})) == set()


class TestLaneCannotGoVacuous:
    def test_at_least_one_subsystem_is_registered(self) -> None:
        assert RETIRED_SUBSYSTEMS, (
            "no retirement declares a subsystem, so the catalog walk above asserts nothing — "
            "an emptied ledger must not read as a green lane"
        )

    def test_the_catalog_walk_sees_scenarios(self) -> None:
        assert len(discover_specs()) > 100, "scenario discovery returned an implausibly small catalog"
