"""Loop scanners — pure-Python signal collectors for the fat loop tick.

Each scanner exposes a :class:`Scanner` (Protocol) implementation that returns
a list of :class:`ScanSignal` records. The tick orchestrator (``loop.tick``)
runs all scanners in parallel, then dispatches each signal to either an
inline mechanical action or a phase agent (BLUEPRINT § 5.6).

Scanners are the only loop layer that touches external systems. They never
invoke Claude — that is the dispatcher's job.
"""

from teatree.loop.scanners.assigned_issues import AssignedIssuesScanner
from teatree.loop.scanners.base import Scanner, ScanSignal
from teatree.loop.scanners.my_prs import MyPrsScanner
from teatree.loop.scanners.notion_view import NotionViewScanner
from teatree.loop.scanners.pending_tasks import PendingTasksScanner
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner
from teatree.loop.scanners.slack_mentions import SlackMentionsScanner

__all__ = [
    "AssignedIssuesScanner",
    "MyPrsScanner",
    "NotionViewScanner",
    "PendingTasksScanner",
    "ReviewerPrsScanner",
    "ScanSignal",
    "Scanner",
    "SlackMentionsScanner",
]
