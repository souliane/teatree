import io
import json
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.agents.headless as headless_mod
import teatree.core.management.commands.tasks as tasks_cmd
import teatree.core.management.commands.tasks_session_view as session_view
import teatree.core.management.commands.worktree as worktree_cmd
import teatree.core.overlay_loader as overlay_loader_mod
import teatree.core.runners.worktree_provision as worktree_provision_mod
import teatree.utils.run as utils_run_mod
from teatree.core.models import Session, Task, TaskAttempt, Ticket, Worktree
from teatree.core.overlay import DbImportStrategy, OverlayBase, ProvisionStep, RunCommands
from tests.teatree_agents._sdk_fake import fake_sdk, success_stream

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class CommandOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        def remember_setup() -> None:
            facts = cast("dict[str, str]", worktree.extra or {})
            facts["setup_hook"] = "ran"
            worktree.extra = facts
            worktree.save(update_fields=["extra"])

        return [ProvisionStep(name="remember-setup", callable=remember_setup)]

    def get_run_commands(self, worktree: Worktree) -> RunCommands:
        return {
            "backend": ["run-backend", worktree.repo_path],
            "frontend": ["run-frontend", worktree.repo_path],
        }


_MOCK_OVERLAY = {"test": CommandOverlay()}

COMMAND_SETTINGS: dict[str, object] = {}


class TestLifecycleCommands(TestCase):
    @override_settings(**COMMAND_SETTINGS)
    def test_create_start_report_and_teardown_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_path = str(tmp_path / "test-worktree-backend")
            Path(wt_path).mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/55", variant="acme")
            wt = Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": wt_path},
            )

            mock_config = MagicMock()
            mock_config.user.workspace_dir = tmp_path

            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": wt_path}),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
                patch.object(utils_run_mod, "subprocess") as mock_sp,
                patch.object(worktree_cmd, "get_worktree_ports", return_value={"backend": 8001, "frontend": 4201}),
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                mock_sp.run.return_value = MagicMock(returncode=0)
                worktree_id = cast("int", call_command("worktree", "provision"))
                status = cast("dict[str, str]", call_command("worktree", "status"))
                # start no-ops with "no compose file" since CommandOverlay has none;
                # state advances to SERVICES_UP, teardown still works from any state
                call_command("worktree", "start")
                call_command("worktree", "teardown")

            assert worktree_id == wt.id
            assert status["state"] == Worktree.State.PROVISIONED
            assert status["repo_path"] == "/tmp/backend"
            # Teardown folds the old `clean` step — the row is deleted, not reset
            assert not Worktree.objects.filter(pk=worktree_id).exists()


class DbStrategyOverlay(CommandOverlay):
    """A CommandOverlay variant that declares a DB import strategy."""

    def get_db_import_strategy(self, worktree: Worktree) -> DbImportStrategy | None:
        return {"kind": "shared", "shared_postgres": True}


class TestHealMissingProvisionedDb(TestCase):
    """``worktree start`` re-provisions when a ``provisioned`` worktree's DB is gone (#1038).

    An interrupted provision can leave the FSM at PROVISIONED with ``db_name``
    set but no Postgres DB created — the start probe then dies with "database
    does not exist". The start command heals it by re-running the idempotent
    provision before advancing.
    """

    def _make_worktree(self, tmp: Path) -> tuple[Ticket, Worktree, str]:
        wt_path = str(tmp / "wt-backend")
        Path(wt_path).mkdir()
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1038")
        wt = Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature",
            db_name="wt_gone_db",
            extra={"worktree_path": wt_path},
            state=Worktree.State.PROVISIONED,
        )
        return ticket, wt, wt_path

    @override_settings(**COMMAND_SETTINGS)
    def test_start_reprovisions_when_db_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _ticket, _wt, wt_path = self._make_worktree(tmp_path)
            mock_config = MagicMock()
            mock_config.user.workspace_dir = tmp_path
            reprovision = MagicMock()
            reprovision.run.return_value = MagicMock(ok=True, detail="healed")
            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": wt_path}),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value={"test": DbStrategyOverlay()}),
                patch.object(
                    worktree_provision_mod, "WorktreeProvisionRunner", return_value=reprovision
                ) as mock_runner,
                patch("teatree.utils.db.db_exists", return_value=False),
                patch.object(worktree_cmd, "reap_stale_local_stacks"),
                patch.object(worktree_cmd, "acquire_or_enqueue", return_value=False),
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                call_command("worktree", "start")
            mock_runner.assert_called_once()
            reprovision.run.assert_called_once()

    @override_settings(**COMMAND_SETTINGS)
    def test_start_does_not_reprovision_when_db_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _ticket, _wt, wt_path = self._make_worktree(tmp_path)
            mock_config = MagicMock()
            mock_config.user.workspace_dir = tmp_path
            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": wt_path}),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value={"test": DbStrategyOverlay()}),
                patch.object(worktree_provision_mod, "WorktreeProvisionRunner") as mock_runner,
                patch("teatree.utils.db.db_exists", return_value=True),
                patch.object(worktree_cmd, "reap_stale_local_stacks"),
                patch.object(worktree_cmd, "acquire_or_enqueue", return_value=False),
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                call_command("worktree", "start")
            mock_runner.assert_not_called()


