"""Followup / full-status / daily must dispatch via teatree-core (#1318 follow-up).

#1312's first fix routed ``review-request discover/check/post`` through
:func:`managepy_core`, but ``full-status``, ``daily``, and every
``followup`` subcommand still went through :func:`managepy` — which prefers
the overlay's own ``manage.py`` whenever one exists. From an overlay clone
whose ``manage.py`` runs against its own settings module (no ``followup``
management command), every one of these commands crashed exactly the same
way ``review-request`` did before #1312.

These tests pin the contract: each of the four entry points emits a
``python -m teatree`` invocation (the teatree-core dispatch path),
regardless of any resolved project path — including the overlay-clone
case where the project path DOES contain a ``manage.py``, which is the
exact shape that crashed the bug.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.overlay import DJANGO_GROUPS, OverlayAppBuilder


@pytest.fixture
def overlay_clone_path(tmp_path: Path) -> Path:
    """An overlay clone with its own ``manage.py`` — the bug's reproduction shape.

    :func:`managepy` prefers ``uv --directory <path> run python manage.py``
    whenever the resolved path contains a ``manage.py``. Before #1318's
    follow-up fix, the followup group + ``full-status``/``daily``
    shortcuts went through that branch and crashed because the overlay's
    ``manage.py`` ran against settings that don't register the
    ``followup`` management command.
    """
    (tmp_path / "manage.py").write_text("# stub overlay manage.py\n", encoding="utf-8")
    return tmp_path


def _build_overlay_app(project_path: Path) -> typer.Typer:
    """Build a fully-configured overlay Typer app for the test runner."""
    return OverlayAppBuilder(overlay_name="acme", project_path=project_path).build()


class TestFollowupGroupCoreDispatch:
    """``followup refresh/sync/discover-mrs/remind`` route through ``python -m teatree``."""

    def test_followup_group_is_marked_core_dispatch(self) -> None:
        """``DJANGO_GROUPS['followup']`` carries the ``core_dispatch`` marker."""
        entry = DJANGO_GROUPS["followup"]
        assert getattr(entry, "core_dispatch", False) is True, (
            f"followup group must opt into core dispatch (#1318), got {entry!r}"
        )

    def test_followup_refresh_uses_core_dispatch(self, overlay_clone_path: Path) -> None:
        runner = CliRunner()
        app = _build_overlay_app(overlay_clone_path)
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["followup", "refresh"])
        assert result.exit_code == 0, result.output
        assert run_streamed.called
        cmd = run_streamed.call_args.args[0]
        assert "-m" in cmd, f"followup refresh must dispatch via python -m teatree, got {cmd!r}"
        assert "teatree" in cmd, f"followup refresh must dispatch via python -m teatree, got {cmd!r}"
        assert "manage.py" not in " ".join(cmd), f"followup refresh must NOT route through manage.py, got {cmd!r}"

    def test_followup_sync_uses_core_dispatch(self, overlay_clone_path: Path) -> None:
        runner = CliRunner()
        app = _build_overlay_app(overlay_clone_path)
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["followup", "sync"])
        assert result.exit_code == 0, result.output
        cmd = run_streamed.call_args.args[0]
        assert "-m" in cmd, f"followup sync must use core dispatch, got {cmd!r}"
        assert "teatree" in cmd, f"followup sync must use core dispatch, got {cmd!r}"
        assert "manage.py" not in " ".join(cmd)

    def test_followup_discover_mrs_uses_core_dispatch(self, overlay_clone_path: Path) -> None:
        runner = CliRunner()
        app = _build_overlay_app(overlay_clone_path)
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["followup", "discover-mrs"])
        assert result.exit_code == 0, result.output
        cmd = run_streamed.call_args.args[0]
        assert "-m" in cmd, f"followup discover-mrs must use core dispatch, got {cmd!r}"
        assert "teatree" in cmd, f"followup discover-mrs must use core dispatch, got {cmd!r}"
        assert "manage.py" not in " ".join(cmd)

    def test_followup_remind_uses_core_dispatch(self, overlay_clone_path: Path) -> None:
        runner = CliRunner()
        app = _build_overlay_app(overlay_clone_path)
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["followup", "remind"])
        assert result.exit_code == 0, result.output
        cmd = run_streamed.call_args.args[0]
        assert "-m" in cmd, f"followup remind must use core dispatch, got {cmd!r}"
        assert "teatree" in cmd, f"followup remind must use core dispatch, got {cmd!r}"
        assert "manage.py" not in " ".join(cmd)


class TestTicketGroupCoreDispatch:
    """``ticket clear/merge`` route through ``python -m teatree`` (core), not the overlay manage.py.

    ``ticket`` subcommands live in ``teatree.core.management.commands.ticket``
    (delegating to ``teatree.core.merge``) — teatree CORE, not any
    overlay-owned ``manage.py``. A non-core overlay clone has its own
    ``manage.py`` with no ``ticket`` command, so without the ``core_dispatch``
    marker the sanctioned merge path crashes with ``Unknown command: 'ticket'``
    — the same #1312/#1318 lockout class as ``followup`` and ``review-request``.
    """

    def test_ticket_group_is_marked_core_dispatch(self) -> None:
        """``DJANGO_GROUPS['ticket']`` carries the ``core_dispatch`` marker."""
        entry = DJANGO_GROUPS["ticket"]
        assert getattr(entry, "core_dispatch", False) is True, (
            f"ticket group must opt into core dispatch — its clear/merge commands live in "
            f"teatree.core, not an overlay manage.py, got {entry!r}"
        )

    def test_ticket_clear_uses_core_dispatch(self, overlay_clone_path: Path) -> None:
        """``t3 <overlay> ticket clear`` must dispatch to core, never the overlay manage.py."""
        runner = CliRunner()
        app = _build_overlay_app(overlay_clone_path)
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["ticket", "clear", "15"])
        assert result.exit_code == 0, result.output
        assert run_streamed.called
        cmd = run_streamed.call_args.args[0]
        assert "-m" in cmd, f"ticket clear must dispatch via python -m teatree, got {cmd!r}"
        assert "teatree" in cmd, f"ticket clear must dispatch via python -m teatree, got {cmd!r}"
        assert "manage.py" not in " ".join(cmd), f"ticket clear must NOT route through manage.py, got {cmd!r}"
        assert "ticket" in cmd
        assert "clear" in cmd

    def test_ticket_merge_uses_core_dispatch(self, overlay_clone_path: Path) -> None:
        """``t3 <overlay> ticket merge`` must dispatch to core, never the overlay manage.py."""
        runner = CliRunner()
        app = _build_overlay_app(overlay_clone_path)
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["ticket", "merge", "abc123"])
        assert result.exit_code == 0, result.output
        cmd = run_streamed.call_args.args[0]
        assert "-m" in cmd, f"ticket merge must use core dispatch, got {cmd!r}"
        assert "teatree" in cmd, f"ticket merge must use core dispatch, got {cmd!r}"
        assert "manage.py" not in " ".join(cmd), f"ticket merge must NOT route through manage.py, got {cmd!r}"
        assert "ticket" in cmd
        assert "merge" in cmd


class TestPrGroupCoreDispatch:
    """``pr create`` routes through ``python -m teatree`` (core), not the overlay manage.py.

    ``pr`` subcommands live in ``teatree.core.management.commands.pr`` —
    teatree CORE — and ``create`` gate-validates against the teatree-core
    control DB the shipping gate reads
    (``teatree.core.provision.db_anchor.assert_lifecycle_db_is_canonical``). Without the
    ``core_dispatch`` marker, the overlay project-path resolver prefers any
    ``manage.py`` discovered from the invoking cwd — from inside a ticket
    worktree that is the worktree's OWN ``manage.py``, running against its
    per-worktree auto-isolated DB the gate never consults
    (souliane/teatree#2925, the same #126 class as ``db migrate``/``db approve``).
    """

    def test_pr_group_is_marked_core_dispatch(self) -> None:
        """``DJANGO_GROUPS['pr']`` carries the ``core_dispatch`` marker."""
        entry = DJANGO_GROUPS["pr"]
        assert getattr(entry, "core_dispatch", False) is True, (
            f"pr group must opt into core dispatch (#2925) — its create command reads/writes "
            f"the teatree-core control DB, not an overlay manage.py, got {entry!r}"
        )

    def test_pr_create_uses_core_dispatch(self, overlay_clone_path: Path) -> None:
        """``t3 <overlay> pr create`` must dispatch to core, never the overlay manage.py."""
        runner = CliRunner()
        app = _build_overlay_app(overlay_clone_path)
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["pr", "create", "15"])
        assert result.exit_code == 0, result.output
        assert run_streamed.called
        cmd = run_streamed.call_args.args[0]
        assert "-m" in cmd, f"pr create must dispatch via python -m teatree, got {cmd!r}"
        assert "teatree" in cmd, f"pr create must dispatch via python -m teatree, got {cmd!r}"
        assert "manage.py" not in " ".join(cmd), f"pr create must NOT route through manage.py, got {cmd!r}"
        assert "pr" in cmd
        assert "create" in cmd


class TestLifecycleGroupCoreDispatch:
    """``lifecycle visit-phase`` routes through ``python -m teatree`` (core), not the overlay manage.py.

    ``lifecycle`` subcommands live in ``teatree.core.management.commands.lifecycle``
    — teatree CORE — and every one of them records a phase attestation against
    the teatree-core control DB the shipping gate reads via
    ``assert_lifecycle_db_is_canonical``. Same #2925 reasoning as ``pr`` above.
    """

    def test_lifecycle_group_is_marked_core_dispatch(self) -> None:
        """``DJANGO_GROUPS['lifecycle']`` carries the ``core_dispatch`` marker."""
        entry = DJANGO_GROUPS["lifecycle"]
        assert getattr(entry, "core_dispatch", False) is True, (
            f"lifecycle group must opt into core dispatch (#2925) — its subcommands record "
            f"phase attestations against the teatree-core control DB, got {entry!r}"
        )

    def test_lifecycle_visit_phase_uses_core_dispatch(self, overlay_clone_path: Path) -> None:
        """``t3 <overlay> lifecycle visit-phase`` must dispatch to core, never the overlay manage.py."""
        runner = CliRunner()
        app = _build_overlay_app(overlay_clone_path)
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["lifecycle", "visit-phase", "15", "testing"])
        assert result.exit_code == 0, result.output
        assert run_streamed.called
        cmd = run_streamed.call_args.args[0]
        assert "-m" in cmd, f"lifecycle visit-phase must dispatch via python -m teatree, got {cmd!r}"
        assert "teatree" in cmd, f"lifecycle visit-phase must dispatch via python -m teatree, got {cmd!r}"
        assert "manage.py" not in " ".join(cmd), f"lifecycle visit-phase must NOT route through manage.py, got {cmd!r}"
        assert "lifecycle" in cmd
        assert "visit-phase" in cmd


class TestDbMigrateDispatch:
    """``db migrate`` runs where the active overlay's Django app is installed.

    ``db migrate`` must apply BOTH the teatree-core migrations AND the active
    overlay's own app migrations against the same gate-resolved control DB.
    An overlay that ships its own Django app lists it in that overlay's own
    settings module (``INSTALLED_APPS``); the teatree-core settings cannot see
    it, because ``teatree.settings._discover_overlay_apps`` imports the overlay
    class at settings-bootstrap time and that import raises before the app
    registry is ready (or on a missing overlay dependency), so the overlay app
    is silently dropped and its migrations are structurally invisible on the
    ``python -m teatree`` path.

    So when the overlay ships its own settings module, ``db migrate`` routes
    through the overlay's ``manage.py`` (its settings list the overlay app
    explicitly and resolve the same canonical control DB). When the overlay
    runs on the base ``teatree.settings`` (or ships no project dir), there is
    nothing extra to load, so it stays on the in-process core path (#126). The
    sibling ``db`` subcommands (``refresh``/``restore-ci``/``reset-passwords``)
    always route through the overlay ``manage.py`` for the ``db_import``
    strategy; ``db approve`` always core-dispatches (it records a ``DbApproval``
    in the core control DB and needs no overlay app).
    """

    def test_db_migrate_marked_overlay_settings_not_core(self) -> None:
        entry = DJANGO_GROUPS["db"]
        assert "migrate" in getattr(entry, "overlay_settings_subcommands", frozenset()), (
            f"db.migrate must opt into overlay-settings dispatch so an overlay-shipped app "
            f"migration is applied, got {entry!r}"
        )
        # migrate must NOT be core-only — that path omits the overlay app.
        assert "migrate" not in getattr(entry, "core_subcommands", frozenset())
        # The group itself must NOT be wholesale core_dispatch — refresh et al.
        # still need the overlay manage.py.
        assert getattr(entry, "core_dispatch", False) is False

    def test_db_migrate_routes_to_overlay_settings_when_overlay_ships_own(self, overlay_clone_path: Path) -> None:
        # An overlay with its own settings module (its INSTALLED_APPS list the
        # overlay app) must run migrate through the overlay manage.py, so the
        # overlay-shipped migration is applied. On the pre-fix core path this
        # ran ``python -m teatree`` against teatree.settings, where the overlay
        # app is structurally invisible and its migration silently skipped.
        runner = CliRunner()
        app = OverlayAppBuilder("acme", overlay_clone_path, settings_module="acme.settings").build()
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["db", "migrate"])
        assert result.exit_code == 0, result.output
        cmd = run_streamed.call_args.args[0]
        assert "manage.py" in " ".join(cmd), (
            f"db migrate must route through the overlay manage.py when the overlay ships its "
            f"own settings module, so the overlay app's migration is applied, got {cmd!r}"
        )
        assert "db" in cmd
        assert "migrate" in cmd

    def test_db_migrate_stays_core_on_base_teatree_settings(self, overlay_clone_path: Path) -> None:
        # An overlay on the base teatree.settings gains nothing from the overlay
        # manage.py context (its INSTALLED_APPS are the core ones), so migrate
        # keeps the in-process core path (#126) — no regression to a
        # ``uv --directory`` subprocess.
        runner = CliRunner()
        app = OverlayAppBuilder("acme", overlay_clone_path, settings_module="teatree.settings").build()
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["db", "migrate"])
        assert result.exit_code == 0, result.output
        cmd = run_streamed.call_args.args[0]
        assert "-m" in cmd, f"db migrate on base settings must dispatch via python -m teatree, got {cmd!r}"
        assert "teatree" in cmd, f"db migrate on base settings must dispatch via python -m teatree, got {cmd!r}"
        assert "manage.py" not in " ".join(cmd), (
            f"db migrate on base teatree.settings must NOT route through manage.py, got {cmd!r}"
        )
        assert "db" in cmd
        assert "migrate" in cmd

    def test_db_refresh_still_uses_overlay_manage_py(self, overlay_clone_path: Path) -> None:
        # The sibling stays on the overlay manage.py path — only migrate is
        # core-dispatched.
        runner = CliRunner()
        app = _build_overlay_app(overlay_clone_path)
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["db", "refresh"])
        assert result.exit_code == 0, result.output
        cmd = run_streamed.call_args.args[0]
        assert "manage.py" in " ".join(cmd), f"db refresh must route through the overlay manage.py, got {cmd!r}"

    def test_db_approve_is_surfaced_and_core_dispatched(self) -> None:
        # The #953 ``db approve`` command (records a DbApproval in the
        # teatree-core control DB) must be both listed as a ``db`` subcommand
        # AND opt into per-subcommand core dispatch — otherwise ``t3 <overlay>
        # db approve`` returns "No such command 'approve'" (the gap this test
        # pins) and a recorded approval never reaches the gate's control DB.
        entry = DJANGO_GROUPS["db"]
        sub_names = {name for name, _ in entry.subcommands}
        assert "approve" in sub_names, f"db.approve must be a listed db subcommand (#953), got {sorted(sub_names)}"
        assert "approve" in getattr(entry, "core_subcommands", frozenset()), (
            f"db.approve must opt into per-subcommand core dispatch (#953/#126), got {entry!r}"
        )

    def test_db_approve_uses_core_dispatch(self, overlay_clone_path: Path) -> None:
        runner = CliRunner()
        app = _build_overlay_app(overlay_clone_path)
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["db", "approve", "fresh-dump", "acme-tenant", "--approver", "souliane"])
        assert result.exit_code == 0, result.output
        cmd = run_streamed.call_args.args[0]
        assert "-m" in cmd, f"db approve must dispatch via python -m teatree, got {cmd!r}"
        assert "teatree" in cmd, f"db approve must dispatch via python -m teatree, got {cmd!r}"
        assert "manage.py" not in " ".join(cmd), f"db approve must NOT route through manage.py, got {cmd!r}"
        assert "db" in cmd
        assert "approve" in cmd


class TestShortcutCoreDispatch:
    """``full-status`` and ``daily`` shortcut to followup commands — same dispatch rule."""

    def test_full_status_uses_core_dispatch(self, overlay_clone_path: Path) -> None:
        """``t3 <overlay> full-status`` must reach ``python -m teatree followup refresh``."""
        runner = CliRunner()
        app = _build_overlay_app(overlay_clone_path)
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["full-status"])
        assert result.exit_code == 0, result.output
        cmd = run_streamed.call_args.args[0]
        assert "-m" in cmd, f"full-status must use core dispatch, got {cmd!r}"
        assert "teatree" in cmd, f"full-status must use core dispatch, got {cmd!r}"
        assert "manage.py" not in " ".join(cmd)
        assert "followup" in cmd
        assert "refresh" in cmd

    def test_daily_uses_core_dispatch(self, overlay_clone_path: Path) -> None:
        """``t3 <overlay> daily`` must reach ``python -m teatree followup sync``."""
        runner = CliRunner()
        app = _build_overlay_app(overlay_clone_path)
        with patch("teatree.cli.overlay.run_streamed") as run_streamed:
            result = runner.invoke(app, ["daily"])
        assert result.exit_code == 0, result.output
        cmd = run_streamed.call_args.args[0]
        assert "-m" in cmd, f"daily must use core dispatch, got {cmd!r}"
        assert "teatree" in cmd, f"daily must use core dispatch, got {cmd!r}"
        assert "manage.py" not in " ".join(cmd)
        assert "followup" in cmd
        assert "sync" in cmd
