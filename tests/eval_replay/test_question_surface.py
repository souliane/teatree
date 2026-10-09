"""The shipped catalog's question-surface labelling — the guard that replaces the SDK quarantine.

``claude-agent-sdk`` was frozen at an exact pin with Dependabot updates disabled
(souliane/teatree#3125) because a bundled ``claude`` CLI at/after 2.1.204 renders an
``AskUserQuestion`` call as a markdown chip rather than a ``tool_use`` block, reddening
every scenario that hard-requires that tool call. Freezing the dependency defended the
wrong asset: the contract teatree must guarantee is the headless Slack round trip, not
one CLI's tool-call rendering.

These tests are what let the quarantine go (souliane/teatree#3855): every scenario that
hard-requires the interactive tool call must be labelled ``surface: interactive`` and is
therefore advisory, so an SDK bump can never red a gating lane again.
"""
# test-path: cross-cutting — an eval-catalog test living under tests/eval_replay/ by
# the established eval-suite convention, spanning teatree.eval + the shipped scenarios.

from teatree.eval.discovery import discover_specs
from teatree.eval.models import HEADLESS_SURFACE
from teatree.eval.surface import is_advisory, mislabelled_interactive_specs

#: The headless question contract: a question REACHES the user (durably recorded, so the
#: Slack DM carries it) and the answer comes back and is applied. These grade the
#: contract rather than a rendering, so they must stay on the blocking surface.
_HEADLESS_QUESTION_CONTRACT = frozenset(
    {
        "headless_blocker_records_durable_question_not_prose",
        "headless_question_survives_denied_tool_surface",
        "applies_injected_askuserquestion_answer",
        "does_not_apply_stale_locally_answered_reply",
        "does_not_apply_superseded_generation_reply",
    }
)


def test_every_interactive_tool_call_scenario_is_labelled_interactive() -> None:
    offenders = mislabelled_interactive_specs(discover_specs())
    assert not offenders, (
        "a scenario that cannot pass without an AskUserQuestion tool call is pinned to a "
        "bundled claude CLI's rendering, not to the question contract. Label it "
        "`surface: interactive` so it is graded but advisory — otherwise an SDK bump reds "
        "a gating lane and the quarantine has to come back. Offenders: "
        + ", ".join(f"{s.name} ({s.source_path.name})" for s in offenders)
    )


def test_the_interactive_surface_is_actually_used() -> None:
    # Anti-vacuity: the guard above passes trivially if nothing is labelled. Claude Code
    # interactive is used heavily and is worth grading, so the label must be in real use.
    labelled = [spec for spec in discover_specs() if is_advisory(spec)]
    assert labelled, "no scenario carries `surface: interactive` — the interactive lane is not being graded"


def test_the_headless_question_contract_is_graded_and_blocking() -> None:
    by_name = {spec.name: spec for spec in discover_specs()}
    missing = sorted(_HEADLESS_QUESTION_CONTRACT - set(by_name))
    assert not missing, f"the headless question-contract scenarios are missing from the catalog: {missing}"
    advisory = sorted(name for name in _HEADLESS_QUESTION_CONTRACT if by_name[name].surface != HEADLESS_SURFACE)
    assert not advisory, f"the Slack round trip is the lane whose failure BLOCKS — these must stay headless: {advisory}"


def test_the_headless_contract_never_depends_on_the_interactive_tool() -> None:
    # The point of the agent lane is that it grades a contract no bundled-CLI rendering
    # can swallow, so none of its scenarios may even OFFER the interactive question tool.
    by_name = {spec.name: spec for spec in discover_specs()}
    offenders = sorted(
        name for name in _HEADLESS_QUESTION_CONTRACT if name in by_name and "AskUserQuestion" in by_name[name].tools
    )
    assert not offenders, f"a headless question-contract scenario must not offer AskUserQuestion: {offenders}"