class TestProvisionTicketFlag(TestCase):
    """``worktree provision --ticket`` pins attribution to a named ticket.

    A manually-added git worktree (``git worktree add``, no ``workspace
    ticket``) has no Worktree row. Resolution would auto-register and could
    cross-attach to an unrelated workspace sibling. ``--ticket <number>``
    overrides the heuristic and binds the worktree to the named ticket.
    """

    @override_settings(**COMMAND_SETTINGS)
    def test_ticket_flag_pins_attribution_for_manual_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # A sibling worktree for an unrelated ticket under the same parent.
            sibling_ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/999")
            sibling_path = tmp_path / "sibling-backend"
            sibling_path.mkdir()
            (sibling_path / ".git").write_text("gitdir: /some/.git/worktrees/sibling-backend\n")
            Worktree.objects.create(
                ticket=sibling_ticket,
                overlay="test",
                repo_path="sibling-backend",
                branch="999-unrelated",
                extra={"worktree_path": str(sibling_path)},
            )

            target_ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/321")

            # The manual worktree: a git worktree marker, no Worktree row, on a
            # branch whose number does not name the target ticket — so only the
            # explicit --ticket flag can attribute it correctly.
            manual_path = tmp_path / "manual-backend"
            manual_path.mkdir()
            (manual_path / ".git").write_text("gitdir: /some/.git/worktrees/manual-backend\n")

            mock_config = MagicMock()
            mock_config.user.workspace_dir = tmp_path

            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": str(manual_path)}),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
                patch.object(utils_run_mod, "subprocess") as mock_sp,
                patch("teatree.core.resolve.git.current_branch", return_value="no-number-branch"),
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                mock_sp.run.return_value = MagicMock(returncode=0)
                worktree_id = cast("int", call_command("worktree", "provision", "--ticket", "321"))

            wt = Worktree.objects.get(pk=worktree_id)
            assert wt.ticket_id == target_ticket.pk
            assert wt.ticket_id != sibling_ticket.pk
            assert not Ticket.objects.filter(issue_url__startswith="auto:").exists()

    @override_settings(**COMMAND_SETTINGS)
    def test_ticket_flag_for_unknown_number_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manual_path = Path(tmp) / "manual-backend"
            manual_path.mkdir()
            (manual_path / ".git").write_text("gitdir: /some/.git/worktrees/manual-backend\n")

            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": str(manual_path)}),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
                pytest.raises(SystemExit),
            ):
                call_command("worktree", "provision", "--ticket", "404")

            assert not Worktree.objects.exists()


class TestTaskCommands(TestCase):
    @override_settings(**COMMAND_SETTINGS)
    def test_claim_and_complete_work(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="agent-1")
        sdk_task = Task.objects.create(ticket=ticket, session=session)
        sdk_followup_task = Task.objects.create(ticket=ticket, session=session)

        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            fake_sdk(success_stream({"summary": "done"}, session_id="test-session")),
        ):
            claimed_task_id = cast(
                "int",
                call_command("tasks", "claim", execution_target="headless", claimed_by="worker-1"),
            )
            sdk_result = cast(
                "dict[str, str]",
                call_command("tasks", "work-next-sdk", claimed_by="worker-1"),
            )
            refresh_summary = cast("dict[str, int]", call_command("followup", "refresh"))
            reminders = cast("list[int]", call_command("followup", "remind"))

        sdk_task.refresh_from_db()
        sdk_followup_task.refresh_from_db()

        assert claimed_task_id == sdk_task.id
        assert "exit_code" in sdk_result
        assert sdk_task.status == Task.Status.CLAIMED
        assert sdk_followup_task.status == Task.Status.COMPLETED
        assert TaskAttempt.objects.count() == 1
        assert refresh_summary == {"tickets": 1, "tasks": 2, "open_tasks": 1}
        assert reminders == []

    @override_settings(**COMMAND_SETTINGS)
    def test_return_none_when_no_work_available(self) -> None:
        assert call_command("tasks", "work-next-sdk", claimed_by="worker-1") is None

    @override_settings(LOOP_ALLOW_HEADLESS_DISPATCH=False)
    def test_work_next_sdk_refuses_loop_dispatched_phase(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session, phase="answering")
        # ``Task.save`` auto-routes a registered-phase HEADLESS insert to
        # INTERACTIVE; force HEADLESS via ``route_to_headless`` (an UPDATE,
        # not an insert) so the save default does not re-fire.
        task.route_to_headless(reason="forced headless for the regression")
        assert task.execution_target == Task.ExecutionTarget.HEADLESS

        with patch.object(headless_mod, "run_headless", MagicMock()) as run_headless_mock:
            sdk_result = cast(
                "dict[str, str]",
                call_command("tasks", "work-next-sdk", claimed_by="worker-1"),
            )

        run_headless_mock.assert_not_called()
        assert "routing_error" in sdk_result
        assert sdk_result["exit_code"] == "1"

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

        attempt = TaskAttempt.objects.get(task=task)
        assert attempt.exit_code == 1
        assert "routing_error" in attempt.result

    @override_settings(LOOP_ALLOW_HEADLESS_DISPATCH=True)
    def test_work_next_sdk_override_allows_loop_dispatched_phase(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session, phase="answering")
        task.route_to_headless(reason="forced headless for the regression")

        attempt = TaskAttempt(task=task, execution_target=task.execution_target, exit_code=0)
        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(headless_mod, "run_headless", MagicMock(return_value=attempt)) as run_headless_mock,
        ):
            call_command("tasks", "work-next-sdk", claimed_by="worker-1")

        run_headless_mock.assert_called_once()

    @override_settings(**COMMAND_SETTINGS)
    def test_work_next_sdk_records_durable_failure_when_runner_raises(self) -> None:
        # Under the no-fallback SDK cutover, ``work_next_sdk`` calls ``run_headless``
        # which may RAISE on an SDK client startup/query/response error. Without the
        # same failure-recording the Celery-style wrapper does, the task stays
        # silently CLAIMED until lease reap, then re-fires forever with NO durable
        # failed TaskAttempt — a real wedge/retry-loop. The command must record a
        # FAILED TaskAttempt carrying the error, FAIL the task (not leave it
        # claimed/cycling), and return a nonzero command result.
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session)

        boom = RuntimeError("SDK client failed to start")
        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(headless_mod, "run_headless", MagicMock(side_effect=boom)) as run_headless_mock,
        ):
            sdk_result = cast(
                "dict[str, str]",
                call_command("tasks", "work-next-sdk", claimed_by="worker-1"),
            )

        run_headless_mock.assert_called_once()
        # Nonzero command result surfaced to the caller.
        assert sdk_result["exit_code"] == "1"

        task.refresh_from_db()
        # The task is FAILED — not silently left CLAIMED to cycle on lease reap.
        assert task.status == Task.Status.FAILED

        # A durable failed TaskAttempt carrying the error was recorded.
        attempt = TaskAttempt.objects.get(task=task)
        assert attempt.exit_code == 1
        assert "SDK client failed to start" in attempt.error


