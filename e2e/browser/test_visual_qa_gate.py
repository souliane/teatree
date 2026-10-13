"""The ship gate judges pages loaded by a real browser in its own namespace."""

from pathlib import Path
from typing import NoReturn
from unittest.mock import MagicMock, patch

from django.test import TestCase

from e2e.browser.site import BrokenSite
from teatree.core.management.commands import pr as pr_command
from teatree.core.models import Ticket, Worktree


def _fail(message: str) -> NoReturn:
    raise AssertionError(message)


class TestVisualQAGateLiveBrowser(TestCase):
    def setUp(self) -> None:
        self.site = BrokenSite()
        self.site.start()
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.test/issues/5239")
        self.worktree = Worktree.objects.create(
            ticket=self.ticket, overlay="test", repo_path=str(Path.cwd()), branch="visual-qa"
        )

    def tearDown(self) -> None:
        self.site.stop()

    def _run_gate(self, target: str) -> dict[str, object] | None:
        overlay = MagicMock()
        overlay.review.visual_qa_targets.return_value = [target]
        overlay.runtime.visual_qa_server.return_value = None
        with (
            patch("teatree.core.management.commands._ship.gates.get_overlay", return_value=overlay),
            patch("teatree.visual_qa.changed_files", return_value=["a.html"]),
            patch(
                "teatree.core.management.commands._ship.gates.resolve_visual_qa_base_url", return_value=self.site.origin
            ),
            patch("teatree.core.management.commands._ship.gates.git.head_sha", return_value="a" * 40),
        ):
            gate = pr_command.__dict__["_run_visual_qa_gate"]
            return gate(self.ticket, self.worktree)

    def test_clean_page_passes_with_a_real_browser(self) -> None:
        if self._run_gate("/clean") is not None:
            _fail("clean page did not pass")
        self.ticket.refresh_from_db()
        if self.ticket.extra["visual_qa"]["pages_checked"] != 1:
            _fail("clean page was not checked")
        if self.ticket.extra["visual_qa"]["errors"] != 0:
            _fail("clean page had findings")

    def test_console_error_and_missing_asset_refuse(self) -> None:
        refusal = self._run_gate("/broken")

        if refusal is None:
            _fail("broken page passed")
        if "REFUSED" not in str(refusal["error"]):
            _fail("broken page was not refused")
        self.ticket.refresh_from_db()
        kinds = {error["kind"] for detail in self.ticket.extra["visual_qa"]["details"] for error in detail["errors"]}
        if not {"console", "http"} <= kinds:
            _fail(f"missing browser findings: {kinds}")
