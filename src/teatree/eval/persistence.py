"""Persist behavioral-eval results into the run-history ledger.

The boundary between the Django-free eval harness (:mod:`teatree.eval.report`)
and the durable ledger (:class:`teatree.core.models.EvalRunRecord`). One
``t3 eval run`` invocation becomes one :class:`EvalRunRecord` plus one
:class:`EvalScenarioResult` per scenario per trial. The harness is single-trial
today, so ``trial`` is fixed at 0 here; the schema already carries it for the
later k>=3 phase.

This module owns only the orchestration (create the run row, fan out the
scenario rows in one transaction); the aggregation and diff logic lives on the
models. Persisting wraps in ``atomic()`` so a partially-written run never
pollutes the history.
"""

from collections.abc import Callable

from django.db import transaction

from teatree.core.models import EvalRunRecord, MatcherDetail, TrajectoryToolCall
from teatree.eval.models import AnyOf, ExpectItem, Matcher
from teatree.eval.report import MatcherResult, ScenarioResult


def _trajectory(result: ScenarioResult) -> list[TrajectoryToolCall]:
    return [TrajectoryToolCall(name=c.name, input=c.input, turn=c.turn) for c in result.run.tool_calls]


def _matcher_detail(item: MatcherResult) -> MatcherDetail:
    matcher: ExpectItem = item.matcher
    if isinstance(matcher, AnyOf):
        return _any_of_detail(matcher, passed=item.passed)
    return MatcherDetail(
        kind=matcher.kind,
        tool=matcher.tool,
        arg_path=matcher.arg_path,
        operator=matcher.operator,
        value=matcher.value,
        passed=item.passed,
    )


def _any_of_detail(matcher: AnyOf, *, passed: bool) -> MatcherDetail:
    def field(getter: Callable[[Matcher], str]) -> str:
        return " | ".join(getter(alt) for alt in matcher.alternatives)

    return MatcherDetail(
        kind="any_of",
        tool=field(lambda alt: alt.tool),
        arg_path=field(lambda alt: alt.arg_path),
        operator=field(lambda alt: alt.operator),
        value=field(lambda alt: alt.value),
        passed=passed,
    )


def _matcher_details(result: ScenarioResult) -> list[MatcherDetail]:
    return [_matcher_detail(m) for m in result.matcher_results]


def persist_run(  # noqa: PLR0913 — run-ledger boundary; each kwarg is a documented run attribute.
    results: list[ScenarioResult],
    *,
    model: str,
    suite: str = "",
    overlay: str = "",
    max_turns_override: int | None = None,
    trial: int = 0,
) -> EvalRunRecord:
    with transaction.atomic():
        run = EvalRunRecord.objects.record(
            model=model,
            suite=suite,
            overlay=overlay,
            max_turns_override=max_turns_override,
        )
        for result in results:
            run.record_scenario(
                scenario_name=result.spec.name,
                verdict=result.verdict,
                trial=trial,
                terminal_reason=result.run.terminal_reason,
                is_error=result.run.is_error,
                tool_calls=_trajectory(result),
                matcher_details=_matcher_details(result),
            )
    return run