class TestTasksListSession(TestCase):
    """``t3 <overlay> tasks list --session`` scopes to the current Claude session."""

    @override_settings(**COMMAND_SETTINGS)
    def test_scopes_rows_to_the_active_claude_session(self) -> None:
        with patch.dict("os.environ", {"CLAUDE_SESSION_ID": "claude-abc", "T3_LOOP_SESSION_ID": ""}):
            ticket = Ticket.objects.create(overlay="test")
            mine = Session.objects.create(ticket=ticket, overlay="test", agent_id="claude-abc")
            other = Session.objects.create(ticket=ticket, overlay="test", agent_id="claude-xyz")
            my_task = Task.objects.create(ticket=ticket, session=mine, phase="coding")
            Task.objects.create(ticket=ticket, session=other, phase="coding")

            rows = cast("list[dict]", call_command("tasks", "list", "--session"))

        assert [row["task_id"] for row in rows] == [my_task.pk]

    @override_settings(**COMMAND_SETTINGS)
    def test_anonymous_session_lists_nothing(self) -> None:
        with patch.dict("os.environ", {"CLAUDE_SESSION_ID": "", "T3_LOOP_SESSION_ID": "", "XDG_DATA_HOME": ""}):
            ticket = Ticket.objects.create(overlay="test")
            session = Session.objects.create(ticket=ticket, overlay="test", agent_id="claude-abc")
            Task.objects.create(ticket=ticket, session=session, phase="coding")

            rows = cast("list[dict]", call_command("tasks", "list", "--session"))

        assert rows == []

    @override_settings(**COMMAND_SETTINGS)
    def test_session_view_does_not_read_the_stale_harness_todo_store(self) -> None:
        # The harness TaskCreate/TaskUpdate list is the agent's LIVE in-memory
        # list and a CLI subprocess can only read a stale on-disk snapshot
        # (`~/.claude/tasks/<session>/*.json`) that lags the live session. The
        # CLI must NOT pretend to show the harness TODO list — it scopes the
        # teatree DB Task rows only, so `/t3:todos` builds the harness half from
        # the live TaskList tool instead. The session view must therefore never
        # feed harness-store rows into the renderer (no `harness_todos`), which
        # goes RED on the old view that read the stale store and rendered it.
        with tempfile.TemporaryDirectory() as tasks_dir:
            session_dir = Path(tasks_dir) / "claude-abc"
            session_dir.mkdir()
            (session_dir / "1.json").write_text(
                json.dumps({"id": "1", "subject": "STALE harness todo on disk", "status": "pending"}),
                encoding="utf-8",
            )
            env = {"CLAUDE_SESSION_ID": "claude-abc", "T3_LOOP_SESSION_ID": "", "CLAUDE_TASKS_DIR": tasks_dir}
            captured: dict[str, object] = {}

            def _capture(rows: object, **kwargs: object) -> None:
                captured["rows"] = rows
                captured["kwargs"] = kwargs

            with (
                patch.dict("os.environ", env),
                patch.object(tasks_cmd, "render_session_view", _capture),
            ):
                ticket = Ticket.objects.create(overlay="test")
                mine = Session.objects.create(ticket=ticket, overlay="test", agent_id="claude-abc")
                Task.objects.create(ticket=ticket, session=mine, phase="coding", execution_reason="real db task")
                call_command("tasks", "list", "--session")

        kwargs = cast("dict[str, object]", captured.get("kwargs", {}))
        assert "harness_todos" not in kwargs, "the session view must not read/pass the stale harness TODO store"


class TestSessionTodoRendering(TestCase):
    """The session-scoped renderer prints the teatree tasks only, grouped by status.

    The harness TODO list is NOT rendered here — it is the agent's live
    in-memory ``TaskList`` state, which a CLI subprocess cannot read. ``/t3:todos``
    builds that half from the live ``TaskList`` harness tool.
    """

    @staticmethod
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def _row(  # noqa: PLR0913 — test-data builder mirroring the TaskRow TypedDict fields.
        task_id: int,
        *,
        status: str,
        ticket_id: int = 1,
        phase: str = "coding",
        reason: str = "do it",
        ticket_title: str = "",
    ) -> session_view.TaskRow:
        return session_view.TaskRow(
            task_id=task_id,
            ticket_id=ticket_id,
            ticket_title=ticket_title,
            status=status,
            execution_target="headless",
            phase=phase,
            execution_reason=reason,
            claimed_by="",
        )

    def test_groups_teatree_tasks_by_status_and_omits_harness_section(self) -> None:
        out = io.StringIO()
        rows = [
            self._row(1, status="pending", reason="write the gate"),
            self._row(2, status="claimed", reason="run the suite"),
            self._row(3, status="completed", reason="read the model"),
        ]
        session_view.render_session_view(rows, session_id="claude-abc", stream=out)
        printed = out.getvalue()
        # The harness-TODO section never renders here (the CLI cannot read the
        # live harness list); only the teatree-tasks section does.
        assert "harness TODO" not in printed
        assert "teatree tasks" in printed
        assert "pending" in printed
        assert "in_progress" in printed
        assert "completed" in printed
        assert "write the gate" in printed
        assert "run the suite" in printed

    def test_no_active_session_is_explicit(self) -> None:
        out = io.StringIO()
        session_view.render_session_view([], session_id="", stream=out)
        assert "No active harness session" in out.getvalue()

    def test_empty_session_says_no_teatree_tasks(self) -> None:
        out = io.StringIO()
        session_view.render_session_view([], session_id="claude-abc", stream=out)
        assert "No teatree tasks for this session" in out.getvalue()

    def test_task_id_uses_distinct_prefix_not_bare_hash(self) -> None:
        out = io.StringIO()
        session_view.render_session_view(
            [self._row(7, status="pending", ticket_id=42, reason="do it")],
            session_id="claude-abc",
            stream=out,
        )
        printed = out.getvalue()
        assert "TODO-7" in printed
        assert "(ticket #42" in printed
        assert "task #7" not in printed

    def test_ticket_title_renders_inline(self) -> None:
        # #2092: the ``ticket #N`` on a todo line must carry the ticket title
        # inline, never a bare ``#N`` the reader can't interpret.
        out = io.StringIO()
        session_view.render_session_view(
            [self._row(7, status="pending", ticket_id=42, ticket_title="fix the broken widget", reason="do it")],
            session_id="claude-abc",
            stream=out,
        )
        printed = out.getvalue()
        assert "fix the broken widget" in printed
        assert "ticket #42 (fix the broken widget)" in printed

    def test_no_ticket_title_renders_plain_id(self) -> None:
        # A task whose ticket has no title degrades to the plain ``#N`` (no
        # empty parens), still namespace-qualified.
        out = io.StringIO()
        session_view.render_session_view(
            [self._row(7, status="pending", ticket_id=42, ticket_title="", reason="do it")],
            session_id="claude-abc",
            stream=out,
        )
        printed = out.getvalue()
        assert "ticket #42 ()" not in printed
        assert "(ticket #42" in printed

    def test_same_number_task_and_ticket_render_distinctly(self) -> None:
        out = io.StringIO()
        session_view.render_session_view(
            [self._row(5, status="pending", ticket_id=5, reason="collision case")],
            session_id="claude-abc",
            stream=out,
        )
        printed = out.getvalue()
        assert "TODO-5" in printed
        assert "ticket #5" in printed
        assert "task #5" not in printed


