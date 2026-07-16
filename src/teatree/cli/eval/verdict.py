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
    """One eval lane's outcome in the unified ``t3 eval`` summary.

    ``setup_hint`` distinguishes a lane that was NOT fully validated for setup
    reasons from one that ran clean. A hint means the lane could not validate
    everything it should have — the AI behavioural lane with no in-session
    transcripts / no key (a full skip), OR a lane that graded SOME scenarios but
    left others un-graded (partial coverage). Either way ``--strict`` fails on it
    and the verdict flags it not-yet-validated, so partial coverage can never read
    a clean green. A skip with no hint is a benign no-applicable-work skip (e.g.
    transcript-replay when no session transcript is in scope).

    ``duration_s`` is the lane's wall-clock, stamped by ``all._timed`` for the
    whole-suite HTML report (the terminal table does not show it).
    """

    name: str
    cost: str
    passed: bool
    skipped: bool
    detail: str
    setup_hint: str | None = None
    duration_s: float = 0.0

    @property
    def needs_setup(self) -> bool:
        """A setup hint means "not fully validated" — whether fully or partially skipped.

        Decoupled from ``skipped`` so a PARTIALLY-graded lane (some scenarios
        graded, others skipped for want of a transcript/grader) also surfaces as
        needs-setup and fails ``--strict``. A fully-skipped setup lane still counts
        (it is ``skipped`` with a hint); a benign no-work skip carries no hint.
        """
        return self.setup_hint is not None

    @property
    def status(self) -> str:
        if self.skipped:
            return "SKIPPED — needs setup" if self.setup_hint is not None else "SKIP"
        if not self.passed:
            return "FAIL"
        return "PASS — partial (needs setup)" if self.setup_hint is not None else "PASS"


def build_verdict(lanes: list[LaneResult]) -> str:
    """Plain-language closing verdict a non-expert can read at a glance.

    Three honest shapes, keyed off the lane outcomes:

    - any real FAIL: ``❌ PROBLEMS FOUND`` naming the failing lane(s).
    - all deterministic checks green but a lane was not fully validated for setup
        reasons (the AI behavioural lane with no transcripts / no key, or one that
        graded only SOME scenarios): ``✅`` for the deterministic part PLUS a ``⚠️``
        that the lane was not-yet-validated, so the reader never reads the run as
        fully validated.
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
        # A fully-skipped setup lane was NOT RUN; a partially-graded one DID grade
        # some scenarios but not all, so it is NOT FULLY VALIDATED rather than "not run".
        phrase = "NOT RUN — SKIPPED" if all(lane.skipped for lane in not_validated) else "NOT FULLY VALIDATED"
        return (
            f"✅ Deterministic checks: ALL GOOD ({ran} lanes). "
            f"⚠️ {names}: {phrase}, needs setup: {not_validated[0].setup_hint} "
            "(not yet validated)."
        )
    return f"✅ ALL GOOD — every check passed ({len(lanes)} lanes)."


def print_verdict(lanes: list[LaneResult]) -> None:
    # typer.echo (not Console().print) so the verdict line is never terminal-width
    # wrapped — a non-expert must be able to read the closing line in one piece.
    typer.echo(build_verdict(lanes))
