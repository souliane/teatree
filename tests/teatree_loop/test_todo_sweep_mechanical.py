"""Tests for the ``todo_completion`` mechanical handler — re-check before complete (#129).

The handler RE-verifies the artifact's terminal state against the live code
host before advancing the FSM (fail-CLOSED: any uncertainty blocks the
irreversible completion). It is idempotent (already-terminal tasks no-op) and
best-effort (a missing task / host error never crashes the tick).

Real Task/Ticket/Session rows; only the code host + overlay resolution are
mocked (the network + entry-point externals).
"""

import os
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.overlay import OverlayBase
from teatree.loop.mechanical import todo_completion
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@dataclass
class _Host:
    issues_by_url: dict[str, RawAPIDict] = field(default_factory=dict)
    raise_on_fetch: bool = False

    def get_issue(self, issue_url: str) -> RawAPIDict:
        if self.raise_on_fetch:
            msg = "network down"
            raise RuntimeError(msg)
        return self.issues_by_url.get(issue_url, {"error": "not found"})


class _Overlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["acme-repo"]

    def get_provision_steps(self, worktree: Any) -> list:
        _ = worktree
        return []

    def is_issue_done(self, issue_data: dict[str, object]) -> bool:
        return issue_data.get("state") in {"closed", "completed", "merged"}


class _TodoCompletionHarness(TestCase):
    OVERLAY = "t3-acme"
    URL = "https://example.com/issues/100"

    def _task(self, *, status: str = Task.Status.PENDING, url: str | None = None) -> Task:
        issue_url = self.URL if url is None else url
        ticket = Ticket.objects.create(overlay=self.OVERLAY, issue_url=issue_url)
        session = Session.objects.create(overlay=self.OVERLAY, ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        if status != Task.Status.PENDING:
            Task.objects.filter(pk=task.pk).update(status=status)
            task.refresh_from_db()
        return task

    def _patch(self, host: _Host | None):
        return (
            patch("teatree.core.overlay_loader.get_overlay", return_value=_Overlay()),
            patch("teatree.backends.loader.get_code_host_for_url", return_value=host),
        )


class RecheckBeforeCompleteTests(_TodoCompletionHarness):
    def test_completes_task_when_artifact_still_terminal(self) -> None:
        task = self._task()
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        overlay_patch, host_patch = self._patch(host)
        with overlay_patch, host_patch:
            todo_completion({"task_id": task.pk, "issue_url": self.URL})
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_does_not_complete_when_artifact_reopened(self) -> None:
        """Artifact went back to open between scan and handler → never complete."""
        task = self._task()
        host = _Host(issues_by_url={self.URL: {"state": "open"}})
        overlay_patch, host_patch = self._patch(host)
        with overlay_patch, host_patch:
            todo_completion({"task_id": task.pk, "issue_url": self.URL})
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING, "a re-opened artifact must NOT auto-complete"

    def test_fetch_error_blocks_completion(self) -> None:
        task = self._task()
        host = _Host(raise_on_fetch=True)
        overlay_patch, host_patch = self._patch(host)
        with overlay_patch, host_patch:
            todo_completion({"task_id": task.pk, "issue_url": self.URL})
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING, "uncertainty fails CLOSED at the completion gate"

    def test_missing_host_blocks_completion(self) -> None:
        task = self._task()
        overlay_patch, host_patch = self._patch(None)
        with overlay_patch, host_patch:
            todo_completion({"task_id": task.pk, "issue_url": self.URL})
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_error_payload_blocks_completion(self) -> None:
        task = self._task()
        host = _Host(issues_by_url={self.URL: {"error": "404"}})
        overlay_patch, host_patch = self._patch(host)
        with overlay_patch, host_patch:
            todo_completion({"task_id": task.pk, "issue_url": self.URL})
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_empty_issue_url_blocks_completion(self) -> None:
        task = self._task(url="")
        host = _Host(issues_by_url={"": {"state": "closed"}})
        overlay_patch, host_patch = self._patch(host)
        with overlay_patch, host_patch:
            todo_completion({"task_id": task.pk, "issue_url": ""})
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING


class MultiOverlayResolutionTests(TestCase):
    """Real ``get_overlay()`` ambiguity path — two overlays registered (#1605).

    The handler must resolve the owning overlay from ``task.ticket.overlay``
    instead of calling bare ``get_overlay()``. With two overlays registered and
    no ``T3_OVERLAY_NAME``, a bare ``get_overlay()`` raises
    ``ImproperlyConfigured("Multiple overlays found ...")`` — swallowed by the
    fail-CLOSED guard, which then logs "artifact no longer terminal" and the
    task never completes. Nothing about overlay resolution is mocked here; only
    the code host (a network external) is.
    """

    OVERLAY = "t3-acme"
    URL = "https://example.com/issues/200"

    def _task(self) -> Task:
        ticket = Ticket.objects.create(overlay=self.OVERLAY, issue_url=self.URL)
        session = Session.objects.create(overlay=self.OVERLAY, ticket=ticket, agent_id="a")
        return Task.objects.create(ticket=ticket, session=session, phase="coding")

    def test_resolves_owning_overlay_and_completes_with_two_overlays(self) -> None:
        task = self._task()
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        overlays = {self.OVERLAY: _Overlay(), "t3-other": _Overlay()}
        env_without_pin = {k: v for k, v in os.environ.items() if k != "T3_OVERLAY_NAME"}
        with (
            patch.dict(os.environ, env_without_pin, clear=True),
            patch("teatree.core.overlay_loader._discover_overlays", return_value=overlays),
            patch("teatree.backends.loader.get_code_host_for_url", return_value=host),
        ):
            todo_completion({"task_id": task.pk, "issue_url": self.URL})
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED, (
            "with two overlays registered, the handler must resolve the owning overlay "
            "from ticket.overlay and complete the terminal task — not skip on ambiguity"
        )


class IdempotencyAndResilienceTests(_TodoCompletionHarness):
    def test_already_completed_task_no_ops(self) -> None:
        task = self._task(status=Task.Status.COMPLETED)
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        overlay_patch, host_patch = self._patch(host)
        with overlay_patch, host_patch:
            todo_completion({"task_id": task.pk, "issue_url": self.URL})  # must not raise
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_failed_task_no_ops(self) -> None:
        task = self._task(status=Task.Status.FAILED)
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        overlay_patch, host_patch = self._patch(host)
        with overlay_patch, host_patch:
            todo_completion({"task_id": task.pk, "issue_url": self.URL})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_missing_task_id_no_ops(self) -> None:
        todo_completion({})  # must not raise

    def test_unknown_task_id_no_ops(self) -> None:
        todo_completion({"task_id": 999999, "issue_url": self.URL})  # must not raise

    def test_overlay_resolution_failure_blocks_completion(self) -> None:
        task = self._task()
        with (
            patch("teatree.core.overlay_loader.get_overlay", side_effect=RuntimeError("no overlay")),
            patch("teatree.backends.loader.get_code_host_for_url", return_value=_Host()),
        ):
            todo_completion({"task_id": task.pk, "issue_url": self.URL})
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
