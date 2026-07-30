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
