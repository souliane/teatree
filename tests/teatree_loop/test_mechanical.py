"""Mechanical action handlers — inline ticket transitions during a tick."""

import datetime as dt
import logging
from typing import cast
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Session, Task, Ticket
from teatree.loop.dispatch import ActionPayload, DispatchAction
from teatree.loop.mechanical import (
    HANDLERS,
    assign_gitlab_reviewer,
    complete_ticket,
    ignore_disposed_ticket,
    payload_author_untrusted_public,
    reopen_ticket,
    reviewer_task_orphaned,
    reviewer_task_self_authored,
)
from teatree.loop.tick import TickReport
from teatree.loop.tick_recovery import _execute_mechanical


def _payload(**kwargs: object) -> ActionPayload:
    return cast("ActionPayload", kwargs)


def _run_mechanical(zone: str, **payload: object) -> TickReport:
    """Drive one mechanical action through ``_execute_mechanical``.

    This is the exact path that turns a handler exception into the
    every-tick WARN noise of #1087 (``report.errors`` + an ERROR log on
    ``teatree.loop.tick_recovery``).
    """
    report = TickReport(started_at=dt.datetime.now(dt.UTC))
    report.actions = [
        DispatchAction(kind="mechanical", zone=zone, detail="x", payload=_payload(**payload)),
    ]
    _execute_mechanical(report)
    return report


