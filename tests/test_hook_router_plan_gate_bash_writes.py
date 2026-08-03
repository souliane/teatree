# test-path: cross-cutting
# Exercises the hooks/scripts/hook_router.py plan gate (no src/teatree mirror) together
# with teatree.core.models, so it spans packages.
"""The plan-before-code gate must see a file written through Bash (#4091).

The gate opened with ``if tool_name not in {"Edit", "Write"}: return False``, so
every file written through the shell reached it as nothing at all. Measured: a
full day of implementation in which each source file was written by a
``python3 - <<PY`` heredoc or ``sed``, and the gate never fired once.

These tests drive the real handler against real ``Worktree``/``Ticket`` rows and
a real git repo, so the whole resolution chain runs. Both directions are pinned:
a shell write into the repo DENIES, and the false positives that would make the
gate unusable — a heredoc into ``/tmp``, a read-only ``grep``, a command whose
target cannot be pinned — all ALLOW.
"""

import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import hooks.scripts.hook_router as router
from teatree.core.models import Ticket, Worktree
from tests._git_repo import make_git_repo, run_git


def _started_worktree(path: Path) -> str:
    """A real git repo registered as the worktree of a STARTED (unplanned) ticket."""
    make_git_repo(path, initial_commit=False)
    toplevel = run_git(path, "rev-parse", "--show-toplevel")
    ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
    Worktree.objects.create(
        overlay="test",
        ticket=ticket,
        repo_path="backend",
        branch="4091-x",
        extra={"worktree_path": toplevel},
    )
    return toplevel


def _bash_event(cwd: str, command: str) -> dict:
    return {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}}


def _run(data: dict) -> tuple[bool, dict | None, str]:
    out, err = StringIO(), StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        blocked = router.handle_block_edit_before_planned(data)
    raw = out.getvalue().strip()
    return blocked, (json.loads(raw) if raw else None), err.getvalue()


class TestBashMediatedWritesAreGated(TestCase):
    def test_python_heredoc_writing_a_source_file_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _started_worktree(Path(tmp))
            command = 'python3 - <<PY\nopen("src/app/x.py", "w").write("hi")\nPY\n'
            blocked, payload, _ = _run(_bash_event(toplevel, command))
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"

    def test_sed_in_place_on_a_source_file_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _started_worktree(Path(tmp))
            blocked, payload, _ = _run(_bash_event(toplevel, "sed -i 's/old/new/' src/app/x.py"))
        assert blocked is True
        assert payload is not None

    def test_heredoc_redirect_into_a_source_file_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _started_worktree(Path(tmp))
            command = "cat > src/app/models.py <<'EOF'\nclass A:\n    pass\nEOF\n"
            blocked, payload, _ = _run(_bash_event(toplevel, command))
        assert blocked is True
        assert payload is not None

    def test_planned_ticket_allows_the_same_bash_write(self) -> None:
        # The anti-vacuous companion: the deny is keyed on the STARTED state,
        # not on the command shape, so a planned ticket is untouched.
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _started_worktree(Path(tmp))
            Ticket.objects.update(state=Ticket.State.PLANNED)
            blocked, _, _ = _run(_bash_event(toplevel, "sed -i 's/old/new/' src/app/x.py"))
        assert blocked is False


class TestLegitimateShellWorkIsNotBlocked(TestCase):
    """The false-positive surface: this gate must not make the shell unusable."""

    def test_write_outside_the_repo_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as scratch:
            toplevel = _started_worktree(Path(tmp))
            command = f"cat > {scratch}/notes.md <<'EOF'\nscratch\nEOF\n"
            blocked, payload, _ = _run(_bash_event(toplevel, command))
        assert blocked is False
        assert payload is None

    def test_read_only_commands_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _started_worktree(Path(tmp))
            command = "grep -rn 'needle' src/ && git status --short && pytest -q 2>&1 | tail -5"
            blocked, payload, _ = _run(_bash_event(toplevel, command))
        assert blocked is False
        assert payload is None

    def test_a_quoted_redirect_character_is_an_argument_not_a_write(self) -> None:
        # Redirection is shell SYNTAX. Reading the quote-DECODED token instead of
        # the verbatim span turned `grep -rn '>' src/` into "writes src/" — the
        # whole source tree — and denied an everyday command.
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _started_worktree(Path(tmp))
            command = "grep -rn '>' src/ && git commit -m \"> blockquote note\""
            blocked, payload, _ = _run(_bash_event(toplevel, command))
        assert blocked is False
        assert payload is None

    def test_read_only_python_heredoc_is_allowed_and_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _started_worktree(Path(tmp))
            command = 'python3 - <<PY\nprint(open("src/app/x.py").read())\nPY\n'
            blocked, payload, stderr = _run(_bash_event(toplevel, command))
        assert blocked is False
        assert payload is None
        assert stderr == ""

    def test_unpinnable_write_target_warns_instead_of_denying(self) -> None:
        # House doctrine: an AMBIGUOUS case warns rather than hard-failing. A
        # false negative is exactly the pre-fix behaviour and costs nothing new;
        # a false positive blocks legitimate work.
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _started_worktree(Path(tmp))
            command = 'python3 - <<PY\np = sys.argv[1]\nopen(p, "w").write("hi")\nPY\n'
            blocked, payload, stderr = _run(_bash_event(toplevel, command))
        assert blocked is False
        assert payload is None
        assert "plan-gate" in stderr.lower()

    def test_skip_token_in_the_command_allows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            toplevel = _started_worktree(Path(tmp))
            command = "sed -i 's/old/new/' src/app/x.py  # [skip-plan-gate: mechanical rename]"
            blocked, payload, _ = _run(_bash_event(toplevel, command))
        assert blocked is False
        assert payload is None
