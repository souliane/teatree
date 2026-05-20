"""Regression tests for the sub-agent return-contract evidence requirement (#1284).

Codex finding #1282-6 (medium): ``_record_success`` validates the result blob
only for ``additionalProperties: false``; nothing forces a phase-specific
evidence field. The headless agent can return ``{}`` (or a one-line summary)
and the task still records the phase visit + completes — the "DM sent
successfully but didn't deliver" false-positive class.

The fix is to refuse the success record when the result lacks the
phase-specific required evidence field(s), surface a structured error on the
attempt, and fail the task (it stays available for the agent to retry with
evidence). No phase visit is recorded on a no-evidence success.
"""

import contextlib
import json
import shlex
from collections.abc import Iterator
from unittest.mock import patch

from django.test import TestCase

import teatree.agents.headless as headless_mod
from teatree.agents.headless import run_headless
from teatree.core.models import Session, Task, Ticket


@contextlib.contextmanager
def _fake_claude(stdout: str = "", stderr: str = "", exit_code: int = 0) -> Iterator[None]:
    script = f"printf %s {shlex.quote(stdout)}; printf %s {shlex.quote(stderr)} >&2; exit {exit_code}"
    with (
        patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
        patch.object(headless_mod, "_build_headless_command", return_value=["sh", "-c", script]),
    ):
        yield


class TestEvidenceRequiredOnPhaseCompletion(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_coding_phase_refuses_success_with_no_files_modified(self) -> None:
        # Pre-fix: agent returns a one-line summary, task completes, phase
        # is recorded — even though the "coding" claim has no file evidence.
        # Post-fix: missing ``files_modified`` is rejected with a structured
        # error; the task does NOT complete and the phase is NOT recorded.
        bare_summary = json.dumps({"summary": "Done"})
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=self.ticket, session=session, phase="coding")

        with _fake_claude(stdout=f"{bare_summary}\n"):
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        session.refresh_from_db()
        assert attempt.exit_code != 0 or attempt.error, (
            f"a no-evidence coding result must fail the attempt; "
            f"got exit_code={attempt.exit_code} error={attempt.error!r}"
        )
        assert "files_modified" in attempt.error or "evidence" in attempt.error.lower(), (
            f"the attempt error must name the missing evidence field, got: {attempt.error!r}"
        )
        assert task.status == Task.Status.FAILED, (
            f"a no-evidence success must NOT complete the task; got status={task.status}"
        )
        assert "coding" not in (session.visited_phases or []), (
            f"phase must NOT be recorded on a no-evidence success; visited={session.visited_phases}"
        )

    def test_coding_phase_accepts_success_with_files_modified(self) -> None:
        # Sanity: the happy path keeps working when the agent supplies the
        # required evidence field.
        good = json.dumps(
            {
                "summary": "Implemented X",
                "files_modified": [{"path": "src/x.py", "action": "modified"}],
            },
        )
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=self.ticket, session=session, phase="coding")

        with _fake_claude(stdout=f"{good}\n"):
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        session.refresh_from_db()
        assert attempt.exit_code == 0
        assert not attempt.error
        assert task.status == Task.Status.COMPLETED
        assert "coding" in (session.visited_phases or [])
