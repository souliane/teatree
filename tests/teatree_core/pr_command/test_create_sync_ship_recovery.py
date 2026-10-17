import subprocess
from typing import cast
from unittest.mock import patch

import httpx
import pytest
from django.core.management import call_command
from django.db import OperationalError
from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import PullRequest, Session, Task, Ticket
from teatree.core.runners.ship import ShipExecutor

from ._shared import _PR_URL, _code_host, _gates_pass, _ship_externals, _shippable_ticket

pytestmark = pytest.mark.usefixtures("readable_ship_tree")


class TestSyncShipRecovery(TestCase):
    def _assert_external_failure_is_structured(self, error: Exception, *, during_push: bool = False) -> None:
        ticket = _shippable_ticket()
        host = _code_host()
        if not during_push:
            host.create_pr.side_effect = error

        with _ship_externals(host) as push, _gates_pass():
            if during_push:
                push.side_effect = error
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))

        assert result["ok"] is False
        assert str(error) in str(result["detail"])
        assert result["state"] == Ticket.State.SELF_REVIEWED

    def test_http_status_error_from_forge_is_structured(self) -> None:
        request = httpx.Request("POST", "https://gitlab.example/api/merge_requests")
        error = httpx.HTTPStatusError("409 conflict", request=request, response=httpx.Response(409, request=request))
        self._assert_external_failure_is_structured(error)

    def test_timeout_expired_from_push_is_structured(self) -> None:
        self._assert_external_failure_is_structured(subprocess.TimeoutExpired("gh pr create", 2), during_push=True)

    def test_os_error_from_forge_is_structured(self) -> None:
        self._assert_external_failure_is_structured(OSError("gh is missing"))

    def test_retry_adopts_pr_once_its_verification_confirms(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()
        host.get_pr_open_state.return_value = PrOpenState.UNKNOWN

        with _ship_externals(host), _gates_pass():
            first = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))

        ticket.refresh_from_db()
        assert ticket.extra["pr_url_by_branch"]["feature-branch"] == _PR_URL
        assert _PR_URL not in ticket.extra.get("pr_urls", [])
        assert not PullRequest.objects.filter(url=_PR_URL).exists()

        host.get_pr_open_state.return_value = PrOpenState.OPEN
        with _ship_externals(host) as retry_push, _gates_pass():
            second = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))

        assert first["ok"] is False
        assert _PR_URL in str(first["detail"])
        assert second["ok"] is True
        assert second["detail"] == _PR_URL
        retry_push.assert_not_called()
        host.create_pr.assert_called_once()
        assert PullRequest.objects.get(url=_PR_URL).create_verification == PullRequest.CreateVerification.CONFIRMED

    def test_retry_does_not_adopt_pr_its_verification_still_cannot_confirm(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()
        host.get_pr_open_state.return_value = PrOpenState.UNKNOWN

        with _ship_externals(host), _gates_pass():
            call_command("pr", "create", str(ticket.pk), sync=True)
            retry = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))

        assert retry["ok"] is False
        assert retry["state"] == Ticket.State.SELF_REVIEWED
        assert host.create_pr.call_count == 2
        assert not PullRequest.objects.filter(url=_PR_URL).exists()

    def test_retry_adopts_pr_when_recording_fails_once_after_create(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()
        original_record = ShipExecutor._record_pr_url
        attempts = 0
        record_error = OperationalError("recording was locked")

        def record_after_transient_failure(ticket: Ticket, url: str, branch: str) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise record_error
            original_record(ticket, url, branch)

        with (
            _ship_externals(host),
            _gates_pass(),
            patch.object(ShipExecutor, "_record_pr_url", side_effect=record_after_transient_failure),
        ):
            first = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))

        with _ship_externals(host) as retry_push, _gates_pass():
            second = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))

        assert first["ok"] is False
        assert _PR_URL in str(first["detail"])
        assert second["ok"] is True
        retry_push.assert_not_called()
        host.create_pr.assert_called_once()

    def test_pr_opened_sync_ship_consumes_pending_shipping_task(self) -> None:
        ticket = _shippable_ticket()
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.PR_OPENED)
        task = Task.objects.create(ticket=ticket, session=Session.objects.get(ticket=ticket), phase="shipping")

        with _ship_externals(_code_host()), _gates_pass():
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))

        task.refresh_from_db()
        assert result["ok"] is True
        assert task.status == Task.Status.COMPLETED
