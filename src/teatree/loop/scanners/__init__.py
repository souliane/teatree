"""Loop scanners — pure-Python signal collectors for the fat loop tick.

Each scanner exposes a :class:`Scanner` (Protocol) implementation that returns
a list of :class:`ScanSignal` records. The tick orchestrator (``loop.tick``)
runs all scanners in parallel, then dispatches each signal to either an
inline mechanical action or a phase agent (BLUEPRINT § 5.6).

Scanners are the only loop layer that touches external systems. They never
invoke Claude — that is the dispatcher's job.
"""

from teatree.loop.scanners.active_tickets import ActiveTicketsScanner
from teatree.loop.scanners.architectural_review import ArchitecturalReviewScanner
from teatree.loop.scanners.assigned_issues import AssignedIssuesScanner
from teatree.loop.scanners.base import Scanner, ScanSignal
from teatree.loop.scanners.codex_review import CodexReviewScanner, GhCodexPrApi
from teatree.loop.scanners.gitlab_approvals import GitLabApprovalsScanner
from teatree.loop.scanners.incoming_events import IncomingEventsScanner
from teatree.loop.scanners.issue_implementer import IssueImplementerScanner
from teatree.loop.scanners.my_prs import MyPrsScanner
from teatree.loop.scanners.notion_view import NotionViewScanner
from teatree.loop.scanners.outbound_audit import OutboundAuditScanner
from teatree.loop.scanners.pending_tasks import PendingTasksScanner
from teatree.loop.scanners.pr_sweep import PrSweepScanner
from teatree.loop.scanners.pr_sweep_adapters import (
    CallCommandMergeKeystone,
    GhPrApiClient,
    NullMergeNotifier,
    SlackMergeNotifier,
)
from teatree.loop.scanners.provision_smoke import ProvisionSmokeScanner
from teatree.loop.scanners.pull_main_clone import PullMainCloneScanner
from teatree.loop.scanners.red_card import RedCardScanner
from teatree.loop.scanners.resource_pressure import ResourcePressureScanner
from teatree.loop.scanners.review_nag import ReviewNagScanner
from teatree.loop.scanners.review_request_merge_react import ReviewRequestMergeReactScanner
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner
from teatree.loop.scanners.scanning_news import ScanningNewsScanner
from teatree.loop.scanners.self_update import SelfUpdateScanner
from teatree.loop.scanners.slack_broadcasts import (
    BackendChannelHistoryFetcher,
    GlabGhMrStateClassifier,
    SlackBroadcastsScanner,
)
from teatree.loop.scanners.slack_dm_inbound import SlackDmInboundScanner
from teatree.loop.scanners.slack_mentions import SlackMentionsScanner
from teatree.loop.scanners.slack_review_intent import SlackReviewIntentScanner
from teatree.loop.scanners.stale_tickets import StaleTicketsScanner
from teatree.loop.scanners.ticket_completion import TicketCompletionScanner
from teatree.loop.scanners.ticket_dispositions import TicketDispositionScanner
from teatree.loop.scanners.todo_sweep import TodoSweepScanner

__all__ = [
    "ActiveTicketsScanner",
    "ArchitecturalReviewScanner",
    "AssignedIssuesScanner",
    "BackendChannelHistoryFetcher",
    "CallCommandMergeKeystone",
    "CodexReviewScanner",
    "GhCodexPrApi",
    "GhPrApiClient",
    "GitLabApprovalsScanner",
    "GlabGhMrStateClassifier",
    "IncomingEventsScanner",
    "IssueImplementerScanner",
    "MyPrsScanner",
    "NotionViewScanner",
    "NullMergeNotifier",
    "OutboundAuditScanner",
    "PendingTasksScanner",
    "PrSweepScanner",
    "ProvisionSmokeScanner",
    "PullMainCloneScanner",
    "RedCardScanner",
    "ResourcePressureScanner",
    "ReviewNagScanner",
    "ReviewRequestMergeReactScanner",
    "ReviewerPrsScanner",
    "ScanSignal",
    "Scanner",
    "ScanningNewsScanner",
    "SelfUpdateScanner",
    "SlackBroadcastsScanner",
    "SlackDmInboundScanner",
    "SlackMentionsScanner",
    "SlackMergeNotifier",
    "SlackReviewIntentScanner",
    "StaleTicketsScanner",
    "TicketCompletionScanner",
    "TicketDispositionScanner",
    "TodoSweepScanner",
]
