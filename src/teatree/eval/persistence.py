"""Persist behavioral-eval results into the run-history ledger.

The boundary between the Django-free eval harness (:mod:`teatree.eval.report`)
and the durable ledger (:class:`teatree.core.models.EvalRunRecord`). One
``t3 eval run`` invocation becomes one :class:`EvalRunRecord` plus one
:class:`EvalScenarioResult` per scenario (per model, for a matrix run).

Three entry points, one transaction each:

*   :func:`persist_run` — a single-trial run (one row per scenario).
*   :func:`persist_pass_at_k` — a pass@k run (one aggregate row per scenario
    carrying ``trials`` and the pass-rate ``score``).
*   :func:`persist_matrix` — a model-matrix run (one row per ``(scenario,
    model)`` cell).

All three record an ungradeable result as the ``error`` verdict rather than a
behavioral ``fail``: ``EvalScenarioResultQuerySet.graded()`` keys on the verdict,
so an API 529 persisted as a fail scores 0.0, survives into the pass-rate math,
and the next ``--gate-regressions`` run reports a regression for a network blip.

This module owns only the orchestration (create the run row, fan out the
scenario rows in one transaction); the aggregation and diff logic lives on the
models. Persisting wraps in ``atomic()`` so a partially-written run never
pollutes the history.

The ``teatree.core.models`` imports are deferred into the functions that build
model instances: importing that package eagerly instantiates Django model
classes (e.g. ``AnthropicActivePick``), which requires configured settings.
This module is imported at CLI startup via ``current_git_sha`` (a Django-free
git helper), so it must import without ``DJANGO_SETTINGS_MODULE`` set. The model
classes appear only in ``TYPE_CHECKING`` (as quoted annotations) and in
function-local runtime imports, never at module import time.
"""

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, TypedDict

from django.db import transaction

from teatree.eval.api_errors import THROTTLE_TERMINAL_PREFIX
from teatree.eval.matrix import MatrixRow
from teatree.eval.models import AnyOf, ExpectItem, FinalStateMatcher, Matcher, TokenUsage
from teatree.eval.pass_at_k import PassAtKResult
from teatree.eval.report import MatcherResult, ScenarioResult
from teatree.utils import git
from teatree.utils.run import CommandFailedError

if TYPE_CHECKING:
    from teatree.core.models import EvalRunRecord, MatcherDetail, TrajectoryToolCall


class _TokenColumns(TypedDict):
    """The four ``record_scenario`` token kwargs, named so ``**`` maps key-by-key.

    A plain ``dict[str, int]`` makes the spread offer ``int`` to EVERY parameter of
    the callee, not just these four, so the checker rejects the unrelated ones.
    """

    input_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    output_tokens: int


def _token_columns(usage: TokenUsage) -> _TokenColumns:
    """Map a :class:`TokenUsage` onto the ``record_scenario`` token kwargs."""
    return {
        "input_tokens": usage.input,
        "cache_creation_tokens": usage.cache_creation,
        "cache_read_tokens": usage.cache_read,
        "output_tokens": usage.output,
    }


def current_git_sha() -> str:
    try:
        return git.head_sha()
    except (CommandFailedError, OSError):
        return ""


def _trajectory(result: ScenarioResult) -> list["TrajectoryToolCall"]:
    from teatree.core.models import TrajectoryToolCall  # noqa: PLC0415 — deferred: keep import Django-free

    return [TrajectoryToolCall(name=c.name, input=c.input, turn=c.turn) for c in result.run.tool_calls]


def _matcher_detail(item: MatcherResult) -> "MatcherDetail":
    from teatree.core.models import MatcherDetail  # noqa: PLC0415 — deferred: keep import Django-free

    matcher: ExpectItem = item.matcher
    if isinstance(matcher, AnyOf):
        return _any_of_detail(matcher, passed=item.passed)
    if isinstance(matcher, FinalStateMatcher):
        return _final_state_detail(matcher, passed=item.passed)
    return MatcherDetail(
        kind=matcher.kind,
        tool=matcher.tool,
        arg_path=matcher.arg_path,
        operator=matcher.operator,
        value=matcher.value,
        passed=item.passed,
    )


def _final_state_detail(matcher: FinalStateMatcher, *, passed: bool) -> "MatcherDetail":
    """Persist a final-state matcher: the subject is the final assistant message.

    ``MatcherDetail`` requires ``tool``/``arg_path`` strings; a final-state
    matcher has neither (its sole subject is the run's terminal message), so the
    ``<final_state>`` sentinel names the subject and ``arg_path`` is empty.
    """
    from teatree.core.models import MatcherDetail  # noqa: PLC0415 — deferred: keep import Django-free

    return MatcherDetail(
        kind="final_state",
        tool="<final_state>",
        arg_path="",
        operator=matcher.operator,
        value=matcher.value,
        passed=passed,
    )


def _any_of_detail(matcher: AnyOf, *, passed: bool) -> "MatcherDetail":
    from teatree.core.models import MatcherDetail  # noqa: PLC0415 — deferred: keep import Django-free

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


def _matcher_details(result: ScenarioResult) -> list["MatcherDetail"]:
    return [_matcher_detail(m) for m in result.matcher_results]


def _judge_rationale(result: ScenarioResult) -> str:
    if result.judge is None or result.judge.skipped:
        return ""
    return result.judge.rationale