class TestReadHarnessTodos(TestCase):
    """``read_harness_todos`` reads the harness's OWN store for the PreCompact snapshot.

    It backs the PreCompact recovery snapshot only (which cannot call the live
    ``TaskList`` tool); it is NOT used by the interactive ``/t3:todos`` CLI view,
    which would lag the live session. There is no teatree-written ``<session>.todos``
    mirror to fall back to — that materialiser was removed as a stale mistake-source.
    """

    def test_reads_from_harness_task_store(self) -> None:
        with tempfile.TemporaryDirectory() as tasks_dir:
            session_dir = Path(tasks_dir) / "claude-abc"
            session_dir.mkdir()
            (session_dir / "1.json").write_text(
                json.dumps({"id": "1", "subject": "draft the helper", "status": "pending"}),
                encoding="utf-8",
            )
            (session_dir / "2.json").write_text(
                json.dumps({"id": "2", "subject": "wire the CLI", "status": "in_progress"}),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"CLAUDE_TASKS_DIR": tasks_dir}):
                todos = session_view.read_harness_todos("claude-abc")
        assert todos == [("pending", "draft the helper"), ("in_progress", "wire the CLI")]

    def test_orders_by_numeric_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as tasks_dir:
            session_dir = Path(tasks_dir) / "claude-abc"
            session_dir.mkdir()
            for task_id in ("2", "10", "1"):
                (session_dir / f"{task_id}.json").write_text(
                    json.dumps({"id": task_id, "subject": f"task {task_id}", "status": "pending"}),
                    encoding="utf-8",
                )
            with patch.dict("os.environ", {"CLAUDE_TASKS_DIR": tasks_dir}):
                todos = session_view.read_harness_todos("claude-abc")
        assert [text for _status, text in todos] == ["task 1", "task 2", "task 10"]

    def test_no_legacy_todowrite_mirror_fallback(self) -> None:
        # The teatree-written ``<session>.todos`` mirror was removed: a present
        # mirror file with an EMPTY harness store no longer produces todos (the
        # reader consults the harness's own store only).
        with (
            tempfile.TemporaryDirectory() as tasks_dir,
            tempfile.TemporaryDirectory() as state_dir,
            override_settings(TEATREE_CLAUDE_STATUSLINE_STATE_DIR=state_dir),
        ):
            (Path(state_dir) / "claude-abc.todos").write_text(
                "- [pending] stale mirror line\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"CLAUDE_TASKS_DIR": tasks_dir}):
                todos = session_view.read_harness_todos("claude-abc")
        assert todos == []

    def test_missing_store_is_empty(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tasks_dir,
            patch.dict("os.environ", {"CLAUDE_TASKS_DIR": tasks_dir}),
        ):
            assert session_view.read_harness_todos("claude-abc") == []

    def test_empty_session_id_is_empty(self) -> None:
        assert session_view.read_harness_todos("") == []


class TestReconcileChecklist(TestCase):
    """``tasks reconcile-checklist`` emits the in-session harness-TODO reconcile discipline.

    The harness TODO list lives only in the agent's live, in-memory ``TaskList``
    state — a CLI subprocess cannot read or write it (the Task tools bypass
    ``PreToolUse``/``PostToolUse`` hooks). So the deterministic helper a
    background loop CANNOT be is, instead, a checklist EMITTER: it prints the
    fixed reconcile/dedupe/complete steps the in-session agent then applies with
    its own ``TaskList`` / ``TaskUpdate`` / ``TaskCreate`` tools, plus the open
    teatree tasks for this session as candidate completion anchors. It writes
    nothing and transitions nothing.
    """

    @override_settings(**COMMAND_SETTINGS)
    def test_emits_the_reconcile_discipline_steps(self) -> None:
        out = io.StringIO()
        with patch.dict("os.environ", {"CLAUDE_SESSION_ID": "claude-abc", "T3_LOOP_SESSION_ID": ""}):
            call_command("tasks", "reconcile-checklist", stdout=out)
        printed = out.getvalue()
        # The agent must drive the live list with its OWN tools — the checklist
        # names them explicitly so the discipline is self-contained.
        assert "TaskList" in printed
        assert "TaskUpdate" in printed
        assert "TaskCreate" in printed
        # The three reconcile actions the maintainer asked for.
        assert "reconcile" in printed.lower()
        assert "dedup" in printed.lower() or "duplicate" in printed.lower()
        assert "completed" in printed.lower()

    @override_settings(**COMMAND_SETTINGS)
    def test_lists_open_teatree_tasks_for_this_session_as_completion_anchors(self) -> None:
        out = io.StringIO()
        with patch.dict("os.environ", {"CLAUDE_SESSION_ID": "claude-abc", "T3_LOOP_SESSION_ID": ""}):
            ticket = Ticket.objects.create(overlay="test", short_description="fix the widget")
            mine = Session.objects.create(ticket=ticket, overlay="test", agent_id="claude-abc")
            open_task = Task.objects.create(
                ticket=ticket, session=mine, phase="coding", execution_reason="land the gate"
            )
            other_ticket = Ticket.objects.create(overlay="test")
            other = Session.objects.create(ticket=other_ticket, overlay="test", agent_id="claude-other")
            Task.objects.create(ticket=other_ticket, session=other, phase="coding", execution_reason="someone else")
            call_command("tasks", "reconcile-checklist", stdout=out)
        printed = out.getvalue()
        # This session's open teatree task surfaces as a completion candidate…
        assert f"TODO-{open_task.pk}" in printed
        assert "land the gate" in printed
        # …and another session's task does not leak in.
        assert "someone else" not in printed

    @override_settings(**COMMAND_SETTINGS)
    def test_is_read_only_and_transitions_nothing(self) -> None:
        with patch.dict("os.environ", {"CLAUDE_SESSION_ID": "claude-abc", "T3_LOOP_SESSION_ID": ""}):
            ticket = Ticket.objects.create(overlay="test")
            mine = Session.objects.create(ticket=ticket, overlay="test", agent_id="claude-abc")
            task = Task.objects.create(ticket=ticket, session=mine, phase="coding")
            call_command("tasks", "reconcile-checklist", stdout=io.StringIO())
            task.refresh_from_db()
        # The emitter is a render — the task stays pending, the count is unchanged.
        assert task.status == Task.Status.PENDING
        assert Task.objects.count() == 1

    @override_settings(**COMMAND_SETTINGS)
    def test_no_session_still_emits_the_discipline(self) -> None:
        out = io.StringIO()
        with patch.dict("os.environ", {"CLAUDE_SESSION_ID": "", "T3_LOOP_SESSION_ID": ""}):
            call_command("tasks", "reconcile-checklist", stdout=out)
        printed = out.getvalue()
        # An anonymous caller has no session-scoped teatree tasks, but the
        # reconcile discipline (the load-bearing half) still prints.
        assert "TaskList" in printed


class DbOverlay(CommandOverlay):
    """CommandOverlay with a DB import strategy that always fails."""

    def get_db_import_strategy(self, worktree: Worktree) -> DbImportStrategy | None:
        return DbImportStrategy(kind="dslr", source_database="development-acme")

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def db_import(  # noqa: PLR0913 — mirrors the OverlayBase.db_import extension-point contract.
        self,
        worktree: Worktree,
        *,
        force: bool = False,
        slow_import: bool = False,
        dslr_snapshot: str = "",
        dump_path: str = "",
        approve_remote_dump: bool = False,
    ) -> bool:
        self.last_approve_remote_dump = approve_remote_dump
        return False


_DB_MOCK_OVERLAY = {"test": DbOverlay()}


class TestDbRefreshFreshDumpApproval(TestCase):
    """`db refresh --fresh-dump` is gated by a per-invocation approval (#777).

    Under `call_command` stdin/stdout are not TTYs — exactly the
    unattended-agent context the gate must refuse. The fresh-dump path
    therefore aborts before any overlay import runs, with no credentials
    or connection string in the message.
    """

    def _make_worktree(self) -> Worktree:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/777")
        return Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature",
            extra={"worktree_path": "/tmp/backend"},
            db_name="wt_777_acme",
        )

    @override_settings(**COMMAND_SETTINGS)
    def test_fresh_dump_refuses_in_non_interactive_agent_context(self) -> None:
        worktree = self._make_worktree()
        overlay = DbOverlay()
        stderr = io.StringIO()
        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value={"test": overlay}),
            patch("teatree.core.management.commands.db.resolve_worktree", return_value=worktree),
            pytest.raises(SystemExit) as exc_info,
        ):
            call_command("db", "refresh", "--fresh-dump", stderr=stderr)
        # Refusal must be a real non-zero exit (#932), not an exit-0 string.
        assert exc_info.value.code == 1
        message = stderr.getvalue()
        assert "aborted" in message
        assert "human must" in message
        # The gate fired BEFORE the overlay import — no remote dump attempted.
        assert not hasattr(overlay, "last_approve_remote_dump")

    @override_settings(**COMMAND_SETTINGS)
    def test_refusal_message_has_no_credentials(self) -> None:
        worktree = self._make_worktree()
        stderr = io.StringIO()
        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_DB_MOCK_OVERLAY),
            patch("teatree.core.management.commands.db.resolve_worktree", return_value=worktree),
            pytest.raises(SystemExit) as exc_info,
        ):
            call_command("db", "refresh", "--fresh-dump", stderr=stderr)
        assert exc_info.value.code == 1
        message = stderr.getvalue()
        assert "postgres://" not in message
        assert "PGPASSWORD" not in message
        assert "password" not in message.lower()


