"""Guards against a decorative eval run that reports green with no real coverage.

Two distinct failure shapes, two guards:

*   *All-skipped*: specs collected, zero executed. A scenario skips (not fails)
    when its run never happened — most often because ``claude`` is not on PATH.
    Every skipped scenario reports as passed, so a suite that collects specs but
    executes none exits green with zero behavioral coverage. The metered (sdk)
    path forces this guard on; the LOCAL subscription backend legitimately
    all-skips before any transcript exists, so for it the guard is opt-in.

*   *Unmetered sdk*: the sdk backend executed scenarios but metered $0 of API
    cost. That is the exact ``$0.00 (no metered calls)`` state the ``--bare``
    OAuth-auth bug produced — ``claude -p`` "ran" but authenticated as nothing,
    made zero tool calls, and billed nothing. A metered run that meters nothing
    never actually executed and must FAIL LOUD, never pass. This guard is
    unconditional for the sdk backend (it is the metered path's reason to exist).
"""


class AllSkippedError(RuntimeError):
    """Raised when a required run collected specs but executed none."""


class UnmeteredSdkRunError(RuntimeError):
    """Raised when the sdk backend ran scenarios but metered $0 — it never executed."""


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
        "Most likely `claude` is not on PATH (no CLAUDE_CODE_OAUTH_TOKEN / CLI provisioned "
        "where the eval job runs). Provision the runner."
    )
    raise AllSkippedError(msg)


def assert_sdk_run_was_metered(*, backend: str, executed: int, total_cost_usd: float) -> None:
    """Fail when the sdk backend executed scenarios but metered $0 of API cost.

    Only the ``sdk`` backend is checked — the subscription backend is unmetered
    by design. ``executed == 0`` is the all-skipped guard's job, not this one;
    this fires only when scenarios ran (``executed > 0``) yet billed nothing,
    which means ``claude -p`` never actually authenticated/executed.
    """
    if backend != "sdk" or executed == 0 or total_cost_usd > 0.0:
        return
    msg = (
        f"sdk eval run executed {executed} scenario(s) but metered $0.00 (no metered "
        "calls). A metered run that bills nothing never actually executed — the SDK made "
        "zero billable tool calls. The two common causes: an auth failure "
        "(CLAUDE_CODE_OAUTH_TOKEN not reaching the CLI), or a subscription usage/weekly "
        "limit so every scenario short-circuited before doing real work. This fails loud "
        "rather than reporting a vacuous green."
    )
    raise UnmeteredSdkRunError(msg)
