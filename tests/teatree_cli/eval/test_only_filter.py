"""``t3 eval run --only a,b`` restricts the run to exactly the named scenarios.

The selective-PR workflow passes ``--only "<comma names>"`` (the scenarios the PR
touched). The filter validates each name against the catalog — an unknown name
exits non-zero, never silently dropped — and intersects with any ``--lane`` /
``--shard`` subset already selected.
"""

from pathlib import Path

import pytest
import typer

from teatree.cli.eval.only_filter import filter_specs_by_only
from teatree.eval.models import EvalSpec


def _spec(name: str, *, lane: str = "clean_room") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="s",
        agent_path="skills/code/SKILL.md",
        prompt="p",
        matchers=(),
        source_path=Path("evals/scenarios/x.yaml"),
        lane=lane,
    )


_CATALOG = [_spec("a"), _spec("b"), _spec("c")]


@pytest.fixture(autouse=True)
def _stub_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.cli.eval.only_filter.discover_specs", lambda: _CATALOG)


class TestFilterSpecsByOnly:
    def test_none_returns_all_unchanged(self) -> None:
        specs = [_spec("a"), _spec("b")]
        assert filter_specs_by_only(specs, None) == specs

    def test_restricts_to_named_subset(self) -> None:
        specs = [_spec("a"), _spec("b"), _spec("c")]
        assert [s.name for s in filter_specs_by_only(specs, "a,c")] == ["a", "c"]

    def test_preserves_catalog_order_not_argument_order(self) -> None:
        specs = [_spec("a"), _spec("b"), _spec("c")]
        assert [s.name for s in filter_specs_by_only(specs, "c,a")] == ["a", "c"]

    def test_whitespace_around_names_tolerated(self) -> None:
        specs = [_spec("a"), _spec("b")]
        assert [s.name for s in filter_specs_by_only(specs, " a , b ")] == ["a", "b"]

    def test_unknown_name_exits_non_zero(self) -> None:
        specs = [_spec("a"), _spec("b")]
        with pytest.raises(typer.Exit) as exc:
            filter_specs_by_only(specs, "a,nope")
        assert exc.value.exit_code != 0

    def test_known_name_sliced_out_by_lane_shard_is_not_an_error(self) -> None:
        # "c" is in the catalog but was already sliced out of *specs* by
        # --lane/--shard; that is legitimately absent, NOT a usage error.
        specs = [_spec("a"), _spec("b")]
        assert filter_specs_by_only(specs, "c") == []

    def test_name_absent_from_catalog_fails_loud(self) -> None:
        specs = [_spec("a"), _spec("b")]
        with pytest.raises(typer.Exit):
            filter_specs_by_only(specs, "ghost")

    def test_empty_only_is_a_usage_error(self) -> None:
        specs = [_spec("a")]
        with pytest.raises(typer.Exit):
            filter_specs_by_only(specs, "  ,  ")
