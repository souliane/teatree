import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase, override_settings

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core.intake.attachment_manifest import AttachmentKind, AttachmentRef, local_path_for
from teatree.core.models import AttachmentManifest, Session, Task, TaskAttempt, Ticket
from teatree.core.runners.base import RunnerResult
from teatree.core.tasks import (
    drain_headless_queue,
    drain_headless_queue_body,
    enqueue_teardown_for_terminal_tickets,
    execute_provision,
    execute_retrospect,
    execute_ship,
    execute_teardown,
    refresh_followup_snapshot,
    sync_followup,
)
from tests.teatree_core.conftest import CommandOverlay

IMMEDIATE_BACKEND = {
    "TASKS": {
        "default": {
            "BACKEND": "django_tasks.backends.immediate.ImmediateBackend",
        },
    },
}

_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestRefreshFollowupSnapshot(TestCase):
    @override_settings(**IMMEDIATE_BACKEND)
    def test_reports_current_counts(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        Task.objects.create(ticket=ticket, session=session)

        result = refresh_followup_snapshot.enqueue()

        assert result.return_value == {"tickets": 1, "tasks": 1, "open_tasks": 1}


class TestSyncFollowup(TestCase):
    @pytest.fixture(autouse=True)
    def _setup_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch

    @override_settings(**IMMEDIATE_BACKEND)
    def test_returns_error_without_token(self) -> None:
        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            result = sync_followup.enqueue()

        errors = result.return_value["errors"]
        assert len(errors) == 1
        assert "No code host token for" in errors[0]


def _stub_headless_runner(testcase: TestCase) -> None:
    """Stub the registered headless runner for the duration of *testcase*.

    The drain/dispatch tests run under ``IMMEDIATE_BACKEND``, so a
    ``execute_headless_task.enqueue(...)`` runs the worker *synchronously*,
    which — with ``claude`` on the dev host's PATH — would drive the REAL
    ``run_headless`` → ``_drive_with_heartbeat``. That path samples usage in
    an ``asyncio.to_thread`` worker whose connection, under ``TestCase``'s
    shared in-memory SQLite, the test harness keeps alive — surfacing as an
    order-dependent ``unclosed database`` ``ResourceWarning`` on GC (the
    flake this isolates). These tests only assert the *dispatch decision*, not
    agent execution, so the runner is stubbed to a recorded attempt — the same
    "don't run a real threaded DB read under TestCase" isolation
    ``tests/teatree_agents/_sdk_fake.py`` documents.
    """
    from unittest.mock import patch  # noqa: PLC0415

    from teatree.core import headless_dispatch  # noqa: PLC0415

    def _runner(task: Task, *, phase: str = "", overlay_skill_metadata: object = None) -> TaskAttempt:
        return task.complete_with_attempt(exit_code=0, result={"summary": "stubbed"})

    patcher = patch.object(headless_dispatch, "_runner", _runner)
    patcher.start()
    testcase.addCleanup(patcher.stop)


class TestDrainHeadlessQueue(TestCase):
    """Drain is a safety net for tasks that missed the post_save auto-enqueue."""

    def setUp(self) -> None:
        from django.db.models.signals import post_save  # noqa: PLC0415

        from teatree.core.signals import _auto_enqueue_headless_task  # noqa: PLC0415

        post_save.disconnect(_auto_enqueue_headless_task, sender=Task, dispatch_uid="auto_enqueue_headless")
        self.addCleanup(
            post_save.connect,
            _auto_enqueue_headless_task,
            sender=Task,
            dispatch_uid="auto_enqueue_headless",
        )
        _stub_headless_runner(self)

    @override_settings(**IMMEDIATE_BACKEND)
    def test_enqueues_pending_headless_tasks(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        # ``architectural_review`` has no registered phase agent, so it is NOT
        # loop-dispatched and the drain safety-net owns it.
        pending = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
            phase="architectural_review",
        )
        # Interactive task should NOT be enqueued
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.PENDING,
            phase="testing",
        )
        # A loop-dispatched author phase (coding) is the loop's sole
        # responsibility — the drain must NOT also enqueue it (double-dispatch).
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
            phase="coding",
        )

        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            result = drain_headless_queue.enqueue()

        assert result.return_value == {"enqueued": [pending.pk], "rerouted": [], "failed_unknown_overlay": []}

    @override_settings(**IMMEDIATE_BACKEND)
    def test_skips_when_no_pending_tasks(self) -> None:
        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            result = drain_headless_queue.enqueue()

        assert result.return_value == {"enqueued": [], "rerouted": [], "failed_unknown_overlay": []}

    @override_settings(**IMMEDIATE_BACKEND)
    def test_body_returns_empty_result_on_empty_queue(self) -> None:
        # The shared body the @task wrapper and the maintenance chain both call.
        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            result = drain_headless_queue_body()

        assert result == {"enqueued": [], "rerouted": [], "failed_unknown_overlay": []}

    @override_settings(**IMMEDIATE_BACKEND)
    def test_unknown_overlay_task_is_failed_not_enqueued(self) -> None:
        ticket = Ticket.objects.create(overlay="ghost-overlay")
        session = Session.objects.create(ticket=ticket, overlay="ghost-overlay")
        poison = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
            phase="architectural_review",
        )

        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            result = drain_headless_queue.enqueue()

        assert result.return_value == {"enqueued": [], "rerouted": [], "failed_unknown_overlay": [poison.pk]}
        poison.refresh_from_db()
        assert poison.status == Task.Status.FAILED

    @override_settings(**IMMEDIATE_BACKEND)
    def test_stale_interactive_row_is_rerouted_and_dispatched_under_headless_runtime(self) -> None:
        # A phase task created during the laptop /loop era: a loop-dispatched
        # (author, coding) pair routed INTERACTIVE, orphaned once the box moved
        # to the headless lane with no interactive session to dispatch it. Under
        # agent_runtime=headless the drain adopts it: route_to_headless + dispatch.
        from teatree.config import AgentRuntime  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket, overlay="test")
        stale = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.PENDING,
            phase="coding",
        )

        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.config.get_effective_settings") as mock_settings,
        ):
            mock_settings.return_value.agent_runtime = AgentRuntime.HEADLESS
            result = drain_headless_queue.enqueue()

        assert result.return_value == {"enqueued": [stale.pk], "rerouted": [stale.pk], "failed_unknown_overlay": []}
        stale.refresh_from_db()
        assert stale.execution_target == Task.ExecutionTarget.HEADLESS

    @override_settings(**IMMEDIATE_BACKEND)
    def test_interactive_row_is_left_untouched_under_interactive_runtime(self) -> None:
        # Under the default interactive runtime the /loop slot owns interactive
        # rows — the drain must not adopt them (that would double-dispatch).
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket, overlay="test")
        interactive = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.PENDING,
            phase="coding",
        )

        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            result = drain_headless_queue.enqueue()

        assert result.return_value == {"enqueued": [], "rerouted": [], "failed_unknown_overlay": []}
        interactive.refresh_from_db()
        assert interactive.execution_target == Task.ExecutionTarget.INTERACTIVE


