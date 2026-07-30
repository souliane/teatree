"""Integration tests for ``t3 env`` management command."""

import json
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket, Worktree, WorktreeEnvOverride
from teatree.utils.postgres_secret import PostgresPasswordUnavailableError


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
        out = StringIO()
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch(
                "teatree.core.management.commands.env.render_env_cache",
                return_value=_FakeSpec("# header\n\nFOO=bar\nBAZ=qux"),
            ),
        ):
            result = call_command("env", "show", "--path", "/tmp/wt/repo", stdout=out)
        assert result == 0
        rendered = out.getvalue()
        # Comment/blank lines are dropped; each KEY=VALUE pair is printed verbatim.
        assert "FOO=bar" in rendered
        assert "BAZ=qux" in rendered
        assert "# header" not in rendered

    def test_show_json_format(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        out = StringIO()
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.render_env_cache", return_value=_FakeSpec("FOO=bar")),
        ):
            result = call_command("env", "show", "--path", "/tmp/wt/repo", "--format", "json", stdout=out)
        assert result == 0
        assert json.loads(out.getvalue()) == {"FOO": "bar"}

    def test_show_returns_error_when_not_provisioned(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        err = StringIO()
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.render_env_cache", return_value=None),
        ):
            result = call_command("env", "show", "--path", "/tmp/wt/repo", stderr=err)
        assert result == 1
        assert "not provisioned" in err.getvalue()


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
        err = StringIO()
        result = call_command("env", "set-var", "NOEQUALS", "--path", "/tmp/wt/repo", stderr=err)
        assert result == 2
        assert "expected KEY=VALUE" in err.getvalue()

    def test_set_var_reports_value_error(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        err = StringIO()
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.set_override", side_effect=ValueError("core key")),
        ):
            result = call_command("env", "set-var", "BAD=val", "--path", "/tmp/wt/repo", stderr=err)
        assert result == 1
        assert "core key" in err.getvalue()


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
        err = StringIO()
        with patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt):
            result = call_command("env", "unset", "NOPE", "--path", "/tmp/wt/repo", stderr=err)
        assert result == 1
        assert "no override named NOPE" in err.getvalue()


class TestEnvOverrides(TestCase):
    def test_lists_overrides(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        out = StringIO()
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.load_overrides", return_value={"A": "1", "B": "2"}),
        ):
            result = call_command("env", "overrides", "--path", "/tmp/wt/repo", stdout=out)
        assert result == 0
        rendered = out.getvalue()
        assert "A=1" in rendered
        assert "B=2" in rendered

    def test_lists_empty_overrides(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        out = StringIO()
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.load_overrides", return_value={}),
        ):
            result = call_command("env", "overrides", "--path", "/tmp/wt/repo", stdout=out)
        assert result == 0
        assert "(no overrides)" in out.getvalue()


class TestEnvSystemCheckCollision(TestCase):
    """Regression: the env management command must not crash Django's system check.

    Django ``BaseCommand`` calls ``self.check(**check_kwargs)`` on every
    management command invocation to run the system checks framework. If a
    typer ``@command`` named ``check`` shadows that method, Django ends up
    invoking the typer wrapper with the ``typer.Option`` descriptor in
    place of the resolved value — every other env subcommand
    (``show`` / ``set-var`` / ``unset`` / ``overrides``) raises a
    ``TypeError: ... not 'OptionInfo'`` before the typer dispatcher gets
    to run. The check subcommand must be exposed under a name that does
    not collide with Django's reserved method.
    """

    def test_django_system_check_does_not_invoke_typer_check_subcommand(self) -> None:
        """``BaseCommand.execute`` calls ``self.check()`` with no kwargs.

        Pre-fix the env command exposed a typer subcommand named ``check``
        whose Python method shadowed Django's reserved
        ``BaseCommand.check`` method. Calling ``cmd.check()`` (as Django
        does on every command invocation) hit the typer wrapper with the
        ``typer.Option`` descriptor as the ``path`` value, raising
        ``TypeError`` from ``Path(OptionInfo(...))``.
        """
        from teatree.core.management.commands.env import Command  # noqa: PLC0415

        # Django's system-checks framework calls this with app_configs/tags
        # kwargs — never with our typer path/format kwargs. Must not raise.
        Command().check()


class TestEnvCheck(TestCase):
    def test_check_in_sync(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        out = StringIO()
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.detect_drift", return_value=(False, "/tmp/cache")),
        ):
            result = call_command("env", "check", "--path", "/tmp/wt/repo", stdout=out)
        assert result == 0
        assert "env cache in sync with DB" in out.getvalue()

    def test_check_drifted(self) -> None:
        ticket = _make_ticket()
        wt = _make_worktree(ticket)
        err = StringIO()
        with (
            patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
            patch("teatree.core.management.commands.env.detect_drift", return_value=(True, "/tmp/cache")),
        ):
            result = call_command("env", "check", "--path", "/tmp/wt/repo", stderr=err)
        assert result == 1
        assert "env cache stale at /tmp/cache" in err.getvalue()


