"""The self-PR review scanner builder — always Claude, no backend selector (#3569).

Self-authored open PRs are ALWAYS admitted to the review board: the builder
returns a :class:`ClaudeSelfPrReviewScanner` (routing to the same ``reviewing`` →
``t3:reviewer`` gate colleague PRs get) whenever the overlay has a Python class
and followup repos. There is no codex/claude/auto selector and no fleet-doctrine
gate — codex is retired as the self-review mechanism.
"""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.overlay import OverlayBase, OverlayConfig, OverlayMetadata
from teatree.loop.scanner_factories import _self_pr_review_scanner_for
from teatree.loop.scanners.self_pr_review import ClaudeSelfPrReviewScanner


def _backend(*, name: str = "t3-teatree", repos: tuple[str, ...] = ("souliane/teatree",)) -> OverlayBackends:
    config = MagicMock(spec=OverlayConfig)
    config.get_github_token = lambda: ""
    metadata = MagicMock(spec=OverlayMetadata)
    metadata.get_followup_repos = lambda: list(repos)
    overlay = MagicMock(spec=OverlayBase)
    overlay.config = config
    overlay.metadata = metadata
    return OverlayBackends(
        name=name,
        hosts=(MagicMock(spec=CodeHostBackend),),
        messaging=None,
        ready_labels=(),
        overlay=overlay,
        identities=(),
    )


class TestSelfPrReviewScannerBuilder(TestCase):
    def test_builds_the_claude_self_pr_scanner(self) -> None:
        scanner = _self_pr_review_scanner_for(_backend())
        assert isinstance(scanner, ClaudeSelfPrReviewScanner)
        assert scanner.repos == ("souliane/teatree",)
        assert scanner.overlay == "t3-teatree"

    def test_overlay_without_python_class_returns_none(self) -> None:
        backend = OverlayBackends(
            name="t3-teatree",
            hosts=(MagicMock(spec=CodeHostBackend),),
            messaging=None,
            ready_labels=(),
            overlay=None,
        )
        assert _self_pr_review_scanner_for(backend) is None

    def test_overlay_with_no_followup_repos_returns_none(self) -> None:
        assert _self_pr_review_scanner_for(_backend(repos=())) is None
