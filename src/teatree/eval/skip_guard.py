"""Guards against a decorative eval run that reports green with no real coverage.

Two distinct failure shapes, two guards:

*   *All-skipped*: specs collected, zero executed. A scenario skips (not fails)
    when its run never happened — most often because ``claude`` is not on PATH.
    A skipped scenario cannot pass; the guard also prevents the lane from
    reporting success when no scenario was executed. The fresh-run (api)
    path forces this guard on; the LOCAL transcript backend legitimately
    all-skips before any transcript exists, so for it the guard is opt-in.

*   *Unmetered api*: the ``api`` backend executed scenarios but recorded $0 of
    model cost. That is the exact ``$0.00 (no metered calls)`` state the ``--bare``
    OAuth-auth bug produced — the model "ran" but authenticated as nothing,
    made zero tool calls, and recorded nothing. A fresh run that records nothing
    never actually executed and must FAIL LOUD, never pass.

*   *Empty fresh run*: the vacuous-green signal for the fresh-run backends whose
    TRANSPORT reports no cost. ``anthropic_api`` and ``pydantic_ai`` both drive the
    model through ``PydanticAiRunner``; their ``cost_usd`` is derived from token usage
    rather than billed, so it is positive for any run that made a call and cannot
    distinguish a healthy run from a vacuous one. An EMPTY trajectory (no tool calls,
    no text) is their equivalent "never actually executed" evidence.

*   *Hooks not registered*: a ``production_hooks`` scenario ran with the shipped
    plugin unregistered, so it graded the raw model rather than the model+hook
    system it exists to measure. Unlike the others above it is detected per scenario,
    inside the runner, and carried out on the run's ``terminal_reason``
    (:mod:`teatree.eval.harness_failure`); the guard is what turns that reason into
    a lane exit. It is deliberately a GUARD rather than a verdict: a verdict can be
    exempted by the advisory surface, and 6 of the 7 hooked scenarios are advisory
    (souliane/teatree#3922).

Which guard owns which backend is the load-bearing detail, and it is NOT the
fresh-run split: ``api`` is guarded by cost because it is the only backend whose
TRANSPORT reports a bill, while the other two fresh lanes are guarded by trajectory
because a usage-derived figure is positive whether or not the run did anything useful.
"""

from collections.abc import Sequence

from teatree.eval.backends import API_BACKEND, UNMETERED_FRESH_BACKENDS

#: Exit code for a lane that MEASURED NOTHING — it collected scenarios and graded none.
#: A third status beside 0 (ran, passed) and 1 (ran, failed), because "the suite declined
#: to run" and "the suite ran and passed" are OPPOSITE facts that exiting 0 made
#: indistinguishable. 75 is already the eval lanes' blocked/nothing-billed code, and the
#: one a CI job scopes ``allow_failure: exit_codes:`` to — so a declined lane renders
#: orange rather than green, and a genuinely red suite still reddens on 1.
MEASURED_NOTHING_EXIT_CODE = 75


def graded_nothing(*, collected: int, executed: int) -> bool:
    """True when a run collected scenarios and graded none of them.

    A suite that collected ZERO is NOT this state: there was nothing to run, so
    nothing declined to run. Only a suite with work in front of it that graded none
    of it has proved nothing while looking like it proved everything.
    """
    return collected > 0 and executed == 0


class AllSkippedError(RuntimeError):
    """Raised when a required run collected specs but executed none."""


class UnmeteredApiRunError(RuntimeError):
    """Raised when the api backend ran scenarios but metered $0 — it never executed."""


class EmptyFreshRunError(RuntimeError):
    """Raised when a fresh-run backend executed scenarios but produced no trajectory."""


class HooksNotRegisteredError(RuntimeError):
    """Raised when a ``production_hooks`` scenario ran with the shipped plugin unregistered."""


class UnmeteredJudgeError(RuntimeError):
    """Raised when ``--judge`` was asked for and judge-oracle scenarios ran, but every judge call skipped."""


def assert_executed_when_required(*, collected: int, executed: int, required: bool) -> None:
    """Fail when ``required`` and the suite collected specs but ran none.

    ``executed`` is the count of scenarios that actually produced a graded
    verdict (a non-skipped result). ``collected`` is the number of discovered
    specs. A zero-spec suite is not a silent skip — there is nothing to run —
    so it never trips the guard.
    """
    if not required or not graded_nothing(collected=collected, executed=executed):
        return
    msg = (
        f"eval suite collected {collected} scenario(s) but executed 0 — every scenario "
        "skipped. The suite produced zero behavioral coverage yet would report green. "
        "Most likely `claude` is not on PATH (no ANTHROPIC_API_KEY / CLI provisioned "
        "where the eval job runs). Provision the runner."
    )
    raise AllSkippedError(msg)


