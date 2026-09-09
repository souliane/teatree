"""The ``execution_reason`` stamps that park a FAILED task out of every requeue scan.

They live here, below both the loop that writes them and the recovery report that
reads them, so the two can never disagree about which FAILED rows are deliberately
parked — a scan that missed one would reopen a task a human was already paged about.
"""

#: Stamped when a task is escalated (dead-lettered), so it is excluded from every future
#: scan — bounds per-tick work and makes the escalation durably once-per-task regardless
#: of whether the question is later answered.
HALT_STAMP = "[repair-halt-parked]"

#: Stamped when a FAILED task is parked because a newer, still-active sibling Task holds
#: its ``(ticket, phase)`` (#3534). The row stays FAILED — the phase has not completed —
#: and drops out of the scan, so the stale predecessor neither escalates nor advances the
#: ticket's FSM.
LIVE_SUCCESSOR_STAMP = "[superseded-parked]"

#: Every stamp meaning "deliberately parked, do not reopen".
PARK_STAMPS: tuple[str, ...] = (HALT_STAMP, LIVE_SUCCESSOR_STAMP)
