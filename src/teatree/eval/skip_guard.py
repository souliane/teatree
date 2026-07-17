"""Guards against a decorative eval run that reports green with no real coverage.

Two distinct failure shapes, two guards:

*   *All-skipped*: specs collected, zero executed. A scenario skips (not fails)
    when its run never happened — most often because ``claude`` is not on PATH.
    Every skipped scenario reports as passed, so a suite that collects specs but
    executes none exits green with zero behavioral coverage. The fresh-run (api)
    path forces this guard on; the LOCAL transcript backend legitimately
    all-skips before any transcript exists, so for it the guard is opt-in.

*   *Unmetered api*: the api backend executed scenarios but recorded $0 of model
    cost. That is the exact ``$0.00 (no metered calls)`` state the ``--bare``
    OAuth-auth bug produced — the model "ran" but authenticated as nothing,
    made zero tool calls, and recorded nothing. A fresh run that records nothing
    never actually executed and must FAIL LOUD, never pass. This guard is
    unconditional for the api backend (it is the fresh-run path's reason to exist).
"""


class AllSkippedError(RuntimeError):
    """Raised when a required run collected specs but executed none."""


class UnmeteredApiRunError(RuntimeError):
    """Raised when the api backend ran scenarios but metered $0 — it never executed."""


class EmptyFreshRunError(RuntimeError):
    """Raised when a fresh-run backend executed scenarios but produced no trajectory."""


class UnmeteredJudgeError(RuntimeError):
    """Raised when ``--judge`` was asked for and judge-oracle scenarios ran, but every judge call skipped."""


def assert_executed_when_required(*, collected: int, executed: int, required: bool) -> None:
    """Fail when ``required`` and the suite collected specs but ran none.

    ``executed`` is the count of scenarios that actually produced a graded
    verdict (a non-skipped result). ``collected`` is the number of discovered
    specs. A zero-spec suite is not a silent skip — there is nothing to run —
    so it never trips the guard.
    """
    if not required or collected == 0 or executed > 0:
        return
    msg = (
        f"eval suite collected {collected} scenario(s) but executed 0 — every scenario "
        "skipped. The suite produced zero behavioral coverage yet would report green. "
        "Most likely `claude` is not on PATH (no ANTHROPIC_API_KEY / CLI provisioned "
        "where the eval job runs). Provision the runner."
    )
    raise AllSkippedError(msg)


def assert_api_run_was_metered(*, backend: str, executed: int, total_cost_usd: float) -> None:
    """Fail when the api backend executed scenarios but metered $0 of API cost.

    Only the ``api`` backend is checked — the transcript backend runs no model
    by design. ``executed == 0`` is the all-skipped guard's job, not this one;
    this fires only when scenarios ran (``executed > 0``) yet recorded nothing,
    which means the model never actually authenticated/executed.
    """
    if backend != "api" or executed == 0 or total_cost_usd > 0.0:
        return
    msg = (
        f"api eval run executed {executed} scenario(s) but metered $0.00 (no metered "
        "calls). A metered run that bills nothing never actually executed — the SDK made "
        "zero billable tool calls. On the DEFAULT subscription-OAuth eval lane "
        "(T3_EVAL_CREDENTIAL=subscription_oauth, #2707 reversal) the usual cause is the "
        "OAuth usage window (5h/7d) being drained so every call was throttled — NOT an "
        "API-key problem. It can also be a credential that never reached the CLI (a "
        "logged-out / key-absent case, which is the only cause on a metered_api_key run). "
        "Check the OAuth usage window first, then the credential. This fails loud rather "
        "than reporting a vacuous green."
    )
    raise UnmeteredApiRunError(msg)


def assert_pydantic_ai_run_produced_output(*, backend: str, executed: int, produced: int) -> None:
    """Fail when the ``pydantic_ai`` backend executed scenarios but every run was empty.

    The ``$0``-metered guard (:func:`assert_api_run_was_metered`) is Claude-specific:
    it keys on ``cost_usd``, which the OrcaRouter BYOK ``pydantic_ai`` lane does not
    meter, so it can never guard a ``pydantic_ai`` fresh run. The backend-appropriate
    vacuous-green signal there is an EMPTY trajectory — a run that captured no tool
    calls AND no text never actually drove the model (the model-evolution lane could
    otherwise report a decorative green). ``produced`` is the count of executed
    (non-skipped) runs with a non-empty trajectory; the guard fires only for the
    ``pydantic_ai`` backend when scenarios ran yet not one produced output.
    """
    if backend != "pydantic_ai" or executed == 0 or produced > 0:
        return
    msg = (
        f"pydantic_ai eval run executed {executed} scenario(s) but every run captured an EMPTY "
        "trajectory (no tool calls, no text). A fresh run that produces nothing never actually "
        "drove the model — the OrcaRouter credential/model likely never authenticated. This fails "
        "loud rather than reporting a vacuous green; check the OrcaRouter BYOK credential and model."
    )
    raise EmptyFreshRunError(msg)


def assert_judge_was_metered(*, judge_requested: bool, judge_eligible: int, judge_calls: int) -> None:
    """Fail when ``--judge`` ran judge-oracle scenarios but every judge call skipped.

    Judge spend flows through a separate ``claude_agent_sdk.query`` that is never
    folded into ``run.cost_usd``, so :func:`assert_api_run_was_metered` cannot see
    it: a ``--judge`` run whose judge-oracle scenarios all skipped (most often
    ``claude`` absent) would report green having graded nothing with the judge.

    ``judge_eligible`` is the number of executed (non-skipped) scenarios that
    carry a judge oracle; ``judge_calls`` is how many of those the judge actually
    graded (a non-skipped :class:`~teatree.eval.report.JudgeOutcome`). The guard
    fires only when the judge was requested, there was at least one oracle to
    grade, and not one was graded — never when ``--judge`` is off or no scenario
    carries a judge block (zero calls is correct there).
    """
    if not judge_requested or judge_eligible == 0 or judge_calls > 0:
        return
    msg = (
        f"--judge requested and {judge_eligible} judge-oracle scenario(s) ran, but the judge "
        "graded 0 of them — every judge call skipped (most likely `claude` is not on PATH where "
        "the judge runs). A judge oracle that never grades reports a vacuous green; this fails "
        "loud instead. Provision `claude` / ANTHROPIC_API_KEY, or drop --judge."
    )
    raise UnmeteredJudgeError(msg)