class TestExecuteHeadlessUnknownOverlay(TestCase):
    """A task on an unknown overlay fails permanently — never an eternal re-crash (#1959)."""

    def setUp(self) -> None:
        from django.db.models.signals import post_save  # noqa: PLC0415

        from teatree.core.signals import _auto_enqueue_headless_task  # noqa: PLC0415

        post_save.disconnect(_auto_enqueue_headless_task, sender=Task, dispatch_uid="auto_enqueue_headless")
        self.addCleanup(
            post_save.connect,
            _auto_enqueue_headless_task,
            sender=Task,
            dispatch_uid="auto_enqueue_headless",
        )

    @override_settings(**IMMEDIATE_BACKEND)
    def test_unknown_overlay_marks_task_failed_with_attempt(self) -> None:
        from teatree.core.tasks import execute_headless_task  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="ghost-overlay")
        session = Session.objects.create(ticket=ticket, overlay="ghost-overlay")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
            phase="architectural_review",
        )

        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            result = execute_headless_task.func(task.pk, task.phase)

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert result["exit_code"] == 1
        attempt = TaskAttempt.objects.get(task=task)
        assert attempt.exit_code == 1
        assert "ghost-overlay" in attempt.error

    @override_settings(**IMMEDIATE_BACKEND)
    def test_headless_subprocess_stderr_is_recorded_on_the_attempt(self) -> None:
        # A claude-agent-sdk ``ProcessError`` stringifies to "Check stderr output
        # for details" and hides the real cause on ``.stderr``. The recorded
        # attempt must carry that stderr so a headless failure is diagnosable.
        from teatree.core import headless_dispatch as headless_dispatch_mod  # noqa: PLC0415
        from teatree.core.tasks import execute_headless_task  # noqa: PLC0415

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
            phase="architectural_review",
        )

        class _FakeProcessError(RuntimeError):
            def __init__(self) -> None:
                super().__init__("Command failed with exit code 1. Check stderr output for details")
                self.stderr = "claude: error: OAuth token expired -- run `claude login`"

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise _FakeProcessError

        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(headless_dispatch_mod, "loop_dispatch_refusal", return_value=None),
            patch.object(headless_dispatch_mod, "get_headless_runner", return_value=_boom),
            pytest.raises(_FakeProcessError),
        ):
            execute_headless_task.func(task.pk, task.phase)

        attempt = TaskAttempt.objects.get(task=task)
        assert attempt.exit_code == 1
        assert "claude subprocess stderr" in attempt.error
        assert "OAuth token expired" in attempt.error

    @override_settings(**IMMEDIATE_BACKEND)
    def test_failed_unknown_overlay_task_is_not_re_enqueued_next_drain(self) -> None:
        from teatree.core.tasks import execute_headless_task  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="ghost-overlay")
        session = Session.objects.create(ticket=ticket, overlay="ghost-overlay")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
            phase="architectural_review",
        )

        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            execute_headless_task.func(task.pk, task.phase)
            result = drain_headless_queue.enqueue()

        assert task.pk not in result.return_value["enqueued"]


