"""``--surface`` filtering — slice the discovered catalog by ``EvalSpec.surface``."""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention, spanning teatree.cli.eval + teatree.eval.

from pathlib import Path

import pytest
import typer

from teatree.cli.eval.surface_filter import filter_specs_by_surface
from teatree.eval.models import EvalSpec


def _spec(name: str, surface: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="synthetic",
        agent_path="skills/rules/SKILL.md",
        prompt="do the thing",
        matchers=(),
        source_path=Path("synthetic.yaml"),
        surface=surface,
    )


_CATALOG = [
    _spec("slack_a", "headless"),
    _spec("chip_a", "interactive"),
    _spec("slack_b", "headless"),
]


def test_none_surface_returns_every_spec_unchanged() -> None:
    assert filter_specs_by_surface(_CATALOG, None) == _CATALOG


def test_headless_surface_selects_only_the_blocking_scenarios() -> None:
    assert [s.name for s in filter_specs_by_surface(_CATALOG, "headless")] == ["slack_a", "slack_b"]


def test_interactive_surface_selects_only_the_advisory_scenarios() -> None:
    assert [s.name for s in filter_specs_by_surface(_CATALOG, "interactive")] == ["chip_a"]


def test_unknown_surface_exits_two_rather_than_silently_empty() -> None:
    with pytest.raises(typer.Exit) as exc:
        filter_specs_by_surface(_CATALOG, "slack")
    assert exc.value.exit_code == 2