class TestDbImportAutoRepair(TestCase):
    @override_settings(**COMMAND_SETTINGS)
    def test_skips_db_import_when_db_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_path = str(tmp_path / "backend")
            Path(wt_path).mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/99")
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": wt_path},
                db_name="wt_99_acme",
            )

            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": wt_path}),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_DB_MOCK_OVERLAY),
                patch("teatree.utils.db.db_exists", return_value=True),
            ):
                call_command("worktree", "provision")

            # db_import was NOT called — DB already exists
            wt = Worktree.objects.get(ticket=ticket)
            assert "db_import_failures" not in (wt.extra or {})

    @override_settings(**COMMAND_SETTINGS)
    def test_aborts_when_db_missing_and_import_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_path = str(tmp_path / "backend")
            Path(wt_path).mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/100")
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": wt_path},
                db_name="wt_100_acme",
            )

            # db is missing → db_import is attempted; the mock overlay's import
            # returns False → #2208 aborts provision with SystemExit(1) rather
            # than warning and continuing.
            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": wt_path}),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_DB_MOCK_OVERLAY),
                patch("teatree.utils.db.db_exists", return_value=False),
                pytest.raises(SystemExit) as exc_info,
            ):
                call_command("worktree", "provision")

            assert exc_info.value.code == 1