class TestExecuteRetrospect(TestCase):
    @staticmethod
    def _ticket_in_merged() -> Ticket:
        ticket = Ticket.objects.create(overlay="test")
        ticket.state = Ticket.State.MERGED
        ticket.save(update_fields=["state"])
        return ticket

    @override_settings(**IMMEDIATE_BACKEND)
    def test_advances_merged_ticket_to_delivered(self) -> None:
        ticket = self._ticket_in_merged()
        ticket.state = Ticket.State.RETROSPECTED
        ticket.save(update_fields=["state"])

        result = execute_retrospect.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED
        assert ticket.extra.get("retro_scheduled") is True
        assert result.return_value == {"ticket_id": ticket.pk, "ok": True, "detail": "retro-scheduled"}

    @override_settings(**IMMEDIATE_BACKEND)
    def test_skips_when_state_does_not_match(self) -> None:
        """At-least-once delivery: redelivered jobs must be no-ops."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.DELIVERED)

        result = execute_retrospect.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED
        assert result.return_value == {
            "ticket_id": ticket.pk,
            "skipped": True,
            "state": "delivered",
        }


class TestExecuteTeardown(TestCase):
    def _ticket_in_merged(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test")
        ticket.state = Ticket.State.MERGED
        ticket.save(update_fields=["state"])
        return ticket

    @override_settings(**IMMEDIATE_BACKEND)
    def test_advances_runner_when_state_matches(self) -> None:
        from teatree.core.runners.base import RunnerResult  # noqa: PLC0415

        ticket = self._ticket_in_merged()

        with patch("teatree.core.tasks.WorktreeTeardown") as teardown:
            teardown.return_value.run.return_value = RunnerResult(ok=True, detail="tore down 2 worktree(s)")
            result = execute_teardown.enqueue(ticket.pk)

        ticket.refresh_from_db()
        # Teardown does NOT advance the FSM — retrospect() does that explicitly.
        assert ticket.state == Ticket.State.MERGED
        assert result.return_value == {
            "ticket_id": ticket.pk,
            "ok": True,
            "detail": "tore down 2 worktree(s)",
        }

    @override_settings(**IMMEDIATE_BACKEND)
    def test_skips_when_state_is_not_terminal(self) -> None:
        # A non-terminal ticket (IN_REVIEW — PR still open) is never torn down:
        # the guard is the done set (MERGED/DELIVERED/IGNORED), SHIPPED excluded.
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)

        result = execute_teardown.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert result.return_value == {
            "ticket_id": ticket.pk,
            "skipped": True,
            "state": "in_review",
        }

    @override_settings(**IMMEDIATE_BACKEND)
    def test_does_not_skip_delivered_or_ignored(self) -> None:
        # The guard relax (#): DELIVERED and IGNORED are terminal, so teardown
        # runs (does not skip) — previously only MERGED passed the guard.
        from teatree.core.runners.base import RunnerResult  # noqa: PLC0415

        for state in (Ticket.State.DELIVERED, Ticket.State.IGNORED):
            ticket = Ticket.objects.create(overlay="test")
            ticket.state = state
            ticket.save(update_fields=["state"])

            with patch("teatree.core.tasks.WorktreeTeardown") as teardown:
                teardown.return_value.run.return_value = RunnerResult(ok=True, detail="tore down 1 worktree(s)")
                result = execute_teardown.enqueue(ticket.pk)

            assert result.return_value == {
                "ticket_id": ticket.pk,
                "ok": True,
                "detail": "tore down 1 worktree(s)",
            }, state

    @override_settings(**IMMEDIATE_BACKEND)
    def test_reports_failure_without_advancing(self) -> None:
        from teatree.core.runners.base import RunnerResult  # noqa: PLC0415

        ticket = self._ticket_in_merged()

        with patch("teatree.core.tasks.WorktreeTeardown") as teardown:
            teardown.return_value.run.return_value = RunnerResult(ok=False, detail="repo-0: branch ahead")
            result = execute_teardown.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert result.return_value == {
            "ticket_id": ticket.pk,
            "ok": False,
            "detail": "repo-0: branch ahead",
        }


class TestEnqueueTeardownBacklogDrain(TestCase):
    """The one-shot operational drain enqueues teardown for terminal tickets holding worktrees."""

    def _terminal_ticket_with_worktree(self, state: Ticket.State) -> Ticket:
        from teatree.core.models import Worktree  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test")
        ticket.state = state
        ticket.save(update_fields=["state"])
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="r", branch="b", extra={"worktree_path": "/tmp/wt"}
        )
        return ticket

    @override_settings(**IMMEDIATE_BACKEND)
    def test_enqueues_only_terminal_tickets_with_worktrees(self) -> None:
        merged = self._terminal_ticket_with_worktree(Ticket.State.MERGED)
        ignored = self._terminal_ticket_with_worktree(Ticket.State.IGNORED)
        # A terminal ticket with NO worktree: nothing to reap.
        Ticket.objects.create(overlay="test", state=Ticket.State.DELIVERED)
        # A non-terminal ticket with a worktree: not eligible.
        non_terminal = self._terminal_ticket_with_worktree(Ticket.State.IN_REVIEW)

        with patch("teatree.core.tasks.execute_teardown") as teardown:
            enqueued = enqueue_teardown_for_terminal_tickets()

        assert sorted(enqueued) == sorted([merged.pk, ignored.pk])
        assert non_terminal.pk not in enqueued
        assert teardown.enqueue.call_count == 2


def _run_git(*args: str, cwd: Path) -> None:
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    git = shutil.which("git") or "git"
    subprocess.run([git, "-C", str(cwd), *args], check=True, capture_output=True)


class TestExecuteTeardownTerminalPurge(TestCase):
    """The FSM-automatic teardown runs for non-MERGED terminal states, against real git.

    Proves the guard relax end-to-end: ``execute_teardown`` purges an IGNORED
    ticket's clean, fully-pushed worktree (previously it skipped anything not
    MERGED), and the ``fsm_terminal=True`` carve-out means a stale never-ended
    Session no longer pins the worktree ACTIVE forever. The #706 data-loss guard
    still holds: a branch with unpushed-unique commits is KEPT on the new path.
    """

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.remote = tmp_path / "remote.git"
        self.remote.mkdir()
        _run_git("init", "-q", "--bare", "-b", "main", cwd=self.remote)

        self.repo_main = self.workspace / "myrepo"
        self.repo_main.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("remote", "add", "origin", str(self.remote), cwd=self.repo_main)
        _run_git("commit", "--allow-empty", "-q", "-m", "initial", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

        self.branch = "ac-myrepo-purge-x"
        self.wt_path = self.workspace / self.branch / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.branch, str(self.wt_path), cwd=self.repo_main)

    def _commit_in_worktree(self, message: str) -> None:
        (self.wt_path / "file.txt").write_text(message, encoding="utf-8")
        _run_git("add", "file.txt", cwd=self.wt_path)
        _run_git("config", "user.email", "t@t", cwd=self.wt_path)
        _run_git("config", "user.name", "t", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", message, cwd=self.wt_path)

    def _ignored_ticket_with_worktree(self) -> Ticket:
        from teatree.core.models import Worktree  # noqa: PLC0415

        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/purge",
            state=Ticket.State.IGNORED,
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="myrepo",
            branch=self.branch,
            extra={"worktree_path": str(self.wt_path)},
        )
        return ticket

    @contextmanager
    def _patched_clone_root(self) -> Iterator[None]:
        with ExitStack() as stack:
            stack.enter_context(patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY))
            for target in (
                "teatree.core.runners.teardown.clone_root",
                "teatree.core.worktree.worktree_done.clone_root",
                "teatree.core.cleanup.cleanup.clone_root",
            ):
                stack.enter_context(patch(target, return_value=self.workspace))
            mock_overlay = stack.enter_context(patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree"))
            mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
            yield

    @override_settings(**IMMEDIATE_BACKEND)
    def test_purges_clean_worktree_of_ignored_ticket(self) -> None:
        from teatree.core.models import Worktree  # noqa: PLC0415

        ticket = self._ignored_ticket_with_worktree()
        # Fully push the branch so nothing is at risk.
        self._commit_in_worktree("pushed work")
        _run_git("push", "-q", "origin", self.branch, cwd=self.wt_path)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)
        # A stale, never-ended Session pins the ticket ACTIVE on the ad-hoc path.
        Session.objects.create(overlay="test", ticket=ticket)

        with self._patched_clone_root():
            result = execute_teardown.enqueue(ticket.pk)

        assert result.return_value["ok"] is True, result.return_value["detail"]
        assert not self.wt_path.exists(), "clean pushed worktree of an IGNORED ticket was not purged"
        assert not Worktree.objects.filter(branch=self.branch).exists()

    @override_settings(**IMMEDIATE_BACKEND)
    def test_keeps_unpushed_unique_commits_on_terminal_purge(self) -> None:
        from teatree.core.models import Worktree  # noqa: PLC0415

        ticket = self._ignored_ticket_with_worktree()
        # A commit on NO remote, not patch-id-equivalent to origin/main.
        self._commit_in_worktree("genuinely unsynced work")

        with self._patched_clone_root():
            result = execute_teardown.enqueue(ticket.pk)

        assert result.return_value["ok"] is False
        assert self.branch in result.return_value["detail"]
        assert "salvage" in result.return_value["detail"]
        assert self.wt_path.exists(), "#706 guard breached: worktree with unpushed commits was destroyed"
        assert Worktree.objects.filter(branch=self.branch).exists()

    @override_settings(**IMMEDIATE_BACKEND)
    def test_keeps_uncommitted_changes_on_terminal_purge(self) -> None:
        from teatree.core.models import Worktree  # noqa: PLC0415

        ticket = self._ignored_ticket_with_worktree()
        # Every commit is pushed, so the unpushed-commit branch of the guard is
        # clean — the ONLY thing at risk is a real uncommitted working-tree change,
        # which is never on any remote. It must gate the purge on its own.
        self._commit_in_worktree("pushed work")
        _run_git("push", "-q", "origin", self.branch, cwd=self.wt_path)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)
        (self.wt_path / "uncommitted.txt").write_text("local edit not on any remote", encoding="utf-8")

        with self._patched_clone_root():
            result = execute_teardown.enqueue(ticket.pk)

        assert result.return_value["ok"] is False
        assert "uncommitted" in result.return_value["detail"]
        assert "salvage" in result.return_value["detail"]
        assert self.wt_path.exists(), "#706 guard breached: worktree with uncommitted changes was destroyed"
        assert Worktree.objects.filter(branch=self.branch).exists()


class TestExecuteProvision(TestCase):
    def _ticket_in_started(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", repos=["repo-a"], extra={"branch": "ac-repo-a-1-x"})
        ticket.state = Ticket.State.STARTED
        ticket.save(update_fields=["state"])
        return ticket

    @override_settings(**IMMEDIATE_BACKEND)
    def test_provisions_then_schedules_planning(self) -> None:
        from teatree.core.runners.base import RunnerResult  # noqa: PLC0415

        ticket = self._ticket_in_started()

        with patch("teatree.core.tasks.WorktreeProvisioner") as provisioner:
            provisioner.return_value.run.return_value = RunnerResult(ok=True, detail="provisioned 1 worktree(s)")
            result = execute_provision.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        assert ticket.tasks.filter(phase="planning").exists()
        assert not ticket.tasks.filter(phase="coding").exists()
        assert result.return_value == {
            "ticket_id": ticket.pk,
            "ok": True,
            "detail": "provisioned 1 worktree(s)",
        }

    @override_settings(**IMMEDIATE_BACKEND)
    def test_persists_landscape_artifact_before_scheduling_planning(self) -> None:
        # #2541: the intake FSM worker bakes the landscape survey into a durable
        # artifact the planner consumes — the survey is PRODUCED by intake, not
        # re-derived by the planner. Revert the persistence and this drops to 0
        # artifacts (the anti-vacuity proof).
        from teatree.core.models import LandscapeArtifact  # noqa: PLC0415
        from teatree.core.runners.base import RunnerResult  # noqa: PLC0415

        ticket = self._ticket_in_started()
        survey = {"worktrees": [], "open_prs": [{"url": "https://forge/pr/9"}], "recommendations": [], "warnings": []}

        with (
            patch("teatree.core.tasks.WorktreeProvisioner") as provisioner,
            patch("teatree.core.tasks.run_landscape", return_value=survey) as gather,
        ):
            provisioner.return_value.run.return_value = RunnerResult(ok=True, detail="provisioned 1 worktree(s)")
            execute_provision.enqueue(ticket.pk)

        gather.assert_called_once()
        artifacts = list(LandscapeArtifact.objects.filter(ticket=ticket))
        assert len(artifacts) == 1
        assert artifacts[0].survey == survey
        # The planner task exists too — persistence happens before scheduling.
        assert ticket.tasks.filter(phase="planning").exists()

    @override_settings(**IMMEDIATE_BACKEND)
    def test_survey_gather_failure_never_blocks_provisioning_or_planning(self) -> None:
        # The survey is best-effort context, not a gate: a gather that raises
        # (a forge outage, a corrupt clone) must not abort provisioning or
        # planning — fail-open, mirroring the landscape module's own doctrine.
        from teatree.core.models import LandscapeArtifact  # noqa: PLC0415
        from teatree.core.runners.base import RunnerResult  # noqa: PLC0415

        ticket = self._ticket_in_started()

        with (
            patch("teatree.core.tasks.WorktreeProvisioner") as provisioner,
            patch("teatree.core.tasks.run_landscape", side_effect=RuntimeError("forge down")),
        ):
            provisioner.return_value.run.return_value = RunnerResult(ok=True, detail="provisioned 1 worktree(s)")
            result = execute_provision.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        assert not LandscapeArtifact.objects.filter(ticket=ticket).exists()
        assert ticket.tasks.filter(phase="planning").exists()
        assert result.return_value == {
            "ticket_id": ticket.pk,
            "ok": True,
            "detail": "provisioned 1 worktree(s)",
        }

    @override_settings(**IMMEDIATE_BACKEND)
    def test_skips_planning_for_externally_delivered_ticket(self) -> None:
        # #2104: a hand-dispatched delivery agent claimed the unit (via
        # ``workspace ticket``). The provision worker must NOT auto-schedule the
        # planner the external owner will never claim — but provisioning itself
        # still succeeds.
        from teatree.core.models.external_delivery import mark_external_delivery  # noqa: PLC0415
        from teatree.core.runners.base import RunnerResult  # noqa: PLC0415

        ticket = self._ticket_in_started()
        mark_external_delivery(ticket)

        with patch("teatree.core.tasks.WorktreeProvisioner") as provisioner:
            provisioner.return_value.run.return_value = RunnerResult(ok=True, detail="provisioned 1 worktree(s)")
            result = execute_provision.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        assert not ticket.tasks.filter(phase="planning").exists()
        assert result.return_value == {
            "ticket_id": ticket.pk,
            "ok": True,
            "detail": "provisioned 1 worktree(s)",
        }

    @override_settings(**IMMEDIATE_BACKEND)
    def test_skips_planning_for_trivial_marked_ticket(self) -> None:
        # Batch C: a trivial-marked AUTHOR ticket skips the auto-planner exactly
        # as an externally-delivered one does — but provisioning still succeeds.
        from teatree.core.models.trivial_plan_skip import mark_trivial_plan_skip  # noqa: PLC0415
        from teatree.core.runners.base import RunnerResult  # noqa: PLC0415

        ticket = self._ticket_in_started()
        mark_trivial_plan_skip(ticket, reason="one-line constant bump", by="operator")

        with patch("teatree.core.tasks.WorktreeProvisioner") as provisioner:
            provisioner.return_value.run.return_value = RunnerResult(ok=True, detail="provisioned 1 worktree(s)")
            result = execute_provision.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        assert not ticket.tasks.filter(phase="planning").exists()
        assert result.return_value == {
            "ticket_id": ticket.pk,
            "ok": True,
            "detail": "provisioned 1 worktree(s)",
        }

    @override_settings(**IMMEDIATE_BACKEND)
    def test_holds_planning_while_attachment_unfetched(self) -> None:
        # PR-15/M5: the intake gate refuses to hand a ticket to the planner while
        # a referenced attachment is un-fetched. Remove the gate call in
        # execute_provision and the planning task appears anyway — the
        # anti-vacuity proof (this assertion goes RED without the gate wiring).
        ticket = self._ticket_in_started()
        attachment = "/uploads/" + "a" * 32 + "/spec.pdf"

        with (
            patch("teatree.core.tasks.WorktreeProvisioner") as provisioner,
            patch("teatree.core.tasks.ticket_text_sources", return_value=[f"spec {attachment}"]),
        ):
            provisioner.return_value.run.return_value = RunnerResult(ok=True, detail="provisioned 1 worktree(s)")
            result = execute_provision.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        assert not ticket.tasks.filter(phase="planning").exists()
        assert attachment in result.return_value["detail"]
        assert "--fetch" in result.return_value["detail"]
        # The gate recorded the manifest with the un-fetched entry.
        manifest = AttachmentManifest.latest_for(ticket)
        assert manifest is not None
        assert manifest.entries[0]["source_url"] == attachment

    @override_settings(**IMMEDIATE_BACKEND)
    def test_kill_switch_lifts_the_hold(self) -> None:
        # Never-lockout: `[teatree] attachment_gate_enabled = false` hands the
        # ticket off even with an un-fetched attachment, so a stuck ticket is
        # never a hard lockout.
        ticket = self._ticket_in_started()
        attachment = "/uploads/" + "a" * 32 + "/spec.pdf"

        with (
            patch("teatree.core.tasks.WorktreeProvisioner") as provisioner,
            patch("teatree.core.tasks.ticket_text_sources", return_value=[f"spec {attachment}"]),
            patch(
                "teatree.core.tasks.get_effective_settings",
                return_value=MagicMock(attachment_gate_enabled=False),
            ),
        ):
            provisioner.return_value.run.return_value = RunnerResult(ok=True, detail="provisioned 1 worktree(s)")
            execute_provision.enqueue(ticket.pk)

        assert ticket.tasks.filter(phase="planning").exists()

    @override_settings(**IMMEDIATE_BACKEND)
    def test_hands_off_once_attachment_is_fetched(self) -> None:
        # The gate is not permanently blocking: once the cached file exists the
        # planner is scheduled. Pairs with the hold test as the two-sided proof.
        ticket = self._ticket_in_started()
        attachment = "/uploads/" + "a" * 32 + "/spec.pdf"
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        cached = local_path_for(att_dir, AttachmentRef(attachment, AttachmentKind.GITLAB_UPLOAD))
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(b"fetched")

        with (
            patch("teatree.core.tasks.WorktreeProvisioner") as provisioner,
            patch("teatree.core.tasks.ticket_text_sources", return_value=[f"spec {attachment}"]),
            patch("teatree.core.tasks.attachments_dir_for", return_value=att_dir),
        ):
            provisioner.return_value.run.return_value = RunnerResult(ok=True, detail="provisioned 1 worktree(s)")
            execute_provision.enqueue(ticket.pk)

        assert ticket.tasks.filter(phase="planning").exists()

    @override_settings(**IMMEDIATE_BACKEND)
    def test_skips_when_state_does_not_match(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.SCOPED)

        result = execute_provision.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SCOPED
        assert not ticket.tasks.filter(phase="planning").exists()
        assert result.return_value == {
            "ticket_id": ticket.pk,
            "skipped": True,
            "state": "scoped",
        }

    @override_settings(**IMMEDIATE_BACKEND)
    def test_keeps_state_when_runner_fails(self) -> None:
        from teatree.core.runners.base import RunnerResult  # noqa: PLC0415

        ticket = self._ticket_in_started()

        with patch("teatree.core.tasks.WorktreeProvisioner") as provisioner:
            provisioner.return_value.run.return_value = RunnerResult(ok=False, detail="repo missing")
            result = execute_provision.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        assert not ticket.tasks.filter(phase="planning").exists()
        assert result.return_value == {"ticket_id": ticket.pk, "ok": False, "detail": "repo missing"}


class TestExecuteShip(TestCase):
    def _ticket_in_shipped(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test")
        ticket.state = Ticket.State.SHIPPED
        ticket.save(update_fields=["state"])
        return ticket

    @override_settings(**IMMEDIATE_BACKEND)
    def test_advances_shipped_ticket_to_in_review(self) -> None:
        from teatree.core.runners.base import RunnerResult  # noqa: PLC0415

        ticket = self._ticket_in_shipped()

        with patch("teatree.core.tasks.ShipExecutor") as ship_exec:
            ship_exec.return_value.run.return_value = RunnerResult(ok=True, detail="https://example.com/mr/1")
            result = execute_ship.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert result.return_value == {
            "ticket_id": ticket.pk,
            "ok": True,
            "detail": "https://example.com/mr/1",
        }

    @override_settings(**IMMEDIATE_BACKEND)
    def test_skips_when_state_does_not_match(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)

        result = execute_ship.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert result.return_value == {
            "ticket_id": ticket.pk,
            "skipped": True,
            "state": "merged",
        }

    @override_settings(**IMMEDIATE_BACKEND)
    def test_keeps_state_when_runner_fails(self) -> None:
        from teatree.core.runners.base import RunnerResult  # noqa: PLC0415

        ticket = self._ticket_in_shipped()

        with patch("teatree.core.tasks.ShipExecutor") as ship_exec:
            ship_exec.return_value.run.return_value = RunnerResult(ok=False, detail="push rejected")
            result = execute_ship.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert result.return_value == {"ticket_id": ticket.pk, "ok": False, "detail": "push rejected"}


class TestExecuteShipOrphanPrWindow(TestCase):
    """A forge PR opened by ``create_pr`` must never be stranded by a rollback.

    The live PR is an external side effect that no transaction rollback can
    undo. If the PR-url marker is committed only when the FSM-advance
    transaction commits, a failure after ``create_pr`` (e.g. the
    ``request_review`` guard raising) rolls back the marker but leaves the
    forge PR live — the ticket is stuck SHIPPED with an orphan PR, and a
    retry re-calls ``create_pr`` and hits a 409.
    """

    def _shipped_ticket_with_worktree(self) -> Ticket:
        from teatree.core.models import Worktree  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/77")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/repo",
            branch="feat-x",
            extra={"worktree_path": "/tmp/repo"},
        )
        ticket.state = Ticket.State.SHIPPED
        ticket.save(update_fields=["state"])
        return ticket

    @contextmanager
    def _ship_collaborators(self, host: object) -> Iterator[None]:
        with ExitStack() as stack:
            stack.enter_context(patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY))
            stack.enter_context(patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host))
            stack.enter_context(patch("teatree.core.runners.ship.git.push"))
            stack.enter_context(patch("teatree.core.runners.ship.git.branch_merged", return_value=False))
            stack.enter_context(patch("teatree.core.runners.ship.sha_conflicts_with_target", return_value=None))
            stack.enter_context(
                patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", "body"))
            )
            yield

    @override_settings(**IMMEDIATE_BACKEND)
    def test_pr_url_recorded_when_fsm_advance_raises_after_create_pr(self) -> None:
        ticket = self._shipped_ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/mr/1", "iid": 1}
        host.current_user.return_value = "souliane"

        with (
            self._ship_collaborators(host),
            patch.object(Ticket, "request_review", side_effect=RuntimeError("post-create_pr failure")),
            pytest.raises(RuntimeError, match="post-create_pr failure"),
        ):
            execute_ship.call(ticket.pk)

        ticket.refresh_from_db()
        # The forge PR is live; its URL MUST survive the FSM-advance rollback.
        assert ticket.extra.get("pr_urls") == ["https://example.com/mr/1"]
        assert host.create_pr.call_count == 1

    @override_settings(**IMMEDIATE_BACKEND)
    def test_retry_after_fsm_advance_failure_adopts_existing_pr(self) -> None:
        ticket = self._shipped_ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/mr/1", "iid": 1}
        host.current_user.return_value = "souliane"

        with (
            self._ship_collaborators(host),
            patch.object(Ticket, "request_review", side_effect=RuntimeError("post-create_pr failure")),
            pytest.raises(RuntimeError, match="post-create_pr failure"),
        ):
            execute_ship.call(ticket.pk)

        # Retry: create_pr must NOT be called again (the recorded URL is adopted),
        # so the forge never returns a 409 "PR already exists".
        with self._ship_collaborators(host):
            execute_ship.call(ticket.pk)

        ticket.refresh_from_db()
        assert host.create_pr.call_count == 1
        assert ticket.state == Ticket.State.IN_REVIEW


class TestExecuteHeadlessTask(TestCase):
    @pytest.fixture(autouse=True)
    def _setup_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch

    @override_settings(**IMMEDIATE_BACKEND)
    def test_records_failure_on_exception(self) -> None:
        """When run_headless raises, execute_headless_task marks the task as failed."""
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="agent-1")

        def _raise(*_args: object, **_kwargs: object) -> None:
            msg = "headless runtime crashed"
            raise RuntimeError(msg)

        self.monkeypatch.setattr("teatree.core.headless_dispatch._runner", _raise)

        # ``architectural_review`` has no registered phase agent, so it is NOT
        # loop-dispatched — it rides the auto-enqueue path the executor owns.
        # ImmediateBackend runs the enqueued job synchronously.
        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            task = Task.objects.create(ticket=ticket, session=session, phase="architectural_review")

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        attempt = TaskAttempt.objects.filter(task=task).first()
        assert attempt is not None
        assert attempt.exit_code == 1
        assert "headless runtime crashed" in attempt.error

    @override_settings(**IMMEDIATE_BACKEND)
    def test_resolves_ticket_overlay_with_multiple_installed(self) -> None:
        """The headless worker resolves the ticket's overlay, not the ambient default.

        Regression for souliane/teatree#1814: with two overlays registered a
        bare ``get_overlay()`` crashes the worker. ``execute_headless_task``
        must key off ``task.ticket.overlay`` instead.
        """
        ticket = Ticket.objects.create(overlay="beta")
        session = Session.objects.create(ticket=ticket, overlay="beta", agent_id="agent-2")

        captured: dict[str, object] = {}

        def _capture(task_obj: object, *, phase: str, overlay_skill_metadata: object) -> MagicMock:
            captured["metadata"] = overlay_skill_metadata
            return MagicMock(pk=1, exit_code=0, result={})

        self.monkeypatch.setattr("teatree.core.headless_dispatch._runner", _capture)

        beta_overlay = CommandOverlay()
        beta_metadata = beta_overlay.metadata.get_skill_metadata()
        registry = {"alpha": CommandOverlay(), "beta": beta_overlay}
        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=registry):
            Task.objects.create(ticket=ticket, session=session, phase="architectural_review")

        assert captured["metadata"] == beta_metadata


class TestAdvanceTicketNormalizesPhase(TestCase):
    """``Task._advance_ticket`` normalizes phase before the FSM compare (#750).

    Mirrors ``_record_phase_visit``. A task whose phase is a short verb
    (``review``/``code``/``test``/...)
    — the vocabulary skills emit and ``tasks create`` stores verbatim —
    records the phase visit on the session but, pre-fix, never advances
    the ticket FSM (``"review" == "reviewing"`` is False). Silent
    persistence/FSM desync.
    """

    def _ticket_with_session(self, state: Ticket.State) -> tuple[Ticket, Session]:
        ticket = Ticket.objects.create(overlay="test", state=state)
        session = Session.objects.create(ticket=ticket, agent_id="t")
        return ticket, session

    def test_short_verb_review_advances_tested_to_reviewed(self) -> None:
        ticket, session = self._ticket_with_session(Ticket.State.TESTED)
        # review()'s FSM condition requires a completed reviewing task to
        # exist (#694) — the real loop always has one. That condition's
        # own raw phase= filter (canonical-only) is a *separate* adjacent
        # bug filed as a follow-up; this test isolates the #750 scope
        # (_advance_ticket normalization) by satisfying the precondition
        # canonically, exactly as the loop's schedule_* chain does.
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            status=Task.Status.COMPLETED,
        )
        task = Task.objects.create(ticket=ticket, session=session, phase="review")

        task._advance_ticket()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED, (
            f"short-verb 'review' task did not advance FSM (state={ticket.state}); "
            "_advance_ticket compared raw self.phase instead of normalize_phase()"
        )
        # The session record agrees (already normalized by _record_phase_visit).
        assert "reviewing" in (session.visited_phases or [])

    def test_short_verb_code_advances_planned_to_coded(self) -> None:
        ticket, session = self._ticket_with_session(Ticket.State.PLANNED)
        task = Task.objects.create(ticket=ticket, session=session, phase="code")

        task._advance_ticket()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED

    def test_canonical_gerund_still_advances(self) -> None:
        # Regression guard: normalization must not break the already-correct
        # canonical-token path (the loop's auto-scheduled chain).
        ticket, session = self._ticket_with_session(Ticket.State.CODED)
        task = Task.objects.create(ticket=ticket, session=session, phase="testing")

        task._advance_ticket()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED


class TestReviewConditionNormalizesPhase(TestCase):
    """#757: the review FSM conditions match the canonical phase contract.

    review()/mark_reviewed_externally() must not use raw
    ``phase="reviewing"``. Distinct from #750's tests (which satisfy the precondition
    canonically to isolate _advance_ticket): here the ONLY completed
    reviewing task is the short-verb ``review`` one, so the condition
    itself must normalize or the transition is refused end-to-end.
    """

    def test_short_verb_review_task_alone_advances_tested_to_reviewed(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.TESTED)
        session = Session.objects.create(ticket=ticket, agent_id="t")
        # The sole reviewing task uses the short verb (unnormalized, as
        # `tasks create <id> review` stores it). Pre-fix, review()'s
        # condition `tasks.filter(phase="reviewing")` misses it.
        task = Task.objects.create(ticket=ticket, session=session, phase="review", status=Task.Status.COMPLETED)

        task._advance_ticket()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED, (
            f"short-verb 'review' task did not satisfy review()'s condition "
            f"(state={ticket.state}); the FSM condition compared raw phase"
        )

    def test_reviewer_role_short_verb_review_advances(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.TESTED, role=Ticket.Role.REVIEWER)
        session = Session.objects.create(ticket=ticket, agent_id="t")
        task = Task.objects.create(ticket=ticket, session=session, phase="review", status=Task.Status.COMPLETED)

        task._advance_ticket()

        ticket.refresh_from_db()
        # mark_reviewed_externally()'s condition must also normalize.
        assert ticket.state != Ticket.State.TESTED, (
            f"reviewer-role short-verb 'review' did not advance (state={ticket.state})"
        )

    def test_canonical_reviewing_task_still_satisfies_condition(self) -> None:
        # Regression guard: the canonical spelling must keep working.
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.TESTED)
        session = Session.objects.create(ticket=ticket, agent_id="t")
        task = Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.COMPLETED)

        task._advance_ticket()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED


class TestTaskCompletedInPhase(TestCase):
    """The shared queryset method both FSM conditions use (#757)."""

    def test_matches_both_short_verb_and_canonical(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.TESTED)
        session = Session.objects.create(ticket=ticket, agent_id="t")
        Task.objects.create(ticket=ticket, session=session, phase="review", status=Task.Status.COMPLETED)

        assert Task.objects.completed_in_phase("reviewing").filter(ticket=ticket).exists()
        # Symmetric: querying by the short verb also resolves.
        assert Task.objects.completed_in_phase("review").filter(ticket=ticket).exists()

    def test_excludes_non_completed_and_other_phases(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.TESTED)
        session = Session.objects.create(ticket=ticket, agent_id="t")
        Task.objects.create(ticket=ticket, session=session, phase="review", status=Task.Status.PENDING)
        Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.COMPLETED)

        assert not Task.objects.completed_in_phase("reviewing").filter(ticket=ticket).exists()


