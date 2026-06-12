"""``act_phase`` — turn collected signals into actions and run them.

Dispatch the tick's signals into actions, run the inline mechanical
handlers (ticket completions, orphan reaps) so the about-to-render
statusline reflects post-transition state, and persist ``kind="agent"``
actions into the Ticket + Task dispatch queue. Mechanical and persist
errors land on ``report.errors`` — they never abort the tick.
"""

from typing import TYPE_CHECKING

from teatree.loop.dispatch import dispatch
from teatree.loop.tick_recovery import _execute_mechanical, _persist_agent_dispatches

if TYPE_CHECKING:
    from teatree.loop.tick import TickReport


def act_phase(report: "TickReport") -> None:
    report.actions = dispatch(report.signals, errors=report.errors)
    _execute_mechanical(report)
    _persist_agent_dispatches(report)