class TestIgnoreDisposedTicket(TestCase):
    def test_transitions_ticket_to_ignored(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/1")
        ignore_disposed_ticket(_payload(ticket_id=ticket.pk, reason="duplicate"))
        ticket.refresh_from_db()
        assert ticket.state == "ignored"

    def test_no_op_when_ticket_id_missing(self) -> None:
        ignore_disposed_ticket(_payload(reason="duplicate"))  # should not raise

    def test_idempotent_on_already_ignored_ticket(self) -> None:
        """#1087: re-dispositioning an already-ignored ticket is a clean no-op.

        Pre-fix this drove ``ticket.ignore()`` from state ``ignored`` —
        ``ignored`` is not a source state of the ``ignore`` transition, so
        django-fsm raised ``TransitionNotAllowed`` which ``_execute_mechanical``
        caught, recorded in ``report.errors`` and logged on every tick. The
        desired end state already holds, so this must be a silent no-op: no
        exception, no recorded error, no log line.
        """
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/1", state="ignored")

        with self.assertNoLogs("teatree.loop.tick_recovery", level="ERROR"):
            report = _run_mechanical("ticket_disposition", ticket_id=ticket.pk, reason="duplicate")

        assert report.errors == {}
        ticket.refresh_from_db()
        assert ticket.state == "ignored"


class TestCompleteTicket(TestCase):
    def test_advances_from_shipped_to_in_review(self) -> None:
        # Direct state injection bypasses the full FSM setup chain.
        Ticket.objects.filter().delete()
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/1", state="shipped")
        complete_ticket(_payload(ticket_id=ticket.pk))
        ticket.refresh_from_db()
        # The three sequential `if` blocks cascade through review_request → mark_merged
        # → retrospect on the same call.
        assert ticket.state in {"in_review", "merged", "delivered", "retrospected"}

    def test_no_op_when_ticket_not_in_completable_state(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/2", state="scoped")
        complete_ticket(_payload(ticket_id=ticket.pk))
        ticket.refresh_from_db()
        assert ticket.state == "scoped"

    def test_no_op_when_ticket_id_missing(self) -> None:
        complete_ticket(_payload())


class TestReopenTicket(TestCase):
    def test_transitions_shipped_ticket_back_to_started(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/1", state="shipped")
        reopen_ticket(_payload(ticket_id=ticket.pk, ticket_state="shipped"))
        ticket.refresh_from_db()
        assert ticket.state == "started"

    def test_no_op_when_ticket_id_missing(self) -> None:
        reopen_ticket(_payload(ticket_state="?"))

    def test_idempotent_when_already_started(self) -> None:
        """#1087: a re-emitted reopen signal on an already-STARTED ticket no-ops.

        ``reopen`` targets ``started`` but ``started`` is not one of its
        source states, so re-driving it on an already-reopened ticket raised
        the same ``TransitionNotAllowed`` every-tick noise as the ignore path.
        """
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/3", state="started")

        with self.assertNoLogs("teatree.loop.tick_recovery", level="ERROR"):
            report = _run_mechanical("ticket_reopen", ticket_id=ticket.pk, ticket_state="started")

        assert report.errors == {}
        ticket.refresh_from_db()
        assert ticket.state == "started"


class TestReviewerTaskOrphaned(TestCase):
    """#998: complete the orphaned reviewing task when MR is merged externally."""

    def _make_reviewer_ticket_with_pending_task(self, url: str) -> tuple[Ticket, Task]:
        ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url=url,
            overlay="acme",
            extra={"reviewed_sha": "abc"},
        )
        session = Session.objects.create(ticket=ticket, agent_id="external-review")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Review needed",
        )
        return ticket, task

    def test_completes_pending_reviewing_task(self) -> None:
        ticket, task = self._make_reviewer_ticket_with_pending_task("https://x/-/merge_requests/373")

        reviewer_task_orphaned(_payload(ticket_id=ticket.pk, url=ticket.issue_url))

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_no_op_when_ticket_id_missing(self) -> None:
        reviewer_task_orphaned(_payload(url="https://x/-/merge_requests/1"))  # must not raise

    def test_no_op_when_ticket_does_not_exist(self) -> None:
        reviewer_task_orphaned(_payload(ticket_id=99999, url="https://x/-/merge_requests/1"))

    def test_idempotent_on_already_completed_task(self) -> None:
        ticket, task = self._make_reviewer_ticket_with_pending_task("https://x/-/merge_requests/374")
        task.status = Task.Status.COMPLETED
        task.save(update_fields=["status"])

        # No open task to complete — handler is a no-op.
        reviewer_task_orphaned(_payload(ticket_id=ticket.pk, url=ticket.issue_url))

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED


class TestReviewerTaskSelfAuthoredAuthorTrust(TestCase):
    """#1773: an untrusted public-repo author never gets the 'no self-review' skip."""

    def _make_reviewer_ticket_with_pending_task(self, url: str) -> tuple[Ticket, Task]:
        ticket = Ticket.objects.create(role=Ticket.Role.REVIEWER, issue_url=url, overlay="acme")
        session = Session.objects.create(ticket=ticket, agent_id="external-review")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Review needed",
        )
        return ticket, task

    def test_untrusted_public_author_does_not_close_reviewing_task(self) -> None:
        url = "https://github.com/souliane/teatree/pull/4242"
        ticket, task = self._make_reviewer_ticket_with_pending_task(url)
        with patch("teatree.core.author_trust.repo_is_internal", return_value=False):
            reviewer_task_self_authored(_payload(ticket_id=ticket.pk, url=url, author="evilhacker"))
        task.refresh_from_db()
        assert task.status != Task.Status.COMPLETED

    def test_no_author_payload_keeps_legacy_self_authored_close(self) -> None:
        url = "https://github.com/souliane/teatree/pull/4243"
        ticket, task = self._make_reviewer_ticket_with_pending_task(url)
        reviewer_task_self_authored(_payload(ticket_id=ticket.pk, url=url))
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_classifier_helper_matches_shared_seam(self) -> None:
        url = "https://github.com/souliane/teatree/pull/1"
        with patch("teatree.core.author_trust.repo_is_internal", return_value=False):
            assert payload_author_untrusted_public({"url": url, "author": "evilhacker"}) is True
        with patch("teatree.core.author_trust.repo_is_internal", return_value=True):
            assert payload_author_untrusted_public({"url": url, "author": "evilhacker"}) is False
        assert payload_author_untrusted_public({"author": "evilhacker"}) is False