def assert_api_run_was_metered(*, backend: str, executed: int, total_cost_usd: float) -> None:
    """Fail when the ``api`` backend executed scenarios but metered $0 of API cost.

    ``api`` ONLY, and deliberately so — this keys on a TRANSPORT-REPORTED bill, which
    no other backend produces. ``transcript`` runs no model by design, and the other two
    fresh-run lanes (:data:`~teatree.eval.backends.UNMETERED_FRESH_BACKENDS`) carry a
    figure derived from token usage, which is positive for any run that made a call and
    so cannot separate a healthy run from a vacuous one. Their vacuous-green signal is an
    empty trajectory, which :func:`assert_fresh_run_produced_output` owns.

    ``executed == 0`` is the all-skipped guard's job, not this one; this fires only
    when scenarios ran (``executed > 0``) yet recorded nothing, which means the model
    never actually authenticated/executed.
    """
    if backend != API_BACKEND or executed == 0 or total_cost_usd > 0.0:
        return
    msg = (
        f"api eval run executed {executed} scenario(s) but metered $0.00 (no metered "
        "calls). A metered run that bills nothing never actually executed — the SDK made "
        "zero billable tool calls. On the DEFAULT subscription-OAuth eval lane the usual "
        "cause is the OAuth usage window (5h/7d) being drained so every call was "
        "throttled — NOT an API-key problem. It can also be a credential that never "
        "reached the CLI (a logged-out / key-absent case, which is the only cause on a "
        "metered api_key run). "
        "Check the OAuth usage window first, then the credential. This fails loud rather "
        "than reporting a vacuous green."
    )
    raise UnmeteredApiRunError(msg)


def assert_fresh_run_produced_output(*, backend: str, executed: int, produced: int) -> None:
    """Fail when an UNMETERED fresh backend executed scenarios but every run was empty.

    The ``$0``-metered guard (:func:`assert_api_run_was_metered`) keys on a
    transport-reported bill, which only the CLI-backed ``api`` lane produces. Every
    backend in :data:`~teatree.eval.backends.UNMETERED_FRESH_BACKENDS` —
    ``anthropic_api`` and ``pydantic_ai`` — derives its cost from token usage instead, a
    figure a vacuous run and a healthy one do not reliably differ on. The
    backend-appropriate vacuous-green signal is an EMPTY trajectory: a run that captured
    no tool calls AND no text never actually drove the model.

    ``anthropic_api`` is the backend CI runs, so leaving it out of this guard left the
    CI eval lane with NO vacuous-green guard at all — the cost guard cannot see it and
    this one skipped it.

    ``produced`` is the count of executed (non-skipped) runs with a non-empty
    trajectory; the guard fires only when scenarios ran yet not one produced output.
    """
    if backend not in UNMETERED_FRESH_BACKENDS or executed == 0 or produced > 0:
        return
    msg = (
        f"{backend} eval run executed {executed} scenario(s) but every run captured an EMPTY "
        "trajectory (no tool calls, no text). A fresh run that produces nothing never actually "
        "drove the model — the backend credential/model likely never authenticated. This backend "
        "reports no metered bill of its own, so the $0 guard cannot see it and this is the only "
        "vacuous-green check standing between the lane and a decorative green. Check the backend "
        "credential and model."
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


def assert_production_hooks_registered(*, unmeasured: Sequence[str]) -> None:
    """Fail when any ``production_hooks`` scenario captured zero hook lifecycle events.

    *unmeasured* is the name of every scenario whose run carried a harness-failure
    terminal reason (:func:`teatree.eval.harness_failure.measured_nothing`). The guard
    takes NO surface argument on purpose: a run that measured nothing has no verdict for
    the advisory exemption to weigh, so the exemption must not be reachable from here.
    """
    if not unmeasured:
        return
    names = ", ".join(sorted(unmeasured))
    msg = (
        f"{len(unmeasured)} production_hooks scenario(s) captured ZERO hook events: {names}. "
        "The shipped plugin never registered, so the lane measured the RAW MODEL rather than "
        "the model+hook system it exists to measure — the run produced no evidence about these "
        "scenarios at all. This is a HARNESS failure, not a scenario verdict, so no surface "
        "exemption applies. Check the repo-root plugin manifest (.claude-plugin/plugin.json + "
        "hooks/hooks.json) reachable from the eval sandbox, then re-run."
    )
    raise HooksNotRegisteredError(msg)
