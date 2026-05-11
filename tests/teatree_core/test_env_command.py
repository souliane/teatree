"""Integration tests for ``t3 env`` management command."""

from dataclasses import dataclass
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket, Worktree, WorktreeEnvOverride


@dataclass
class _FakeSpec:
    content: str


def _make_ticket(overlay: str = "teatree") -> Ticket:
    return Ticket.objects.create(overlay=overlay, issue_url="https://example.com/issues/1")


def _make_worktree(ticket: Ticket, repo_path: str = "/tmp/wt/repo") -> Worktree:
    return Worktree.objects.create(
        ticket=ticket,
        repo_path=repo_path,
        branch="ac-test",
        extra={"worktree_path": f"{repo_path}-wt"},
    )


class TestEnvShow(TestCase):
    def test_show_renders_env_from_db(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch(
                "teatree.core.management.commands.env.render_env_cache",
                return_value=_FakeSpec("# header\n\nFOO=bar\nBAZ=qux"),
            ),
        ):
            call_command("env", "show", "--path", "/tmp/wt/repo")

    def test_show_json_format(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.render_env_cache", return_value=_FakeSpec("FOO=bar")),
        ):
            call_command("env", "show", "--path", "/tmp/wt/repo", "--format", "json")

    def test_show_returns_error_when_not_provisioned(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.render_env_cache", return_value=None),
        ):
            call_command("env", "show", "--path", "/tmp/wt/repo")


class TestEnvSetVar(TestCase):
    def test_set_var_persists_override(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.set_override") as mock_set,
        ):
            call_command("env", "set-var", "MY_KEY=my_value", "--path", "/tmp/wt/repo")
            mock_set.assert_called_once_with(wt, "MY_KEY", "my_value")

    def test_set_var_rejects_missing_equals(self) -> None:
        call_command("env", "set-var", "NOEQUALS", "--path", "/tmp/wt/repo")

    def test_set_var_reports_value_error(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.set_override", side_effect=ValueError("core key")),
        ):
            call_command("env", "set-var", "BAD=val", "--path", "/tmp/wt/repo")


class TestEnvUnset(TestCase):
    def test_unset_deletes_override(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        WorktreeEnvOverride.objects.create(worktree=wt, key="MY_KEY", value="val")
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.write_env_cache"),
        ):
            call_command("env", "unset", "MY_KEY", "--path", "/tmp/wt/repo")
            assert not WorktreeEnvOverride.objects.filter(worktree=wt, key="MY_KEY").exists()

    def test_unset_nonexistent_key(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        with patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt):
            call_command("env", "unset", "NOPE", "--path", "/tmp/wt/repo")


class TestEnvOverrides(TestCase):
    def test_lists_overrides(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.load_overrides", return_value={"A": "1", "B": "2"}),
        ):
            call_command("env", "overrides", "--path", "/tmp/wt/repo")

    def test_lists_empty_overrides(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.load_overrides", return_value={}),
        ):
            call_command("env", "overrides", "--path", "/tmp/wt/repo")


class TestEnvCheck(TestCase):
    def test_check_in_sync(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.detect_drift", return_value=(False, "/tmp/cache")),
        ):
            call_command("env", "check", "--path", "/tmp/wt/repo")

    def test_check_drifted(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.detect_drift", return_value=(True, "/tmp/cache")),
        ):
            call_command("env", "check", "--path", "/tmp/wt/repo")
