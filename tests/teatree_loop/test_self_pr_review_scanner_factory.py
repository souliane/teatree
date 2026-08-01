"""The self-PR review scanner builder — which reviewer, never whether (#3569).

Self-authored open PRs are ALWAYS admitted to the review board. The builder
returns a scanner whenever the overlay has a Python class and followup repos;
``pr_review_backend`` picks WHICH one — the Claude scanner routing to
``reviewing`` → ``t3:reviewer``, the codex one to ``codex_reviewing`` →
``/codex:review``. No setting value can produce "no reviewer".
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.config import PrReviewBackend
from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.overlay import OverlayBase, OverlayConfig, OverlayMetadata
from teatree.loop.scanner_factories import _self_pr_review_scanner_for
from teatree.loop.scanners.codex_review import CodexReviewScanner
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


@contextmanager
def _resolved_backend(backend: PrReviewBackend) -> Iterator[None]:
    with patch("teatree.loop.scanner_factories.resolve_pr_review_backend", return_value=backend):
        yield


class TestSelfPrReviewScannerBuilder(TestCase):
    def test_claude_backend_builds_the_claude_scanner(self) -> None:
        with _resolved_backend(PrReviewBackend.CLAUDE):
            scanner = _self_pr_review_scanner_for(_backend())
        assert isinstance(scanner, ClaudeSelfPrReviewScanner)
        assert scanner.repos == ("souliane/teatree",)
        assert scanner.overlay == "t3-teatree"

    def test_codex_backend_builds_the_codex_scanner(self) -> None:
        with _resolved_backend(PrReviewBackend.CODEX):
            scanner = _self_pr_review_scanner_for(_backend())
        assert isinstance(scanner, CodexReviewScanner)
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
