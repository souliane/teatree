"""``select_specs`` — the one chokepoint composing name / lane / surface / shard."""

from pathlib import Path
from unittest import mock

import pytest
import typer

from teatree.cli.eval.catalog_selection import select_specs
from teatree.eval.models import EvalSpec


def _spec(name: str, *, lane: str = "clean_room", surface: str = "headless") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="synthetic",
        agent_path="skills/rules/SKILL.md",
        prompt="do the thing",
        matchers=(),
        source_path=Path("synthetic.yaml"),
        lane=lane,
        surface=surface,
    )


_CATALOG = [
    _spec("clean_headless"),
    _spec("clean_interactive", surface="interactive"),
    _spec("loaded_headless", lane="under_load"),
]


def test_no_filters_returns_the_whole_catalog() -> None:
    assert select_specs(_CATALOG, None, lane=None, surface=None, shard=None) == _CATALOG


def test_lane_and_surface_compose() -> None:
    selected = select_specs(_CATALOG, None, lane="clean_room", surface="headless", shard=None)
    assert [s.name for s in selected] == ["clean_headless"]


def test_a_named_scenario_bypasses_every_catalog_filter() -> None:
    named = _spec("loaded_headless", lane="under_load")
    with mock.patch("teatree.cli.eval.catalog_selection.require_spec", return_value=named) as require:
        selected = select_specs(_CATALOG, "loaded_headless", lane="clean_room", surface="headless", shard=None)
    require.assert_called_once_with("loaded_headless")
    assert selected == [named]


def test_a_malformed_shard_exits_two_rather_than_grading_an_empty_subset() -> None:
    with pytest.raises(typer.Exit) as exc:
        select_specs(_CATALOG, None, lane=None, surface=None, shard="not-a-shard")
    assert exc.value.exit_code == 2


def test_a_well_formed_shard_partitions_the_catalog() -> None:
    shards = [select_specs(_CATALOG, None, lane=None, surface=None, shard=f"{i}/3") for i in (1, 2, 3)]
    assert sorted(spec.name for shard in shards for spec in shard) == sorted(s.name for s in _CATALOG)


def test_a_rotating_shard_reports_which_shard_it_resolved_and_why(capsys: pytest.CaptureFixture[str]) -> None:
    select_specs(_CATALOG, None, lane=None, surface=None, shard="rotate/3")
    reported = capsys.readouterr().out
    assert "rotate/3" in reported
    assert "/3 (UTC " in reported


def test_a_rotating_shard_selects_exactly_the_subset_it_reported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    selected = select_specs(_CATALOG, None, lane=None, surface=None, shard="rotate/3")
    resolved = capsys.readouterr().out.split("->")[1].split("(")[0].strip()
    assert [s.name for s in selected] == [
        s.name for s in select_specs(_CATALOG, None, lane=None, surface=None, shard=resolved)
    ]


def test_a_fixed_shard_reports_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    select_specs(_CATALOG, None, lane=None, surface=None, shard="1/3")
    assert capsys.readouterr().out == ""


def test_a_malformed_rotating_total_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit) as exc:
        select_specs(_CATALOG, None, lane=None, surface=None, shard="rotate/0")
    assert exc.value.exit_code == 2
    assert "rotate" in capsys.readouterr().err
