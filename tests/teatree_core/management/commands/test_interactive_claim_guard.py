"""Every in-session claim seam honours ``interactive_claim_refusal``.

Under ``agent_runtime=headless`` the headless factory owns loop-dispatched
phase work, so no interactive claim path may take it — and each refusal must
name what to run instead. Ordinary interactive work stays claimable.
"""

import io
import json
from typing import Any, cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ConfigSetting, Session, Task, Ticket

_HEADLESS_OWNED_PHASE = "answering"
_FREE_FORM_PHASE = "ad-hoc-phase"


class _ClaimSeamCase(TestCase):
    def _make_interactive_task(self, *, phase: str) -> Task:
        ticket = Ticket.objects.create(overlay="test", role="author")
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        task.route_to_interactive(reason="stale interactive row from before the runtime flip")
        return task


class TestTasksClaimSeam(_ClaimSeamCase):
    def test_headless_runtime_refuses_the_interactive_claim(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "headless")
        task = self._make_interactive_task(phase=_HEADLESS_OWNED_PHASE)

        err = io.StringIO()
        assert call_command("tasks", "claim", execution_target="interactive", stderr=err) is None

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING, "the refused task must be left unclaimed"
        assert "work-next-headless" in err.getvalue()

    def test_headless_runtime_still_allows_a_free_form_interactive_claim(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "headless")
        task = self._make_interactive_task(phase=_FREE_FORM_PHASE)

        assert call_command("tasks", "claim", execution_target="interactive") == task.pk

    def test_interactive_runtime_allows_the_claim(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "interactive")
        task = self._make_interactive_task(phase=_HEADLESS_OWNED_PHASE)

        assert call_command("tasks", "claim", execution_target="interactive") == task.pk


class TestTasksStartSeam(_ClaimSeamCase):
    """``tasks start <id>`` — the named-task arm, which bypasses the claimable queryset."""

    def test_headless_runtime_refuses_the_named_task(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "headless")
        task = self._make_interactive_task(phase=_HEADLESS_OWNED_PHASE)

        err = io.StringIO()
        with pytest.raises(SystemExit):
            call_command("tasks", "start", task.pk, stderr=err)

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING, "the refused task must be left unclaimed"
        assert "work-next-headless" in err.getvalue()


class TestLoopDispatchSeam(_ClaimSeamCase):
    @staticmethod
    def _emitted(*argv: str) -> list[dict[str, Any]]:
        out = io.StringIO()
        call_command("loop_dispatch", *argv, "--json", stdout=out)
        return cast("list[dict[str, Any]]", json.loads(out.getvalue()))

    def test_headless_runtime_hides_the_task_from_the_loop_slot(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "headless")
        task = self._make_interactive_task(phase=_HEADLESS_OWNED_PHASE)

        assert self._emitted("pending-spawn") == []
        assert self._emitted("claim-next") == []
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_interactive_runtime_offers_the_task_to_the_loop_slot(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "interactive")
        task = self._make_interactive_task(phase=_HEADLESS_OWNED_PHASE)

        pending = self._emitted("pending-spawn")
        assert [entry["task_id"] for entry in pending] == [task.pk]
