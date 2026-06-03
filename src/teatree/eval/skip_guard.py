"""Guard against a decorative eval run: specs collected, zero executed.

A scenario skips (not fails) when its run never happened — most often because
``claude`` is not on PATH (no ``ANTHROPIC_API_KEY`` / CLI provisioned where the
suite runs). Every skipped scenario reports as passed, so a suite that collects
specs but executes none exits green with zero behavioral coverage.

This guard makes that state loud where it must never pass silently — the metered
CI eval job. It is opt-in (``required``): the LOCAL subscription backend
legitimately all-skips before any transcript exists, and that must stay green.
"""


class AllSkippedError(RuntimeError):
    """Raised when a required run collected specs but executed none."""


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
        "where the eval job runs). Provision the runner or drop --require-executed."
    )
    raise AllSkippedError(msg)
