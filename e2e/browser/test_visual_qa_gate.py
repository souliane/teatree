"""The ship gate judges pages loaded by a real browser in its own namespace."""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import NoReturn
from unittest.mock import MagicMock, patch

from django.test import TestCase

from e2e.browser.site import BrokenSite
from teatree.contrib.t3_teatree.overlay import TeatreeOverlay
from teatree.core.management.commands import pr as pr_command
from teatree.core.models import Ticket, Worktree
from teatree.types import RunCommand
from teatree.visual_qa import MAX_PAGES


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
        live_port = int(self.site.origin.rsplit(":", 1)[1])
        with (
            patch("teatree.core.management.commands._ship.gates.get_overlay", return_value=overlay),
            patch("teatree.visual_qa.changed_files", return_value=["a.html"]),
            patch(
                "teatree.core.management.commands._e2e_resolvers._disc.compose_frontend_port", return_value=live_port
            ),
            patch("teatree.core.management.commands._ship.gates.git.head_sha", return_value="a" * 40),
        ):
            gate = pr_command.__dict__["_run_visual_qa_gate"]
            return gate(self.ticket, self.worktree)

    def test_clean_page_passes_with_a_real_browser(self) -> None:
        result = self._run_gate("/clean")
        if result is not None:
            _fail(f"clean page did not pass: {result}")
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

    def test_real_checkout_hook_serves_all_dashboard_and_admin_targets(self) -> None:
        self.ticket.overlay = "t3-teatree"
        self.ticket.save(update_fields=["overlay"])
        self.worktree.overlay = "t3-teatree"
        self.worktree.extra = {"worktree_path": str(Path.cwd())}
        self.worktree.save(update_fields=["overlay", "extra"])
        with (
            patch("teatree.core.management.commands._ship.gates.get_overlay", return_value=TeatreeOverlay()),
            patch(
                "teatree.visual_qa.changed_files",
                return_value=["src/teatree/dash/templates/dash/sessions.html"],
            ),
            patch("teatree.core.management.commands._ship.gates.git.head_sha", return_value="a" * 40),
        ):
            gate = pr_command.__dict__["_run_visual_qa_gate"]
            result = gate(self.ticket, self.worktree)

        if result is not None:
            _fail(f"checkout gate did not pass: {result}")
        self.ticket.refresh_from_db()
        evidence = self.ticket.extra["visual_qa"]
        if evidence["pages_checked"] != MAX_PAGES:
            _fail(f"wrong page count: {evidence['pages_checked']}")
        if evidence["errors"] != 0:
            _fail(f"browser findings: {evidence['details']}")
        if not evidence["base_url"].startswith("http://127.0.0.1:"):
            _fail(f"not served beside the browser: {evidence['base_url']}")
        if not any(detail["url"].endswith("/dash/board/") for detail in evidence["details"]):
            _fail("dashboard board was not checked")

    def test_missing_checkout_server_script_is_did_not_run(self) -> None:
        overlay = TeatreeOverlay()
        missing_script = "missing-serve.py"
        command = RunCommand(
            args=[sys.executable, missing_script],
            cwd=Path.cwd(),
            env={"DJANGO_SETTINGS_MODULE": "e2e.dash.settings"},
        )
        with (
            patch("teatree.core.management.commands._ship.gates.get_overlay", return_value=overlay),
            patch.object(overlay.runtime, "visual_qa_server", return_value=command),
            patch("teatree.visual_qa.changed_files", return_value=["src/teatree/dash/templates/dash/sessions.html"]),
        ):
            gate = pr_command.__dict__["_run_visual_qa_gate"]
            result = gate(self.ticket, self.worktree)

        if result is None or not str(result["error"]).startswith("[gate:visual_qa] DID NOT RUN"):
            _fail(f"missing script was not reported as DID NOT RUN: {result}")
        self.ticket.refresh_from_db()
        if missing_script not in self.ticket.extra["visual_qa"]["not_run_reason"]:
            _fail("missing script was absent from the recorded reason")

    def test_broken_checkout_template_is_refused(self) -> None:
        source = Path.cwd()
        with tempfile.TemporaryDirectory(prefix="visual-qa-broken-branch-") as scratch:
            branch = Path(scratch)
            shutil.copytree(
                source / "src" / "teatree", branch / "src" / "teatree", ignore=shutil.ignore_patterns("__pycache__")
            )
            shutil.copytree(source / "e2e", branch / "e2e", ignore=shutil.ignore_patterns("__pycache__"))
            template = branch / "src" / "teatree" / "dash" / "templates" / "dash" / "board.html"
            template.write_text(
                template.read_text().replace(
                    "{% block content %}", "{% block content %}<script>console.error('broken-template')</script>", 1
                )
            )
            self.worktree.repo_path = str(branch)
            self.worktree.save(update_fields=["repo_path"])
            overlay = TeatreeOverlay()
            command = RunCommand(
                args=[sys.executable, "e2e/dash/serve.py"],
                cwd=branch,
                env={
                    "DJANGO_SETTINGS_MODULE": "e2e.dash.settings",
                    "PYTHONPATH": os.pathsep.join((str(branch / "src"), str(branch), str(source))),
                },
            )
            with (
                patch("teatree.core.management.commands._ship.gates.get_overlay", return_value=overlay),
                patch.object(overlay.runtime, "visual_qa_server", return_value=command),
                patch("teatree.visual_qa.changed_files", return_value=["src/teatree/dash/templates/dash/board.html"]),
            ):
                gate = pr_command.__dict__["_run_visual_qa_gate"]
                result = gate(self.ticket, self.worktree)

        if result is None or not str(result["error"]).startswith("[gate:visual_qa] REFUSED"):
            _fail(f"broken branch was not refused: {result}")
        self.ticket.refresh_from_db()
        details = self.ticket.extra["visual_qa"]["details"]
        if not any(
            error["kind"] == "console" and "broken-template" in error["message"]
            for detail in details
            for error in detail["errors"]
        ):
            _fail("the broken template finding was not recorded")
