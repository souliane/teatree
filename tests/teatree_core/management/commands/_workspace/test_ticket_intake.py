"""``workspace ticket``'s forge read happens before the control-DB write lock is taken.

Every ``atomic()`` opens ``BEGIN IMMEDIATE``, so a forge read inside one holds SQLite's
write lock for as long as the forge takes, up to its 60 s timeout, while every other
writer waits at most the 30 s busy timeout.
"""

import os
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.utils.module_loading import import_string

import teatree.core.overlay_loader as overlay_loader_mod
import teatree.utils.run as utils_run_mod
from teatree.core.models import Ticket
from tests.teatree_core.management_commands._overlays import FULL_OVERLAY, SETTINGS

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class TestTheIssueTitleIsReadOutsideTheWriteTransaction(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(
            patch.object(utils_run_mod.subprocess, "run", return_value=MagicMock(returncode=0, stdout="", stderr=""))
        )
        workspace = Path(os.environ["HOME"]) / "workspace"
        for repo in ("backend", "frontend"):
            (workspace / repo / ".git").mkdir(parents=True, exist_ok=True)

    @override_settings(**SETTINGS)
    def test_the_title_fetch_runs_at_the_callers_transaction_depth(self) -> None:
        depth_at_fetch: list[int] = []

        def _title(_url: str) -> str:
            depth_at_fetch.append(len(connection.atomic_blocks))
            return "Fix Login Flow"

        overlay = import_string(FULL_OVERLAY)()
        overlay.get_issue_title = _title
        caller_depth = len(connection.atomic_blocks)

        with patch.object(overlay_loader_mod, "_discover_overlays", return_value={"test": overlay}):
            ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/4913"))

        assert depth_at_fetch == [caller_depth]
        assert Ticket.objects.get(pk=ticket_id).extra["description"] == "Fix Login Flow"