class TestUpdateTicketVariant(TestCase):
    def test_updates_ticket_variant_and_recomputes_db_name(self) -> None:
        from teatree.core.management.commands.worktree import _update_ticket_variant  # noqa: PLC0415

        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/200",
            variant="old",
        )
        wt = Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="backend",
            branch="feature",
            db_name=f"wt_{ticket.ticket_number}_old",
        )

        _update_ticket_variant(ticket, "new")

        ticket.refresh_from_db()
        wt.refresh_from_db()
        assert ticket.variant == "new"
        assert wt.db_name == f"wt_{ticket.ticket_number}_new"

    def test_skips_save_when_db_name_unchanged(self) -> None:
        from teatree.core.management.commands.worktree import _update_ticket_variant  # noqa: PLC0415

        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/201",
            variant="",
        )
        wt = Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="backend",
            branch="feature",
            db_name=f"wt_{ticket.ticket_number}",
        )
        original_db_name = wt.db_name

        # Variant "" → "acme" should change the db_name
        _update_ticket_variant(ticket, "acme")

        wt.refresh_from_db()
        assert wt.db_name != original_db_name
        assert wt.db_name == f"wt_{ticket.ticket_number}_acme"


class TestFollowupCommands(TestCase):
    def test_sync_reports_no_repos_from_default_overlay(self) -> None:
        with patch.object(
            overlay_loader_mod,
            "_discover_overlays",
            return_value=_MOCK_OVERLAY,
        ):
            result = cast("dict[str, int | list[str]]", call_command("followup", "sync"))

        errors = result["errors"]
        assert isinstance(errors, list)
        assert len(errors) == 1
        assert "No code host token for" in errors[0]