class TestTaskPendingInPhase(TestCase):
    """The shared consume-side queryset method (#769).

    Mirrors ``completed_in_phase`` (#757) on the opposite status set:
    non-terminal (PENDING/CLAIMED) tasks whose phase normalizes to the
    target, so ``_consume_pending_phase_tasks`` matches a short-verb
    ``review`` task the same as a canonical ``reviewing`` one.
    """

    def test_matches_both_short_verb_and_canonical(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.TESTED)
        session = Session.objects.create(ticket=ticket, agent_id="t")
        Task.objects.create(ticket=ticket, session=session, phase="review", status=Task.Status.PENDING)

        assert Task.objects.pending_in_phase("reviewing").filter(ticket=ticket).exists()
        assert Task.objects.pending_in_phase("review").filter(ticket=ticket).exists()

    def test_includes_claimed_excludes_terminal_and_other_phases(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.TESTED)
        session = Session.objects.create(ticket=ticket, agent_id="t")
        Task.objects.create(ticket=ticket, session=session, phase="review", status=Task.Status.CLAIMED)
        assert Task.objects.pending_in_phase("reviewing").filter(ticket=ticket).count() == 1

        Task.objects.create(ticket=ticket, session=session, phase="review", status=Task.Status.COMPLETED)
        Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.PENDING)
        # Still only the CLAIMED short-verb reviewing task — terminal and
        # other-phase rows are excluded.
        assert Task.objects.pending_in_phase("reviewing").filter(ticket=ticket).count() == 1