class TestEnvMigrateSecrets(TestCase):
    """The ``migrate-secrets`` subcommand moves literals into ``pass`` and refreshes the cache.

    These tests provision a real ticket + worktree row, write a tiny
    ``.t3-env.cache`` to ``tmp_path``, and patch only the ``pass`` boundary
    (writing to a fake password store). Verifies that the literal disappears
    from the regenerated cache and that the canonical pass key is stored.
    """

    def test_single_worktree_migration_writes_to_pass_and_regenerates_cache(self) -> None:
        from tempfile import TemporaryDirectory  # noqa: PLC0415

        from teatree.core.worktree.worktree_env import CACHE_DIRNAME, CACHE_FILENAME  # noqa: PLC0415

        with TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp) / "ticket-42"
            ticket_dir.mkdir()
            wt_path = ticket_dir / "backend"
            wt_path.mkdir()
            cache_dir = ticket_dir / CACHE_DIRNAME / wt_path.name
            cache_dir.mkdir(parents=True)
            cache_file = cache_dir / CACHE_FILENAME
            cache_file.write_text("FOO=bar\nPOSTGRES_PASSWORD=swordfish\n", encoding="utf-8")

            ticket = Ticket.objects.create(overlay="teatree", issue_url="https://example.com/issues/42")
            wt = Worktree.objects.create(
                overlay="teatree",
                ticket=ticket,
                repo_path="backend",
                branch="ac-test",
                db_name="wt_42",
                extra={"worktree_path": str(wt_path)},
            )

            stored: dict[str, str] = {}

            def _fake_write_pass(key: str, value: str) -> bool:
                stored[key] = value
                return True

            with (
                patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
                patch("teatree.utils.secrets.write_pass", side_effect=_fake_write_pass),
            ):
                call_command("env", "migrate-secrets", "--path", str(wt_path))

            # Pass key is ticket-pk-scoped (canonical, unique), not ticket_number.
            assert stored == {f"teatree/wt/{wt.ticket_id}/postgres": "swordfish"}
            new_body = cache_file.read_text(encoding="utf-8")
            assert "POSTGRES_PASSWORD=swordfish" not in new_body
            assert f"POSTGRES_PASSWORD_PASS_KEY=teatree/wt/{wt.ticket_id}/postgres" in new_body

    def test_reports_already_migrated_when_no_literal_present(self) -> None:
        from tempfile import TemporaryDirectory  # noqa: PLC0415

        from teatree.core.worktree.worktree_env import CACHE_DIRNAME, CACHE_FILENAME  # noqa: PLC0415

        with TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp) / "ticket-7"
            ticket_dir.mkdir()
            wt_path = ticket_dir / "backend"
            wt_path.mkdir()
            cache_dir = ticket_dir / CACHE_DIRNAME / wt_path.name
            cache_dir.mkdir(parents=True)
            cache_file = cache_dir / CACHE_FILENAME
            cache_file.write_text(
                "FOO=bar\nPOSTGRES_PASSWORD_PASS_KEY=teatree/wt/7/postgres\n",
                encoding="utf-8",
            )

            ticket = Ticket.objects.create(overlay="teatree", issue_url="https://example.com/issues/7")
            wt = Worktree.objects.create(
                overlay="teatree",
                ticket=ticket,
                repo_path="backend",
                branch="ac-test",
                db_name="wt_7",
                extra={"worktree_path": str(wt_path)},
            )

            with (
                patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
                patch("teatree.utils.secrets.write_pass") as mock_write,
            ):
                call_command("env", "migrate-secrets", "--path", str(wt_path))

            mock_write.assert_not_called()

    def test_returns_nonzero_when_pass_not_available(self) -> None:
        from tempfile import TemporaryDirectory  # noqa: PLC0415

        from teatree.core.worktree.worktree_env import CACHE_DIRNAME, CACHE_FILENAME  # noqa: PLC0415

        with TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp) / "ticket-99"
            ticket_dir.mkdir()
            wt_path = ticket_dir / "backend"
            wt_path.mkdir()
            cache_dir = ticket_dir / CACHE_DIRNAME / wt_path.name
            cache_dir.mkdir(parents=True)
            cache_file = cache_dir / CACHE_FILENAME
            cache_file.write_text("POSTGRES_PASSWORD=needs-migration\n", encoding="utf-8")

            ticket = Ticket.objects.create(overlay="teatree", issue_url="https://example.com/issues/99")
            wt = Worktree.objects.create(
                overlay="teatree",
                ticket=ticket,
                repo_path="backend",
                branch="ac-test",
                db_name="wt_99",
                extra={"worktree_path": str(wt_path)},
            )

            with (
                patch("teatree.core.management.commands.env.resolve_worktree", return_value=wt),
                patch(
                    "teatree.core.management.commands.env.ensure_postgres_pass_entry",
                    side_effect=PostgresPasswordUnavailableError("pass missing"),
                ),
            ):
                # call_command returns the return value of the command's handler
                result = call_command("env", "migrate-secrets", "--path", str(wt_path))
                # The handler returns 1 on failure (non-zero exit code).
                assert result == 1
            # Literal must not be wiped — caller needs to retry once pass is configured.
            assert "POSTGRES_PASSWORD=needs-migration" in cache_file.read_text(encoding="utf-8")