class TestAssignGitlabReviewer:
    """#1295 cap B: Slack-mention pickup appends the user as reviewer.

    The handler walks: payload → overlay → code host → ``assign_reviewer``.
    Each step is best-effort: missing payload, no host, no method, raising
    host, or False return — all log and exit cleanly without raising.
    """

    def test_no_op_when_url_missing(self) -> None:
        # Must not even touch the overlay loader.
        assign_gitlab_reviewer(_payload(reviewer_username="alice"))

    def test_no_op_when_username_missing(self) -> None:
        assign_gitlab_reviewer(_payload(url="https://gitlab.example.com/x/y/-/merge_requests/1"))

    def test_logs_when_overlay_loader_raises(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from teatree.core import overlay_loader  # noqa: PLC0415

        def _raise(name: str | None = None) -> object:
            msg = "no overlay"
            raise RuntimeError(msg)

        monkeypatch.setattr(overlay_loader, "get_overlay", _raise)
        with caplog.at_level(logging.ERROR, logger="teatree.loop.mechanical"):
            assign_gitlab_reviewer(_payload(url="https://x/-/merge_requests/9", reviewer_username="bob"))

        assert any("Could not resolve code host" in r.message for r in caplog.records)

    def test_logs_when_host_is_none(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from teatree.backends import loader  # noqa: PLC0415
        from teatree.core import overlay_loader  # noqa: PLC0415

        monkeypatch.setattr(overlay_loader, "get_overlay", lambda name=None: object())
        monkeypatch.setattr(loader, "get_code_host", lambda overlay: None)
        with caplog.at_level(logging.INFO, logger="teatree.loop.mechanical"):
            assign_gitlab_reviewer(_payload(url="https://x/-/merge_requests/9", reviewer_username="bob"))

        assert any("No code host resolved" in r.message for r in caplog.records)

    def test_logs_when_host_has_no_assign_reviewer(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from teatree.backends import loader  # noqa: PLC0415
        from teatree.core import overlay_loader  # noqa: PLC0415

        class _PlainHost:  # no assign_reviewer attr
            pass

        monkeypatch.setattr(overlay_loader, "get_overlay", lambda name=None: object())
        monkeypatch.setattr(loader, "get_code_host", lambda overlay: _PlainHost())
        with caplog.at_level(logging.INFO, logger="teatree.loop.mechanical"):
            assign_gitlab_reviewer(_payload(url="https://x/-/merge_requests/9", reviewer_username="bob"))

        assert any("no assign_reviewer support" in r.message for r in caplog.records)

    def test_logs_when_assign_reviewer_raises(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from teatree.backends import loader  # noqa: PLC0415
        from teatree.core import overlay_loader  # noqa: PLC0415

        class _RaisingHost:
            def assign_reviewer(self, *, pr_url: str, username: str) -> bool:
                msg = "API exploded"
                raise RuntimeError(msg)

        monkeypatch.setattr(overlay_loader, "get_overlay", lambda name=None: object())
        monkeypatch.setattr(loader, "get_code_host", lambda overlay: _RaisingHost())
        with caplog.at_level(logging.ERROR, logger="teatree.loop.mechanical"):
            assign_gitlab_reviewer(_payload(url="https://x/-/merge_requests/9", reviewer_username="bob"))

        assert any("Failed to assign" in r.message for r in caplog.records)

    def test_success_logs_info(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from teatree.backends import loader  # noqa: PLC0415
        from teatree.core import overlay_loader  # noqa: PLC0415

        class _Host:
            def assign_reviewer(self, *, pr_url: str, username: str) -> bool:
                return True

        monkeypatch.setattr(overlay_loader, "get_overlay", lambda name=None: object())
        monkeypatch.setattr(loader, "get_code_host", lambda overlay: _Host())
        with caplog.at_level(logging.INFO, logger="teatree.loop.mechanical"):
            assign_gitlab_reviewer(_payload(url="https://x/-/merge_requests/42", reviewer_username="alice"))

        assert any("Assigned alice as reviewer" in r.message for r in caplog.records)

    def test_warning_logged_when_assign_returns_false(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from teatree.backends import loader  # noqa: PLC0415
        from teatree.core import overlay_loader  # noqa: PLC0415

        class _RefusingHost:
            def assign_reviewer(self, *, pr_url: str, username: str) -> bool:
                return False

        monkeypatch.setattr(overlay_loader, "get_overlay", lambda name=None: object())
        monkeypatch.setattr(loader, "get_code_host", lambda overlay: _RefusingHost())
        with caplog.at_level(logging.WARNING, logger="teatree.loop.mechanical"):
            assign_gitlab_reviewer(_payload(url="https://x/-/merge_requests/43", reviewer_username="bob"))

        assert any("returned False" in r.message for r in caplog.records)


class TestHandlersRegistry:
    def test_registry_maps_kind_to_handler(self) -> None:
        assert HANDLERS["ticket_disposition"] is ignore_disposed_ticket
        assert HANDLERS["ticket_completion"] is complete_ticket
        assert HANDLERS["ticket_reopen"] is reopen_ticket
        assert HANDLERS["reviewer_task_orphaned"] is reviewer_task_orphaned
        assert HANDLERS["assign_gitlab_reviewer"] is assign_gitlab_reviewer
