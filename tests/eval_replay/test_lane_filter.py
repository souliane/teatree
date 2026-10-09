"""``--lane`` filtering — slice the discovered catalog by ``EvalSpec.lane``."""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention, spanning teatree.cli.eval + teatree.eval.

from pathlib import Path

import pytest
import typer

from teatree.cli.eval.lane_filter import filter_specs_by_lane
from teatree.eval.models import EvalSpec


def _spec(name: str, lane: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="synthetic",
        agent_path="skills/rules/SKILL.md",
        prompt="do the thing",
        matchers=(),
        source_path=Path("synthetic.yaml"),
        lane=lane,
    )


_CATALOG = [
    _spec("clean_a", "clean_room"),
    _spec("under_a", "under_load"),
    _spec("clean_b", "clean_room"),
]


def test_none_lane_returns_every_spec_unchanged() -> None:
    # The default (no --lane) must not change any run: the whole catalog passes through.
    assert filter_specs_by_lane(_CATALOG, None) == _CATALOG


def test_clean_room_lane_selects_only_clean_room_specs() -> None:
    selected = filter_specs_by_lane(_CATALOG, "clean_room")
    assert [s.name for s in selected] == ["clean_a", "clean_b"]


def test_under_load_lane_selects_only_under_load_specs() -> None:
    selected = filter_specs_by_lane(_CATALOG, "under_load")
    assert [s.name for s in selected] == ["under_a"]


def test_unknown_lane_exits_two_rather_than_silently_empty() -> None:
    # An unknown lane must fail loud (CLI usage error), never return a silently-
    # green empty subset that reports zero failures while testing nothing.
    with pytest.raises(typer.Exit) as exc:
        filter_specs_by_lane(_CATALOG, "heavy_load")
    assert exc.value.exit_code == 2
