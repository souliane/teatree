"""Anti-vacuity proofs for the second-round recurring-feedback scenarios.

Two scenarios pin this session's two new post-mortems:

*   ``traverse_linked_specs_before_building`` (``skills/ticket``) — before building,
    the agent traverses every artifact LINKED from the ticket (a Notion/Confluence
    spec is the source of truth) and surfaces the full deliverable map for
    confirmation; it does NOT scope from the ticket title and jump into building one
    layer. (Source: a ticket whose display banner was built while the config-portal
    authoring UI the linked spec defined was missed.)
*   ``followup_loop_scan_only_never_auto_implement`` (``skills/checking``) — the cron
    follow-up tick is scan-only (``agent_runtime=interactive``); it never
    auto-dispatches a headless coder to implement a ticket, and never dispatches for a
    CLOSED ticket. (Source: a ``*/12`` tick that spent ~13 min implementing an
    already-CLOSED ticket.)

Each proof drives the ``_fail`` fixture RED, the ``_pass`` fixture GREEN, the
``_noop`` fixture RED, and the mandatory teeth check: dropping the discriminating
NEGATIVE matcher flips the ``_fail`` fixture GREEN — proving that matcher (not the
positive anchor, which the ``_fail`` fixture deliberately also satisfies) is the
tooth that catches the drift.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

import pytest

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.report import evaluate

_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_SCENARIOS = (
    "traverse_linked_specs_before_building",
    "followup_loop_scan_only_never_auto_implement",
)


def _spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    assert spec is not None, f"scenario {name!r} not discovered — check evals/scenarios/*.yaml"
    return spec


def _grade(spec: EvalSpec, variant: str, tmp_path: Path) -> bool:
    fixture = _FIXTURES / f"{spec.name}_{variant}.stream.jsonl"
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _without_negative_matchers(spec: EvalSpec) -> EvalSpec:
    """``spec`` with its NEGATIVE matcher(s) removed — the discriminating tooth.

    The positive anchor stays; the ``_fail`` fixture is built to satisfy it (so the
    fixture fails ONLY because of the negative matcher). Dropping the negative must
    therefore flip ``_fail`` GREEN.
    """
    kept = tuple(m for m in spec.matchers if not (isinstance(m, Matcher) and m.kind == "negative"))
    assert len(kept) < len(spec.matchers), f"expected a negative matcher to drop; matchers={spec.matchers!r}"
    return dataclasses.replace(spec, matchers=kept)


@pytest.mark.parametrize("name", _SCENARIOS)
def test_fail_fixture_drives_scenario_red(name: str, tmp_path: Path) -> None:
    assert _grade(_spec(name), "fail", tmp_path) is False, (
        f"the {name} _fail fixture must grade RED — the discriminating matcher is toothless"
    )


@pytest.mark.parametrize("name", _SCENARIOS)
def test_pass_fixture_drives_scenario_green(name: str, tmp_path: Path) -> None:
    assert _grade(_spec(name), "pass", tmp_path) is True, (
        f"the {name} _pass fixture must grade GREEN — the matchers over-fit or the fixture violates the rule"
    )


@pytest.mark.parametrize("name", _SCENARIOS)
def test_noop_fixture_drives_scenario_red(name: str, tmp_path: Path) -> None:
    assert _grade(_spec(name), "noop", tmp_path) is False, (
        f"the {name} _noop fixture must grade RED — a do-nothing turn must not satisfy the scenario"
    )


@pytest.mark.parametrize("name", _SCENARIOS)
def test_dropping_negative_matcher_turns_fail_green(name: str, tmp_path: Path) -> None:
    """The mandatory teeth check — the negative matcher is the discriminator."""
    toothless = _without_negative_matchers(_spec(name))
    assert _grade(toothless, "fail", tmp_path) is True, (
        f"dropping the negative matcher must flip the {name} _fail fixture GREEN — "
        "that proves the negative matcher (not the positive anchor) catches the drift"
    )
