"""The leaderless-process-group reader (#4580).

Every case PLANTS its own group: the live box carries none, so a reader that
returned an empty list unconditionally would pass a "clean box" assertion. The
silent cases below are only evidence because the RED cases beside them fire.
"""

import os
from pathlib import Path

import pytest

from teatree.core.cleanup.orphan_process_groups import scan_orphan_groups, survey_orphan_groups
from tests._orphan_procfs import PlantedProcess, plant_uptime
from tests._process_table_venue import pinned_process_table

_HZ = os.sysconf("SC_CLK_TCK")
_UPTIME_SECONDS = 100_000.0
_OLD_ENOUGH_SECONDS = 6 * 3600.0
#: Burns ~57% of a core over the planted lifetime — the incident's own ratio.
_BURNING_TICKS = int(_UPTIME_SECONDS * 0.57 * _HZ)


def _table(root: Path) -> Path:
    plant_uptime(root, _UPTIME_SECONDS)
    return root


def _ticks_for_age(age_seconds: float) -> int:
    return int((_UPTIME_SECONDS - age_seconds) * _HZ)


def _scan(root: Path, **kwargs: object) -> list:
    return scan_orphan_groups(root, signalable=True, min_age_seconds=_OLD_ENOUGH_SECONDS, **kwargs)  # type: ignore[arg-type]


class TestDetectionRule:
    def test_old_burning_leaderless_group_is_reported(self, tmp_path: Path) -> None:
        root = _table(tmp_path)
        PlantedProcess(pid=200, pgid=199, cpu_ticks=_BURNING_TICKS, comm="bash").write(root)
        PlantedProcess(pid=201, pgid=199, cpu_ticks=_BURNING_TICKS, comm="wc").write(root)

        groups = _scan(root)

        assert [group.pgid for group in groups] == [199]
        assert len(groups[0].members) == 2
        assert groups[0].signalable is True

    def test_a_live_leader_keeps_its_group_silent(self, tmp_path: Path) -> None:
        root = _table(tmp_path)
        PlantedProcess(pid=199, pgid=199, cpu_ticks=_BURNING_TICKS).write(root)
        PlantedProcess(pid=200, pgid=199, cpu_ticks=_BURNING_TICKS).write(root)

        assert _scan(root) == []

    def test_a_young_leaderless_group_is_silent(self, tmp_path: Path) -> None:
        root = _table(tmp_path)
        young = _ticks_for_age(60.0)
        PlantedProcess(pid=200, pgid=199, cpu_ticks=_BURNING_TICKS, start_ticks=young).write(root)

        assert _scan(root) == []

    def test_an_old_but_idle_leaderless_group_is_silent(self, tmp_path: Path) -> None:
        root = _table(tmp_path)
        PlantedProcess(pid=200, pgid=199, cpu_ticks=0, comm="sleep").write(root)

        assert _scan(root) == []

    def test_a_runnable_member_fires_even_with_no_accumulated_cpu(self, tmp_path: Path) -> None:
        root = _table(tmp_path)
        PlantedProcess(pid=200, pgid=199, state="R", cpu_ticks=0).write(root)

        groups = _scan(root)

        assert [group.pgid for group in groups] == [199]
        assert groups[0].runnable is True

    def test_group_zero_is_never_reported(self, tmp_path: Path) -> None:
        root = _table(tmp_path)
        PlantedProcess(pid=200, pgid=0, state="R", cpu_ticks=_BURNING_TICKS).write(root)

        assert _scan(root) == []

    def test_age_is_the_oldest_member_and_cpu_is_the_sum(self, tmp_path: Path) -> None:
        root = _table(tmp_path)
        PlantedProcess(pid=200, pgid=199, cpu_ticks=_BURNING_TICKS, start_ticks=0).write(root)
        PlantedProcess(pid=201, pgid=199, cpu_ticks=_BURNING_TICKS, start_ticks=_ticks_for_age(120.0)).write(root)

        group = _scan(root)[0]

        assert group.age_seconds == pytest.approx(_UPTIME_SECONDS)
        assert group.cpu_seconds == pytest.approx(2 * _BURNING_TICKS / _HZ)

    def test_a_comm_carrying_spaces_and_parens_still_parses(self, tmp_path: Path) -> None:
        root = _table(tmp_path)
        PlantedProcess(pid=200, pgid=199, comm="sh -c (loop)", state="R").write(root)

        assert [member.comm for member in _scan(root)[0].members] == ["sh -c (loop)"]

    def test_an_unreadable_uptime_reports_nothing_rather_than_guessing(self, tmp_path: Path) -> None:
        PlantedProcess(pid=200, pgid=199, state="R").write(tmp_path)

        assert _scan(tmp_path) == []


