"""The per-lane result type and the plain-language closing verdict.

Split out of :mod:`teatree.cli.eval.all` so the verdict — the part a non-expert
actually reads to decide "is this good?" — has a cohesive home, and so the
orchestration module stays under the module-health function cap. ``LaneResult``
lives here too because the verdict is computed entirely from it.
"""

import dataclasses

import typer


@dataclasses.dataclass(frozen=True)
class LaneResult:
    """One eval lane's outcome in the unified ``t3 eval all`` summary.

    ``setup_hint`` distinguishes the two reasons a lane can be skipped. A skip
    with a hint is *unrunnable for setup reasons* (the AI behavioural lane with
    no in-session transcripts / no key) — the run did NOT validate it, so the
    final verdict flags it as not-yet-validated and ``--strict`` fails on it. A
    skip with no hint is a benign no-applicable-work skip (e.g. transcript-replay
    when no session transcript is in scope) that does not undermine the verdict.
    """

    name: str
    cost: str
    passed: bool
    skipped: bool
    detail: str
    setup_hint: str | None = None

    @property
    def needs_setup(self) -> bool:
        return self.skipped and self.setup_hint is not None

    @property
    def status(self) -> str:
        if self.needs_setup:
            return "SKIPPED — needs setup"
        if self.skipped:
            return "SKIP"
        return "PASS" if self.passed else "FAIL"


def build_verdict(lanes: list[LaneResult]) -> str:
    """Plain-language closing verdict a non-expert can read at a glance.

    Three honest shapes, keyed off the lane outcomes:

    - any real FAIL: ``❌ PROBLEMS FOUND`` naming the failing lane(s).
    - all deterministic checks green but a lane was skipped-for-setup (the AI
        behavioural lane with no transcripts / no key): ``✅`` for the
        deterministic part PLUS a ``⚠️`` that the skipped lane was NOT RUN and
        not-yet-validated, so the reader never reads the run as fully validated.
    - everything that ran passed and nothing needed setup: ``✅ ALL GOOD``.
    """
    failed = [lane for lane in lanes if not lane.passed and not lane.skipped]
    if failed:
        names = ", ".join(lane.name for lane in failed)
        plural = "checks" if len(failed) > 1 else "check"
        return f"❌ PROBLEMS FOUND — {len(failed)} {plural} failed ({names}), see the {names} row(s) above."
    not_validated = [lane for lane in lanes if lane.needs_setup]
    ran = len(lanes) - len(not_validated)
    if not_validated:
        names = ", ".join(lane.name for lane in not_validated)
        return (
            f"✅ Deterministic checks: ALL GOOD ({ran} lanes). "
            f"⚠️ {names}: NOT RUN — SKIPPED, needs setup: {not_validated[0].setup_hint} "
            "(not yet validated)."
        )
    return f"✅ ALL GOOD — every check passed ({len(lanes)} lanes)."


def print_verdict(lanes: list[LaneResult]) -> None:
    # typer.echo (not Console().print) so the verdict line is never terminal-width
    # wrapped — a non-expert must be able to read the closing line in one piece.
    typer.echo(build_verdict(lanes))