# ast-grep-ignore: ac-django-no-complexity-suppressions
def persist_run(  # noqa: PLR0913 — run-ledger boundary; each kwarg is a documented run attribute.
    results: list[ScenarioResult],
    *,
    model: str,
    suite: str = "",
    overlay: str = "",
    max_turns_override: int | None = None,
    trial: int = 0,
    git_sha: str | None = None,
) -> "EvalRunRecord":
    from teatree.core.models import EvalRunRecord  # noqa: PLC0415 — deferred: keep import Django-free

    with transaction.atomic():
        run = EvalRunRecord.objects.record(
            model=model,
            suite=suite,
            overlay=overlay,
            max_turns_override=max_turns_override,
            git_sha=current_git_sha() if git_sha is None else git_sha,
        )
        for result in results:
            run.record_scenario(
                scenario_name=result.spec.name,
                verdict=_single_trial_verdict(result),
                trial=trial,
                model=result.spec.model,
                terminal_reason=result.run.terminal_reason,
                is_error=result.run.is_error,
                tool_calls=_trajectory(result),
                matcher_details=_matcher_details(result),
                judge_rationale=_judge_rationale(result),
                cost_usd=result.run.cost_usd,
                main_cost_usd=result.run.main_cost_usd,
                aux_cost_usd=result.run.aux_cost_usd,
                **_token_columns(result.run.usage),
            )
    return run


def persist_pass_at_k(
    results: Sequence[PassAtKResult],
    *,
    model: str,
    max_turns_override: int | None = None,
    git_sha: str | None = None,
) -> "EvalRunRecord":
    from teatree.core.models import EvalRunRecord  # noqa: PLC0415 — deferred: keep import Django-free

    with transaction.atomic():
        run = EvalRunRecord.objects.record(
            model=model,
            max_turns_override=max_turns_override,
            git_sha=current_git_sha() if git_sha is None else git_sha,
        )
        for result in results:
            errored = _pass_at_k_errored(result)
            run.record_scenario(
                scenario_name=result.spec_name,
                verdict=_pass_at_k_verdict(result),
                model=model,
                score=0.0 if (result.skipped or errored) else result.pass_rate,
                trials=result.trials,
                is_error=errored,
                terminal_reason=_pass_at_k_terminal_reason(result),
                cost_usd=result.cost_usd,
                main_cost_usd=result.main_cost_usd,
                aux_cost_usd=result.aux_cost_usd,
                **_token_columns(result.usage),
            )
    return run


def persist_matrix(
    rows: Sequence[MatrixRow],
    *,
    models: Sequence[str],
    max_turns_override: int | None = None,
    git_sha: str | None = None,
) -> "EvalRunRecord":
    from teatree.core.models import EvalRunRecord  # noqa: PLC0415 — deferred: keep import Django-free

    with transaction.atomic():
        run = EvalRunRecord.objects.record(
            model=",".join(models),
            max_turns_override=max_turns_override,
            git_sha=current_git_sha() if git_sha is None else git_sha,
        )
        for row in rows:
            run.record_scenario(
                scenario_name=row.scenario,
                verdict=_matrix_verdict(row),
                model=row.model,
                score=0.0 if (row.skipped or row.errored) else row.score,
                trials=row.trials,
                cost_usd=row.cost_usd,
                main_cost_usd=row.main_cost_usd,
                aux_cost_usd=row.aux_cost_usd,
                **_token_columns(row.usage),
            )
    return run


def _single_trial_verdict(result: ScenarioResult) -> str:
    # A trial the runner could not grade is `error`, not `fail`: `ScenarioResult.verdict`
    # collapses the two, and `graded()` keys on the verdict, so a transport blip
    # persisted as a behavioral fail scores 0.0 and reads as a regression next run.
    if result.run.is_error and not result.skipped:
        return "error"
    return result.verdict


def _executed_trials(result: PassAtKResult) -> list[ScenarioResult]:
    return [trial for trial in result.trial_results if not trial.skipped]


def _pass_at_k_errored(result: PassAtKResult) -> bool:
    """Whether EVERY executed trial errored — transport, not behaviour.

    One clean trial with a real matcher diff is behavioral signal, so a mixed
    aggregate is a genuine fail. Mirrors the same derivation the publish-safe
    ``--summary-json`` row makes for its triage class.
    """
    executed = _executed_trials(result)
    return bool(executed) and all(trial.run.is_error for trial in executed)


def _pass_at_k_terminal_reason(result: PassAtKResult) -> str:
    """The aggregate terminal reason: a cap outranks a throttle common to every trial."""
    if result.terminal_reason:
        return result.terminal_reason
    executed = _executed_trials(result)
    if executed and all(trial.run.terminal_reason.startswith(THROTTLE_TERMINAL_PREFIX) for trial in executed):
        return executed[0].run.terminal_reason
    return ""


def _pass_at_k_verdict(result: PassAtKResult) -> str:
    # An all-errored aggregate is recorded as `error`, exactly as the matrix path
    # records an errored cell: VISIBLE in the ledger and the baseline diff, yet
    # excluded from pass-rate math by `graded()`, so an API 529 across every trial
    # of the weekly lane can never read as a behavioral regression.
    if _pass_at_k_errored(result):
        return "error"
    if result.skipped:
        return "skip"
    return "pass" if result.ok else "fail"


def _matrix_verdict(row: MatrixRow) -> str:
    # An errored cell is recorded as its own `error` verdict — VISIBLE in the
    # ledger and the baseline diff (a chronically-errored scenario no longer
    # vanishes from history), yet EvalScenarioResultQuerySet.graded() excludes it
    # from pass-rate math so a transient blip never counts as a pass or a fail.
    if row.errored:
        return "error"
    if row.skipped:
        return "skip"
    return "pass" if row.passed else "fail"
