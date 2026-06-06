"""Back-compat — legacy scanner imports keep working.

The §1434 directory consolidation introduced ``teatree.loops.<domain>``
subpackages but kept the legacy ``teatree.loop.scanners`` import surface
untouched (no physical move in this MR — the legacy module stays
authoritative and the new mini-loops delegate to it via
``teatree.loop.domain_jobs``). This test pins that contract: every
existing import path callers rely on must keep resolving.
"""

from teatree.loop.scanners import (
    ActiveTicketsScanner,
    MyPrsScanner,
    PendingTasksScanner,
    ReviewerPrsScanner,
    SlackMentionsScanner,
)


class TestLegacyScannerImports:
    def test_my_prs_scanner_imports_from_legacy_path(self) -> None:
        assert MyPrsScanner is not None

    def test_reviewer_prs_scanner_imports_from_legacy_path(self) -> None:
        assert ReviewerPrsScanner is not None

    def test_pending_tasks_scanner_imports_from_legacy_path(self) -> None:
        assert PendingTasksScanner is not None

    def test_slack_mentions_scanner_imports_from_legacy_path(self) -> None:
        assert SlackMentionsScanner is not None

    def test_active_tickets_scanner_imports_from_legacy_path(self) -> None:
        assert ActiveTicketsScanner is not None