class TestConsumePendingPhaseTasksNormalizesPhase(TestCase):
    """#769: the consume side honours the canonical phase contract.

    Same root-cause class as #757 (raw phase compare), distinct code
    path: ``_consume_pending_phase_tasks`` (the consume side) vs the
    ``review()`` FSM *condition* (#757). The direct-CLI path leaves a
    short-verb ``review`` task PENDING; ``review()`` must consume it so
    it is not later picked up as a zombie session.
    """

    def test_direct_review_consumes_pending_short_verb_task(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.TESTED)
        session = Session.objects.create(ticket=ticket, agent_id="t")
        # Condition-satisfier: a COMPLETED reviewing task so review() is legal.
        Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.COMPLETED)
        # The zombie: a short-verb `review` task left PENDING (as the
        # direct-CLI path leaves it). Pre-fix, the raw `phase="reviewing"`
        # filter misses it and it survives as a zombie session.
        zombie = Task.objects.create(ticket=ticket, session=session, phase="review", status=Task.Status.PENDING)

        ticket.review()
        ticket.save()

        zombie.refresh_from_db()
        assert zombie.status == Task.Status.COMPLETED, (
            f"short-verb 'review' PENDING task was not consumed (status={zombie.status}); "
            f"_consume_pending_phase_tasks compared raw phase"
        )

    def test_canonical_pending_task_still_consumed(self) -> None:
        # Regression guard: the canonical spelling must keep being consumed.
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.TESTED)
        session = Session.objects.create(ticket=ticket, agent_id="t")
        Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.COMPLETED)
        zombie = Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.CLAIMED)

        ticket.review()
        ticket.save()

        zombie.refresh_from_db()
        assert zombie.status == Task.Status.COMPLETED
