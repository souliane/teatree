"""Anti-vacuous proof the plan-before-code edit-block gate is LIVE (#1957).

The gate (``handle_block_edit_before_planned``) denies Edit/Write when the
worktree's ticket is still STARTED. It resolves the ticket through the REAL
``_ticket_state_for_cwd`` → git toplevel → ``Worktree`` row → ``Ticket.state``
path. The gate-liveness corpus monkeypatches ``_ticket_state_for_cwd`` away, so
it never exercised that real lookup — and the lookup queried ``path=`` (a field
that does not exist on ``Worktree``; the on-disk path lives in
``extra['worktree_path']``), raised ``FieldError``, was swallowed by the broad
``except``, and the gate failed open on EVERY invocation since merge.

These tests drive the gate through a real git repo + a real ``Worktree`` row so
the resolver runs for real. RED with the field-name bug reintroduced.
"""

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import hooks.scripts.hook_router as router
from teatree.core.models import Ticket, Worktree
from tests._git_repo import make_git_repo, run_git


def _git_repo(path: Path) -> str:
    """Init a real git repo at *path* and return its resolved toplevel."""
    make_git_repo(path, initial_commit=False)
    return run_git(path, "rev-parse", "--show-toplevel")


def _capture_block(data: dict) -> tuple[bool, dict | None]:
    buf = StringIO()
    with patch("sys.stdout", buf):
        blocked = router.handle_block_edit_before_planned(data)
    raw = buf.getvalue().strip()
    return blocked, (json.loads(raw) if raw else None)


class TestBlockEditBeforePlannedIsLive(TestCase):
    def _edit_input(self, cwd: str) -> dict:
        return {
            "tool_name": "Edit",
            "cwd": cwd,
            "tool_input": {"file_path": f"{cwd}/foo.py", "old_string": "a", "new_string": "b"},
        }

    def test_denies_edit_on_started_ticket(self) -> None:
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _git_repo(Path(tmp))
            ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="42-x",
                extra={"worktree_path": toplevel},
            )
            blocked, payload = _capture_block(self._edit_input(toplevel))
        assert blocked is True
        assert payload is not None

    def test_allows_edit_on_planned_ticket(self) -> None:
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _git_repo(Path(tmp))
            ticket = Ticket.objects.create(overlay="test", state=Ticket.State.PLANNED)
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="42-x",
                extra={"worktree_path": toplevel},
            )
            blocked, _ = _capture_block(self._edit_input(toplevel))
        assert blocked is False

    def test_ticket_state_for_cwd_resolves_via_real_worktree_row(self) -> None:
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _git_repo(Path(tmp))
            ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="42-x",
                extra={"worktree_path": toplevel},
            )
            assert router._ticket_state_for_cwd(toplevel) == "started"

    def test_programming_error_in_resolver_logs_loudly_not_silently(self) -> None:
        """A programming-error class bug must be LOUD, not silently → ALLOW.

        The #1957 root cause was a ``FieldError`` (a programming error)
        swallowed by a broad ``except`` and converted to a silent fail-open.
        The hook must stay crash-proof (no raise to the user, returns ``None``)
        but a programming error must emit a loud stderr NOTE so the dead gate
        is diagnosable instead of invisible. Simulate a programming error in
        the narrow resolver and assert: (a) no crash, (b) a loud NOTE on stderr.
        """
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _git_repo(Path(tmp))
            buf = StringIO()
            with (
                patch.object(router, "_resolve_worktree_state", side_effect=TypeError("boom")),
                patch("sys.stderr", buf),
            ):
                state = router._ticket_state_for_cwd(toplevel)
        assert state is None
        assert "plan-gate" in buf.getvalue().lower()


class TestBlockBashMutationBeforePlanned(TestCase):
    """The plan-gate must also block change-making Bash on a STARTED ticket (#2425).

    Edit/Write are not the only way to make a change before a plan exists — a
    raw ``git commit`` / ``git push`` / ``gh pr create`` lands work just as
    surely. #2425's acceptance widens the gated set to Bash matching the
    change-making verbs while leaving read-only investigation
    (``git status`` / ``git log`` / ``git diff``) ungated. Each test drives the
    REAL ``_ticket_state_for_cwd`` resolver through a real git repo + Worktree
    row so the gate runs end to end.
    """

    def _bash_input(self, cwd: str, command: str) -> dict:
        return {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}}

    def _started_worktree(self, tmp: str) -> str:
        toplevel = _git_repo(Path(tmp))
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="backend",
            branch="42-x",
            extra={"worktree_path": toplevel},
        )
        return toplevel

    def test_denies_git_commit_on_started_ticket(self) -> None:
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            toplevel = self._started_worktree(tmp)
            blocked, payload = _capture_block(self._bash_input(toplevel, "git commit -m 'wip'"))
        assert blocked is True
        assert payload is not None
        # The deny carries the plan_gate marker so the transcript-eval (#2138)
        # can distinguish it from the other PreToolUse gates.
        assert "PLAN GATE" in payload["permissionDecisionReason"]

    def test_denies_git_push_on_started_ticket(self) -> None:
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            toplevel = self._started_worktree(tmp)
            blocked, _ = _capture_block(self._bash_input(toplevel, "git push origin 42-x"))
        assert blocked is True

    def test_denies_gh_pr_create_on_started_ticket(self) -> None:
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            toplevel = self._started_worktree(tmp)
            blocked, _ = _capture_block(self._bash_input(toplevel, "gh pr create --fill"))
        assert blocked is True

    def test_allows_read_only_git_status_on_started_ticket(self) -> None:
        """Investigation is never gated — plans are for changes, not for looking."""
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            toplevel = self._started_worktree(tmp)
            blocked, _ = _capture_block(self._bash_input(toplevel, "git status"))
        assert blocked is False

    def test_allows_read_only_git_log_on_started_ticket(self) -> None:
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            toplevel = self._started_worktree(tmp)
            blocked, _ = _capture_block(self._bash_input(toplevel, "git log --oneline -5"))
        assert blocked is False

    def test_allows_git_commit_on_planned_ticket(self) -> None:
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _git_repo(Path(tmp))
            ticket = Ticket.objects.create(overlay="test", state=Ticket.State.PLANNED)
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="42-x",
                extra={"worktree_path": toplevel},
            )
            blocked, _ = _capture_block(self._bash_input(toplevel, "git commit -m 'wip'"))
        assert blocked is False

    def test_skip_token_in_command_allows_git_commit_on_started_ticket(self) -> None:
        """The per-call ``[skip-plan-gate: <reason>]`` escape works on the Bash arm too."""
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            toplevel = self._started_worktree(tmp)
            command = "git commit -m 'typo fix [skip-plan-gate: trivial mechanical edit]'"
            buf = StringIO()
            with patch("sys.stderr", buf):
                blocked, _ = _capture_block(self._bash_input(toplevel, command))
        assert blocked is False
