"""Aggregate every declared scenario group into one ordered catalog."""

from scripts.eval.corpus_gen.catalog import RECURRING
from scripts.eval.corpus_gen.model import Scenario
from scripts.eval.corpus_gen.per_skill import PER_SKILL

ALL_SCENARIOS: list[Scenario] = list(RECURRING) + list(PER_SKILL)


def _assert_unique_names(scenarios: list[Scenario]) -> None:
    seen: set[str] = set()
    for scenario in scenarios:
        if scenario.name in seen:
            msg = f"duplicate scenario name: {scenario.name}"
            raise ValueError(msg)
        seen.add(scenario.name)


_assert_unique_names(ALL_SCENARIOS)
