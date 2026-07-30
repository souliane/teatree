"""`t3 ticket create-sub` — create a child work item nested under a parent."""

from typing import cast
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase

from teatree.backends import loader as loader_mod
from teatree.core import overlay_loader as overlay_loader_mod
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}
_PARENT_URL = "https://gitlab.com/org/repo/-/work_items/8545"
_CHILD = {"iid": 8546, "web_url": "https://gitlab.com/org/repo/-/work_items/8546"}


class TicketCreateSubCommandTest(TestCase):
    def setUp(self) -> None:
        super().setUp()
        # `create-sub` now routes the child title/body/labels through the scanned
        # forge-write seam; pin the leak-gate visibility probe to PRIVATE (clean
        # pass, no gh/glab subprocess) so these mechanics tests stay deterministic.
        # The leak-refusal path has its own suite (test_forge_write_cli_scrub.py).
        patcher = patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_creates_child_via_resolved_code_host(self) -> None:
        host = MagicMock()
        host.create_sub_issue.return_value = _CHILD

        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
        ):
            result = cast(
                "dict[str, object]",
                call_command("ticket", "create-sub", parent=_PARENT_URL, title="Finding 1", labels="sec,pentest"),
            )

        assert result == {
            "parent_url": _PARENT_URL,
            "child_iid": 8546,
            "child_url": _CHILD["web_url"],
        }
        host.create_sub_issue.assert_called_once_with(
            parent_url=_PARENT_URL,
            title="Finding 1",
            body="",
            labels=["sec", "pentest"],
            child_type="Task",
        )

    def test_passes_explicit_type_and_inline_description(self) -> None:
        host = MagicMock()
        host.create_sub_issue.return_value = _CHILD

        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
        ):
            call_command(
                "ticket",
                "create-sub",
                parent=_PARENT_URL,
                title="An incident",
                description="details here",
                type="Incident",
            )

        host.create_sub_issue.assert_called_once_with(
            parent_url=_PARENT_URL,
            title="An incident",
            body="details here",
            labels=[],
            child_type="Incident",
        )


class TicketCreateSubDescriptionFileTest(TestCase):
    def test_reads_description_from_file(self) -> None:
        import tempfile  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        host = MagicMock()
        host.create_sub_issue.return_value = _CHILD

        with tempfile.TemporaryDirectory() as tmp:
            desc_path = Path(tmp) / "child.md"
            desc_path.write_text("From a file\n", encoding="utf-8")

            with (
                patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
                patch.object(loader_mod, "get_code_host_for_url", return_value=host),
            ):
                call_command(
                    "ticket",
                    "create-sub",
                    parent=_PARENT_URL,
                    title="t",
                    description_file=str(desc_path),
                )

        host.create_sub_issue.assert_called_once_with(
            parent_url=_PARENT_URL,
            title="t",
            body="From a file\n",
            labels=[],
            child_type="Task",
        )


class TicketCreateSubErrorTest(TestCase):
    def test_refuses_when_required_options_blank(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command("ticket", "create-sub", parent=_PARENT_URL),
        )
        assert result == {"error": "create-sub refused: --parent and --title are both required"}

    def test_errors_when_no_code_host_resolves(self) -> None:
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=None),
        ):
            result = cast(
                "dict[str, object]",
                call_command("ticket", "create-sub", parent=_PARENT_URL, title="t"),
            )
        assert result == {"error": f"No code host could be resolved for {_PARENT_URL}"}

    def test_propagates_code_host_error(self) -> None:
        host = MagicMock()
        host.create_sub_issue.return_value = {"error": "Unknown work item type: Bogus"}
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
        ):
            result = cast(
                "dict[str, object]",
                call_command("ticket", "create-sub", parent=_PARENT_URL, title="t", type="Bogus"),
            )
        assert result == {"error": "Unknown work item type: Bogus"}