class TestNestedNamespacePartition:
    def test_a_nested_namespace_pid_is_excluded_when_asked(self, tmp_path: Path) -> None:
        root = _table(tmp_path)
        PlantedProcess(pid=200, pgid=199, state="R", nspids=(200, 7)).write(root)

        assert _scan(root, exclude_nested_namespaces=True) == []
        assert [group.pgid for group in _scan(root)] == [199]

    def test_a_host_native_pid_survives_the_exclusion(self, tmp_path: Path) -> None:
        root = _table(tmp_path)
        PlantedProcess(pid=200, pgid=199, state="R", nspids=(200,)).write(root)

        assert [group.pgid for group in _scan(root, exclude_nested_namespaces=True)] == [199]

    def test_a_pid_with_no_status_file_is_treated_as_host_native(self, tmp_path: Path) -> None:
        root = _table(tmp_path)
        PlantedProcess(pid=200, pgid=199, state="R").write(root)

        assert [group.pgid for group in _scan(root, exclude_nested_namespaces=True)] == [199]


class TestSurvey:
    def test_the_venue_table_yields_signalable_groups(self, tmp_path: Path) -> None:
        venue = _table(tmp_path / "proc")
        PlantedProcess(pid=200, pgid=199, state="R").write(venue)

        with pinned_process_table(venue=venue, host=tmp_path / "absent"):
            survey = survey_orphan_groups(min_age_seconds=_OLD_ENOUGH_SECONDS)

        assert [(group.pgid, group.signalable) for group in survey.groups] == [(199, True)]

    def test_a_host_table_yields_unsignalable_groups_and_drops_nested_ones(self, tmp_path: Path) -> None:
        venue = _table(tmp_path / "proc")
        PlantedProcess(pid=1, pgid=1).write(venue)
        host = _table(tmp_path / "host-proc")
        PlantedProcess(pid=900, pgid=899, state="R", nspids=(900,)).write(host)
        PlantedProcess(pid=901, pgid=898, state="R", nspids=(901, 7)).write(host)

        with pinned_process_table(venue=venue, host=host):
            survey = survey_orphan_groups(min_age_seconds=_OLD_ENOUGH_SECONDS)

        assert [(group.pgid, group.signalable) for group in survey.groups] == [(899, False)]

    def test_no_readable_table_reports_a_gap_rather_than_an_empty_pass(self, tmp_path: Path) -> None:
        absent = tmp_path / "absent"

        with pinned_process_table(venue=absent, host=absent):
            survey = survey_orphan_groups(min_age_seconds=_OLD_ENOUGH_SECONDS)

        assert survey.groups == ()
        assert survey.gaps != ()

    def test_report_names_the_numbers_an_operator_acts_on(self, tmp_path: Path) -> None:
        root = _table(tmp_path)
        PlantedProcess(pid=200, pgid=199, cpu_ticks=_BURNING_TICKS, comm="bash").write(root)

        rendered = _scan(root)[0].report()

        assert "199" in rendered
        assert "bash" in rendered

    def test_the_program_word_drops_the_directory_it_happened_to_run_from(self, tmp_path: Path) -> None:
        # The never-reap rules key on this, and a path-carrying command line is what
        # made them match every process on the box.
        root = _table(tmp_path)
        PlantedProcess(pid=200, pgid=199, state="R", cmdline="/opt/build/teatree/tools/sh -c :").write(root)

        assert _scan(root)[0].members[0].program == "sh"
