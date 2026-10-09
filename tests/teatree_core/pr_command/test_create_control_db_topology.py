from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import _pr_control_db, _pr_ticket_resolve
from teatree.core.models import Ticket
from teatree.paths import CONTROL_DB_DIR_ENV, DB_FILENAME

from ._shared import _MOCK_OVERLAY


class TestPrCreateReadsTheControlDbTopologyFirst(TestCase):
    """``pr create`` states an unreachable control DB instead of dying on it (#4170).

    The canonical control DB lives in a named volume with no host path, so a host
    invocation used to reach ``resolve_ticket`` and die on a raw ``OperationalError``
    that named neither the cause nor the remedy — and three agents in one day answered
    it by falling back to a raw ``gh pr create``, the workaround the standing rule
    forbids. The topology is read BEFORE any ORM touch because it is a statement about
    where this code is running, not an exception to recover from afterwards.
    """

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    _CONTAINER_ONLY = Path("/nonexistent/container-only/control-db")

    def _aim_at_the_container_only_db(self) -> None:
        self._monkeypatch.setenv(CONTROL_DB_DIR_ENV, str(self._CONTAINER_ONLY))
        self._monkeypatch.setattr(_pr_control_db, "configured_db_path", lambda: self._CONTAINER_ONLY / DB_FILENAME)

    def test_refuses_with_the_container_remedy_and_never_touches_the_orm(self) -> None:
        self._aim_at_the_container_only_db()
        resolve = MagicMock(side_effect=Ticket.DoesNotExist)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(_pr_ticket_resolve, "resolve_ticket", resolve),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", "4242"))

        error = str(result["error"])
        assert str(self._CONTAINER_ONLY) in error
        assert "deploy/t3" in str(result["hint"])
        resolve.assert_not_called()

    def test_a_reachable_database_still_gets_the_real_answer(self) -> None:
        """Anti-vacuous: a guard that always fired would mask every ORM answer.

        The test database is not under the container-only mount, so the command must
        reach ``resolve_ticket`` and report the MISSING TICKET — the second, distinct
        cause the refusals must never collapse into one.
        """
        self._monkeypatch.setenv(CONTROL_DB_DIR_ENV, str(self._CONTAINER_ONLY))

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "create", "4242"))

        assert "workspace ticket" in str(result["error"])
        assert "deploy/t3" not in str(result["hint"])

    def test_the_ticketless_refusal_names_the_ticketless_route(self) -> None:
        """``pr create`` requires a Ticket, so the refusal names the command that does not."""
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "create", "4242"))

        assert "ensure-pr" in str(result["error"])