class TestTicketCommand(TestCase):
    """Tests for the ticket management command (transition + list)."""

    def test_transition_scopes_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        result = cast(
            "dict[str, object]",
            call_command("ticket", "transition", ticket.pk, "scope"),
        )
        assert result["state"] == Ticket.State.SCOPED

    def test_transition_unknown_returns_error(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        result = cast(
            "dict[str, object]",
            call_command("ticket", "transition", ticket.pk, "nonexistent"),
        )
        assert "Unknown transition" in str(result["error"])

    def test_transition_not_found_returns_error(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command("ticket", "transition", 99999, "scope"),
        )
        assert "not found" in str(result["error"])

    def test_transition_not_allowed_returns_error(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        result = cast(
            "dict[str, object]",
            call_command("ticket", "transition", ticket.pk, "code"),
        )
        assert "not allowed" in str(result["error"])

    def test_transition_dod_refusal_returns_error_not_traceback(self) -> None:
        # #1652: a ship transition whose body raises DodLocalE2EError
        # (an InvalidTransitionError, disjoint from TransitionNotAllowed)
        # returns a refusal error carrying the reason; the FSM stays put.
        from teatree.core.gates.dod_gate import DodLocalE2EError  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        reason = "UI-visible ticket has no local-stack E2E"
        with patch.object(Ticket, "ship", side_effect=DodLocalE2EError(reason)):
            result = cast(
                "dict[str, object]",
                call_command("ticket", "transition", ticket.pk, "ship"),
            )
        assert "refused" in str(result["error"])
        assert reason in str(result["error"])
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

    def test_transition_mark_review_no_action_delivers_reviewer_ticket(self) -> None:
        """#1077: the no-action disposition is reachable via the CLI transition."""
        from teatree.core.models.ticket import schedule_external_review  # noqa: PLC0415

        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab/x/-/merge_requests/1077c",
            role=Ticket.Role.REVIEWER,
            extra={"reviewed_sha": "sha1"},
        )
        schedule_external_review(ticket)
        result = cast(
            "dict[str, object]",
            call_command("ticket", "transition", ticket.pk, "mark_review_no_action"),
        )
        assert result["state"] == Ticket.State.DELIVERED
        ticket.refresh_from_db()
        assert ticket.extra["last_review_state"] == "reviewed_no_action"

    def test_list_returns_all_tickets(self) -> None:
        Ticket.objects.create(overlay="test")
        Ticket.objects.create(overlay="other")
        result = cast(
            "list[dict[str, object]]",
            call_command("ticket", "list"),
        )
        assert len(result) == 2

    def test_list_filters_by_state(self) -> None:
        Ticket.objects.create(overlay="test", state=Ticket.State.SCOPED)
        Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        result = cast(
            "list[dict[str, object]]",
            call_command("ticket", "list", state="scoped"),
        )
        assert len(result) == 1
        assert result[0]["state"] == "scoped"

    def test_list_filters_by_overlay(self) -> None:
        Ticket.objects.create(overlay="alpha")
        Ticket.objects.create(overlay="beta")
        result = cast(
            "list[dict[str, object]]",
            call_command("ticket", "list", overlay="alpha"),
        )
        assert len(result) == 1
        assert result[0]["overlay"] == "alpha"


class TestTasksCreateCommand(TestCase):
    """Tests for the tasks create subcommand — phase handoff used by /t3:next."""

    def test_create_headless_defaults_for_free_form_phase(self) -> None:
        # ``scoping`` has no registered author phase agent, so it is genuinely
        # headless and the default sticks. (A loop-dispatched phase like
        # ``coding`` is routed to INTERACTIVE by the Task.save invariant — see
        # test_create_loop_dispatched_phase_is_interactive.)
        ticket = Ticket.objects.create(overlay="test")
        result = cast(
            "dict[str, object]",
            call_command("tasks", "create", ticket.pk, phase="scoping", reason="Decide X."),
        )
        assert result["phase"] == "scoping"
        assert result["execution_target"] == Task.ExecutionTarget.HEADLESS
        task = Task.objects.get(pk=result["task_id"])
        assert task.ticket_id == ticket.pk
        assert task.execution_reason == "Decide X."
        assert task.execution_target == Task.ExecutionTarget.HEADLESS
        assert task.session.ticket_id == ticket.pk

    def test_create_loop_dispatched_phase_is_interactive(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        result = cast(
            "dict[str, object]",
            call_command("tasks", "create", ticket.pk, phase="coding", reason="Implement X."),
        )
        assert result["execution_target"] == Task.ExecutionTarget.INTERACTIVE
        task = Task.objects.get(pk=result["task_id"])
        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE

    def test_create_reuses_latest_session(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        existing = Session.objects.create(ticket=ticket, overlay="test")
        result = cast(
            "dict[str, object]",
            call_command("tasks", "create", ticket.pk, phase="coding", reason="x"),
        )
        task = Task.objects.get(pk=result["task_id"])
        assert task.session_id == existing.pk

    def test_create_interactive_flag(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        result = cast(
            "dict[str, object]",
            call_command("tasks", "create", ticket.pk, phase="scoping", reason="Decide X.", interactive=True),
        )
        task = Task.objects.get(pk=result["task_id"])
        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE

    def test_create_reason_from_file(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write("Long multiline prompt.\nWith details.")
            reason_path = Path(fh.name)
        self.addCleanup(reason_path.unlink)
        result = cast(
            "dict[str, object]",
            call_command("tasks", "create", ticket.pk, phase="coding", reason_file=reason_path),
        )
        task = Task.objects.get(pk=result["task_id"])
        assert task.execution_reason == "Long multiline prompt.\nWith details."

    def test_create_requires_non_blank_reason(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        with pytest.raises(SystemExit):
            call_command("tasks", "create", ticket.pk, phase="coding", reason="   ")

    def test_create_requires_phase(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        with pytest.raises(SystemExit):
            call_command("tasks", "create", ticket.pk, reason="x")

    def test_create_nonexistent_ticket(self) -> None:
        with pytest.raises(SystemExit):
            call_command("tasks", "create", 99999, phase="coding", reason="x")


class TestTasksCancelCommand(TestCase):
    """Tests for the tasks cancel subcommand."""

    def test_cancel_pending_task(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        call_command("tasks", "cancel", task.pk)
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_cancel_claimed_task_without_confirm(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        task.claim(claimed_by="worker-1")
        with pytest.raises(SystemExit):
            call_command("tasks", "cancel", task.pk)

    def test_cancel_claimed_task_with_confirm(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        task.claim(claimed_by="worker-1")
        call_command("tasks", "cancel", task.pk, confirm=True)
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_cancel_completed_task(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.COMPLETED,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        with pytest.raises(SystemExit):
            call_command("tasks", "cancel", task.pk)

    def test_cancel_nonexistent_task(self) -> None:
        with pytest.raises(SystemExit):
            call_command("tasks", "cancel", 99999)

    def test_cancel_with_reason_persists_a_task_attempt(self) -> None:
        # #2559: a cancellation reason must persist to the DB so the audit trail
        # records WHY a task was cancelled — mirroring how ``complete --note``
        # records a TaskAttempt. Before the fix ``cancel`` accepted no --reason.
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )

        call_command("tasks", "cancel", task.pk, reason="superseded by !6219")

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        attempt = task.attempts.get()
        assert attempt.error == "superseded by !6219"
        assert attempt.result == {"cancel_reason": "superseded by !6219"}
        assert attempt.exit_code == 1  # a cancellation is a non-success terminal

    def test_cancel_without_reason_records_no_attempt(self) -> None:
        # The reason is optional — a bare cancel stays a clean no-attempt fail
        # exactly as before (no regression).
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )

        call_command("tasks", "cancel", task.pk)

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.attempts.count() == 0

    def test_cancel_blank_reason_records_no_attempt(self) -> None:
        # A whitespace-only reason is treated as no reason — no empty audit row.
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )

        call_command("tasks", "cancel", task.pk, reason="   ")

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.attempts.count() == 0


class TestTasksCompleteCommand(TestCase):
    """Tests for the tasks complete subcommand (#1031).

    Out-of-band terminal-success transition: a claimed task whose
    underlying work was driven outside the loop is marked completed so
    the loop stops re-emitting it.
    """

    def _claimed_task(self) -> Task:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.PLANNED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        task.claim(claimed_by="worker-1")
        return task

    def test_complete_claimed_task_clears_lease(self) -> None:
        task = self._claimed_task()

        call_command("tasks", "complete", task.pk)

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert task.claimed_by == ""
        assert task.lease_expires_at is None
        assert task.heartbeat_at is None

    def test_complete_advances_ticket(self) -> None:
        task = self._claimed_task()

        call_command("tasks", "complete", task.pk)

        task.ticket.refresh_from_db()
        assert task.ticket.state == Ticket.State.CODED

    def test_complete_records_note_as_attempt(self) -> None:
        task = self._claimed_task()

        call_command("tasks", "complete", task.pk, note="work landed via !6219 out-of-band")

        attempt = TaskAttempt.objects.filter(task=task).first()
        assert attempt is not None
        assert attempt.exit_code == 0
        assert attempt.result == {"complete_note": "work landed via !6219 out-of-band"}

    def test_complete_without_note_records_no_attempt(self) -> None:
        task = self._claimed_task()

        call_command("tasks", "complete", task.pk)

        assert not TaskAttempt.objects.filter(task=task).exists()

    def test_complete_already_completed_is_idempotent(self) -> None:
        task = self._claimed_task()
        task.complete()
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

        # No exception, exit 0: idempotent no-op.
        call_command("tasks", "complete", task.pk)

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert not TaskAttempt.objects.filter(task=task).exists()

    def test_complete_pending_task_rejected(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        with pytest.raises(SystemExit) as exc:
            call_command("tasks", "complete", task.pk)
        assert exc.value.code == 1
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_complete_failed_task_rejected(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.FAILED,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        with pytest.raises(SystemExit) as exc:
            call_command("tasks", "complete", task.pk)
        assert exc.value.code == 1

    def test_complete_nonexistent_task(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command("tasks", "complete", 99999)
        assert exc.value.code == 1


class TestTasksListCommand(TestCase):
    """Tests for the tasks list subcommand."""

    def test_list_all_tasks(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        result = cast("list[dict[str, object]]", call_command("tasks", "list"))
        assert len(result) == 2

    def test_list_filters_by_status(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.COMPLETED,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        result = cast(
            "list[dict[str, object]]",
            call_command("tasks", "list", status="completed"),
        )
        assert len(result) == 1

    def test_list_filters_by_execution_target(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        result = cast(
            "list[dict[str, object]]",
            call_command("tasks", "list", execution_target="interactive"),
        )
        assert len(result) == 1
        assert result[0]["execution_target"] == "interactive"

    def test_list_reaps_stale_claims_before_returning_rows(self) -> None:
        from datetime import timedelta  # noqa: PLC0415

        from django.utils import timezone  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        stale = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
            claimed_by="dead-worker",
            lease_expires_at=timezone.now() - timedelta(minutes=5),
        )

        result = cast("list[dict[str, object]]", call_command("tasks", "list"))

        stale.refresh_from_db()
        assert stale.status == Task.Status.FAILED
        statuses = [row["status"] for row in result]
        assert "claimed" not in statuses
        assert "failed" in statuses

    def test_render_tasks_table_formats_rows(self) -> None:
        from io import StringIO  # noqa: PLC0415

        from teatree.core.management.commands.tasks import TaskRow, _render_tasks_table  # noqa: PLC0415

        rows: list[TaskRow] = [
            TaskRow(
                task_id=7,
                ticket_id=42,
                ticket_title="fix the broken widget",
                status="pending",
                execution_target="interactive",
                phase="coding",
                execution_reason="resume after user input",
                claimed_by="",
            ),
        ]
        buf = StringIO()
        _render_tasks_table(rows, stream=buf)
        out = buf.getvalue()
        assert "teatree tasks (1)" in out
        assert "ID" in out
        assert "Ticket" in out
        assert "Phase" in out
        assert "interactive" in out
        assert "coding" in out
        assert "resume after" in out
        # #2092: the table carries the ticket title, never a bare numeric id alone.
        assert "Title" in out
        assert "fix the broken widget" in out

    def test_render_tasks_table_handles_empty(self) -> None:
        from io import StringIO  # noqa: PLC0415

        from teatree.core.management.commands.tasks import _render_tasks_table  # noqa: PLC0415

        buf = StringIO()
        _render_tasks_table([], stream=buf)
        assert "No tasks" in buf.getvalue()


class TestTasksStartCommand(TestCase):
    """Tests for the tasks start subcommand (inline interactive launch)."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/99")
        self.session = Session.objects.create(ticket=self.ticket, overlay="test", agent_id="agent-1")

    @staticmethod
    def _patch_env(run_mock: MagicMock) -> list[AbstractContextManager[object]]:
        return [
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(tasks_cmd.shutil, "which", return_value="/usr/bin/claude"),
            patch("teatree.utils.run.run_streamed", new=run_mock),
        ]

    @override_settings(**COMMAND_SETTINGS)
    def test_start_claims_next_interactive_task_and_runs_claude(self) -> None:
        Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        user_task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            phase="coding",
        )

        run_mock = MagicMock(return_value=0)
        p1, p2, p3 = self._patch_env(run_mock)
        with p1, p2, p3, pytest.raises(SystemExit) as exc:
            call_command("tasks", "start")

        assert exc.value.code == 0
        run_mock.assert_called_once()
        argv = run_mock.call_args[0][0]
        assert argv[0] == "/usr/bin/claude"
        assert "--append-system-prompt" in argv

        user_task.refresh_from_db()
        assert user_task.status == Task.Status.CLAIMED
        assert user_task.claimed_by == "cli"
        assert TaskAttempt.objects.filter(task=user_task, launch_url="").count() == 1

    @override_settings(**COMMAND_SETTINGS)
    def test_start_with_task_id_claims_specific_task(self) -> None:
        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )

        run_mock = MagicMock(return_value=0)
        p1, p2, p3 = self._patch_env(run_mock)
        with p1, p2, p3, pytest.raises(SystemExit):
            call_command("tasks", "start", task.pk)

        run_mock.assert_called_once()
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED

    @override_settings(**COMMAND_SETTINGS)
    def test_start_with_no_pending_tasks_skips_run(self) -> None:
        run_mock = MagicMock(return_value=0)
        p1, p2, p3 = self._patch_env(run_mock)
        with p1, p2, p3:
            call_command("tasks", "start")
        run_mock.assert_not_called()

    @override_settings(**COMMAND_SETTINGS)
    def test_start_rejects_headless_task_id(self) -> None:
        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )

        run_mock = MagicMock(return_value=0)
        p1, p2, p3 = self._patch_env(run_mock)
        with p1, p2, p3, pytest.raises(SystemExit):
            call_command("tasks", "start", task.pk)

        run_mock.assert_not_called()
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    @override_settings(**COMMAND_SETTINGS)
    def test_start_resumes_when_session_has_claude_uuid(self) -> None:
        uuid = "01234567-89ab-cdef-0123-456789abcdef"
        session = Session.objects.create(ticket=self.ticket, overlay="test", agent_id=uuid)
        Task.objects.create(
            ticket=self.ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )

        run_mock = MagicMock(return_value=0)
        p1, p2, p3 = self._patch_env(run_mock)
        with p1, p2, p3, pytest.raises(SystemExit):
            call_command("tasks", "start")

        argv = run_mock.call_args[0][0]
        assert "--resume" in argv
        assert uuid in argv

    @override_settings(**COMMAND_SETTINGS)
    def test_start_with_invalid_task_id_exits(self) -> None:
        run_mock = MagicMock(return_value=0)
        p1, p2, p3 = self._patch_env(run_mock)
        with p1, p2, p3, pytest.raises(SystemExit) as exc:
            call_command("tasks", "start", 99999)
        assert exc.value.code == 1
        run_mock.assert_not_called()


class TestResolveReason:
    def test_reads_stdin_when_dash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from io import StringIO  # noqa: PLC0415

        from teatree.core.management.commands.tasks import _resolve_reason  # noqa: PLC0415

        monkeypatch.setattr("sys.stdin", StringIO("from stdin"))
        assert _resolve_reason(reason="-", reason_file=None) == "from stdin"

    def test_returns_inline_reason(self) -> None:
        from teatree.core.management.commands.tasks import _resolve_reason  # noqa: PLC0415

        assert _resolve_reason(reason="inline", reason_file=None) == "inline"

    def test_reads_from_file_when_provided(self, tmp_path: Path) -> None:
        from teatree.core.management.commands.tasks import _resolve_reason  # noqa: PLC0415

        f = tmp_path / "reason.txt"
        f.write_text("from file")
        assert _resolve_reason(reason="", reason_file=f) == "from file"

    def test_returns_empty_when_nothing_provided(self) -> None:
        from teatree.core.management.commands.tasks import _resolve_reason  # noqa: PLC0415

        assert _resolve_reason(reason="", reason_file=None) == ""
