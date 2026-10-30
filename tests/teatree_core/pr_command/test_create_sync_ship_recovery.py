import subprocess
from typing import cast
from unittest.mock import patch

import httpx
import pytest
from django.core.management import call_command
from django.db import OperationalError
from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState
from teatree.core.gates import pr_budget_gate
from teatree.core.gates.pr_budget_forge import reset_forge_pr_budget_cache
from teatree.core.management.commands._ship import gates as ship_gates_mod
from teatree.core.models import PullRequest, Session, Task, Ticket
from teatree.core.runners.ship import ShipExecutor

from ._shared import _PR_URL, _code_host, _gates_pass, _ship_externals, _shippable_ticket

pytestmark = pytest.mark.usefixtures("readable_ship_tree")


class TestSyncShipRecovery(TestCase):
    def test_retry_adopts_marker_when_forge_lists_the_pr(self) -> None:
        ticket = _shippable_ticket()
        ticket.issue_url = "https://github.com/acme/backend/issues/123"
        ticket.save()
        host = _code_host()
        host.get_pr_open_state.return_value = PrOpenState.UNKNOWN
        host.list_my_prs.return_value = []

        with (
            _ship_externals(host) as push,
            _gates_pass(),
            patch.object(ship_gates_mod.git, "remote_slug", return_value="acme/backend"),
            patch.object(ship_gates_mod, "_resolve_host_for_repo", return_value=host),
            patch.object(pr_budget_gate, "resolve_pr_budget", return_value=1),
        ):
            first = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))
            ticket.refresh_from_db()
            assert ticket.extra["unverified_pr_by_branch"]["feature-branch"] == _PR_URL
            push.reset_mock()
            host.list_my_prs.return_value = [{"html_url": _PR_URL, "body": "Closes #123"}]
            host.get_pr_open_state.return_value = PrOpenState.OPEN
            reset_forge_pr_budget_cache()
            retry = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))

        assert first["ok"] is False
        assert retry["ok"] is True
        assert retry["detail"] == _PR_URL
        push.assert_not_called()
        host.create_pr.assert_called_once()
        assert PullRequest.objects.filter(ticket=ticket, url=_PR_URL).exists()

    def test_unconfirmed_marker_does_not_refuse_retry_before_executor(self) -> None:
        ticket = _shippable_ticket()
        ticket.issue_url = "https://github.com/acme/backend/issues/124"
        ticket.save()
        host = _code_host()
        host.get_pr_open_state.return_value = PrOpenState.UNKNOWN
        host.list_my_prs.return_value = []

        with (
            _ship_externals(host) as push,
            _gates_pass(),
            patch.object(ship_gates_mod.git, "remote_slug", return_value="acme/backend"),
            patch.object(ship_gates_mod, "_resolve_host_for_repo", return_value=host),
            patch.object(pr_budget_gate, "resolve_pr_budget", return_value=1),
        ):
            call_command("pr", "create", str(ticket.pk), sync=True)
            host.list_my_prs.return_value = [{"html_url": _PR_URL, "body": "Closes #124"}]
            reset_forge_pr_budget_cache()
            retry = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))

        assert retry["ok"] is False
        assert "max_open_prs_per_repo_per_ticket" not in str(retry["detail"])
        assert push.call_count == 2
        assert host.create_pr.call_count == 2

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
        assert ticket.extra["unverified_pr_by_branch"]["feature-branch"] == _PR_URL
        assert "pr_url_by_branch" not in ticket.extra
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
        ticket.refresh_from_db()
        assert "unverified_pr_by_branch" not in ticket.extra

    def test_retry_does_not_adopt_pr_its_verification_still_cannot_confirm(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()
        host.get_pr_open_state.return_value = PrOpenState.UNKNOWN

        with (
            _ship_externals(host) as push,
            _gates_pass(),
            patch("teatree.core.runners.ship.git.remote_slug", return_value="acme/backend"),
            patch.object(pr_budget_gate, "resolve_pr_budget", return_value=1),
            patch.object(pr_budget_gate, "cached_forge_open_pr_urls_for_ticket", return_value=set()),
        ):
            first = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))
            ticket.refresh_from_db()
            assert ticket.extra["unverified_pr_by_branch"]["feature-branch"] == _PR_URL
            retry = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))

        assert first["ok"] is False
        assert retry["ok"] is False
        assert retry["state"] == Ticket.State.SELF_REVIEWED
        assert "verify-by-re-read" in str(retry["detail"])
        assert push.call_count == 2
        assert host.create_pr.call_count == 2
        assert not PullRequest.objects.filter(url=_PR_URL).exists()

    def test_fresh_create_after_unconfirmed_retry_clears_marker(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()
        host.get_pr_open_state.side_effect = [PrOpenState.UNKNOWN, PrOpenState.UNKNOWN, PrOpenState.OPEN]

        with _ship_externals(host), _gates_pass():
            call_command("pr", "create", str(ticket.pk), sync=True)
            retry = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))

        ticket.refresh_from_db()
        assert retry["ok"] is True
        assert host.create_pr.call_count == 2
        assert "unverified_pr_by_branch" not in ticket.extra

    def test_recording_failure_preserves_verification_reason(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()
        host.get_pr_open_state.return_value = PrOpenState.UNKNOWN

        with (
            _ship_externals(host),
            _gates_pass(),
            patch.object(ShipExecutor, "record_opened_pr", side_effect=OperationalError("recording failed")),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))

        assert result["ok"] is False
        assert "verify-by-re-read" in str(result["detail"])
        assert "recording failed" in str(result["detail"])

    def test_recording_retry_success_reports_original_failure(self) -> None:
        ticket = _shippable_ticket()
        host = _code_host()
        host.get_pr_open_state.return_value = PrOpenState.UNKNOWN
        original_record = ShipExecutor.record_opened_pr
        calls = 0
        record_error = OperationalError("first recording locked")

        def record_after_transient_failure(executor: ShipExecutor) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise record_error
            original_record(executor)

        with (
            _ship_externals(host),
            _gates_pass(),
            patch.object(ShipExecutor, "record_opened_pr", autospec=True, side_effect=record_after_transient_failure),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk), sync=True))

        assert result["ok"] is False
        assert calls == 2
        assert "verify-by-re-read" in str(result["detail"])
        assert "first recording locked" in str(result["detail"])
        assert "recording it failed" not in str(result["detail"])
        ticket.refresh_from_db()
        assert ticket.extra["unverified_pr_by_branch"]["feature-branch"] == _PR_URL

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
