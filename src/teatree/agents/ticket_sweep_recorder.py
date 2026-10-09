"""Server-side verification of a backlog sweep's returned ``ticket_sweep`` (#162 Rule 4).

Rule 4 of #162 asks for a metric: "each sweep tends to zero changes", and a rising
count is a defect to investigate. That only works if the count is MEASURED. A
number the sweeping agent types into its own envelope is not — the agent that
swept a clean backlog and the agent that skipped the sweep both write ``0``, and
the trend the owner is supposed to read becomes unreadable at exactly the moment
it would have said something.

So the measurement is the :class:`~teatree.core.models.TicketSweepRun` row: the
sweep opens it, :func:`teatree.core.issue_hygiene.append_description_section`
records each changed URL against it as the writes land, and the sweep closes it.
The envelope's job is only to NAME that row. This module checks the name against
the row and refuses the four ways it can be wrong:

* no ``run_id`` at all — the count has no backing;
* a ``run_id`` nobody began — the run was invented;
* a run still open — the sweep crashed or never finished, so the row's count is
    not final and reporting it as one hides the crash;
* a ``changed_count`` disagreeing with the row — the envelope contradicts the
    facade's own record of what it wrote.

Absent is a NO-OP on every other phase, exactly like ``fix_record``: an overlay
whose agents never sweep keeps today's behaviour and earns no new refusal. On
``backlog_sweep`` itself absence is refused one layer up, by
:data:`~teatree.agents.result_schema.PHASE_REQUIRED_EVIDENCE`.
"""

from collections.abc import Mapping

from teatree.agents.envelope_refusal import MALFORMED_TICKET_SWEEP_PREFIX
from teatree.agents.result_schema import AgentResultBlob
from teatree.core.models import TicketSweepRun


def verify_returned_ticket_sweep(result: AgentResultBlob) -> str:
    """Check *result*'s ``ticket_sweep`` against its persisted run; return a refusal, or ``""``."""
    raw = result.get("ticket_sweep")
    if raw is None:
        return ""
    if not isinstance(raw, Mapping):
        return (
            f"{MALFORMED_TICKET_SWEEP_PREFIX}expected an object naming the sweep run, got "
            f"{type(raw).__name__}. Return {{'run_id': '<the id t3 ticket sweep-begin printed>'}}."
        )
    run_id = str(raw.get("run_id") or "").strip()
    if not run_id:
        return (
            f"{MALFORMED_TICKET_SWEEP_PREFIX}no run_id. Open the run with "
            "`t3 <overlay> ticket sweep-begin`, pass its id to every write the sweep makes, "
            "close it with `ticket sweep-finish`, and return that id — the count is read "
            "from the run, never from this envelope."
        )
    return _run_refusal(run_id, raw.get("changed_count"))


def _run_refusal(run_id: str, reported: object) -> str:
    """The refusal for a named run that does not back the envelope, or ``""``."""
    run = TicketSweepRun.objects.filter(run_id=run_id).first()
    if run is None:
        return (
            f"{MALFORMED_TICKET_SWEEP_PREFIX}no sweep run {run_id!r} exists. A run id that "
            "names nothing is a count with no backing — begin the run before sweeping, not after."
        )
    if run.finished_at is None:
        return (
            f"{MALFORMED_TICKET_SWEEP_PREFIX}sweep run {run_id} is still open. Close it with "
            "`t3 <overlay> ticket sweep-finish` — a zero-change sweep finishes too, because an "
            "unfinished run and a sweep that never ran are the same row."
        )
    if reported is not None and reported != run.changed_count:
        return (
            f"{MALFORMED_TICKET_SWEEP_PREFIX}sweep run {run_id} changed {run.changed_count} ticket(s), "
            f"but the envelope reports {reported}. The run's recorded URLs are the count; "
            "omit changed_count rather than restating it."
        )
    return ""


__all__ = ["verify_returned_ticket_sweep"]
