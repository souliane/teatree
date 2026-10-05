"""One-time database conversions preserve current rows while retiring old formats."""

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from teatree.core.models import ConfigSetting, ConsolidatedMemory, Session, Ticket, Worktree

_MIGRATION = importlib.import_module("teatree.core.migrations.0125_convert_stored_data_to_current_formats")


class TestUnattributedWorkOverlayMigration(TransactionTestCase):
    def test_multiple_unattributed_work_rows_without_an_overlay_setting(self) -> None:
        monkeypatch = pytest.MonkeyPatch()
        self.addCleanup(monkeypatch.undo)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        before = ("core", "0124_delete_rows_of_retired_settings")
        after = ("core", "0125_convert_stored_data_to_current_formats")
        head = ("core", "0127_remove_session_repo_ledgers")
        executor = MigrationExecutor(connection)
        executor.migrate([before])
        try:
            historical = executor.loader.project_state([before]).apps
            historical.get_model("core", "ConfigSetting").objects.update_or_create(
                scope="", key="overlays", defaults={"value": {"t3-teatree": {}, "t3-acme": {}}}
            )
            ticket_model = historical.get_model("core", "Ticket")
            session_model = historical.get_model("core", "Session")
            worktree_model = historical.get_model("core", "Worktree")
            tickets = [
                ticket_model.objects.create(overlay="", issue_url=f"https://example.com/unknown/issues/{number}")
                for number in range(2)
            ]
            sessions = [session_model.objects.create(ticket=ticket, overlay="") for ticket in tickets]
            worktrees = [
                worktree_model.objects.create(
                    ticket=ticket, overlay="", repo_path=f"unknown-{number}", branch="feature"
                )
                for number, ticket in enumerate(tickets)
            ]

            with self.assertLogs(_MIGRATION.__name__, level="INFO") as captured:
                MigrationExecutor(connection).migrate([after])

            migrated = MigrationExecutor(connection).loader.project_state([after]).apps
            for model_name, rows in (("Ticket", tickets), ("Session", sessions), ("Worktree", worktrees)):
                model = migrated.get_model("core", model_name)
                assert all(model.objects.get(pk=row.pk).overlay == "" for row in rows)
            assert any("6 unresolved work overlay row(s) left untouched" in line for line in captured.output)
        finally:
            MigrationExecutor(connection).migrate([head])

    def test_multiple_unattributed_rows_all_gain_the_single_registered_overlay(self) -> None:
        monkeypatch = pytest.MonkeyPatch()
        self.addCleanup(monkeypatch.undo)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        before = ("core", "0124_delete_rows_of_retired_settings")
        after = ("core", "0125_convert_stored_data_to_current_formats")
        head = ("core", "0127_remove_session_repo_ledgers")
        executor = MigrationExecutor(connection)
        executor.migrate([before])
        try:
            historical = executor.loader.project_state([before]).apps
            historical.get_model("core", "ConfigSetting").objects.update_or_create(
                scope="", key="overlays", defaults={"value": {"t3-teatree": {}}}
            )
            tickets = [
                historical.get_model("core", "Ticket").objects.create(
                    overlay="", issue_url=f"https://example.com/unknown/issues/{number}"
                )
                for number in range(2)
            ]
            sessions = [
                historical.get_model("core", "Session").objects.create(ticket=ticket, overlay="") for ticket in tickets
            ]
            worktrees = [
                historical.get_model("core", "Worktree").objects.create(
                    ticket=ticket, overlay="", repo_path=f"unknown-{number}", branch="feature"
                )
                for number, ticket in enumerate(tickets)
            ]
            MigrationExecutor(connection).migrate([after])
            migrated = MigrationExecutor(connection).loader.project_state([after]).apps
            for model_name, rows in (("Ticket", tickets), ("Session", sessions), ("Worktree", worktrees)):
                model = migrated.get_model("core", model_name)
                assert all(model.objects.get(pk=row.pk).overlay == "t3-teatree" for row in rows)
        finally:
            MigrationExecutor(connection).migrate([head])


class TestCanonicalizeWipSettings(TestCase):
    def test_old_key_and_value_move_without_overwriting_a_current_row(self) -> None:
        ConfigSetting.objects.create(
            scope="alpha", key="speed", value="high", seeded_by="entrypoint", seed_value="high", written_by="owner"
        )
        ConfigSetting.objects.create(scope="beta", key="speed", value="low")
        ConfigSetting.objects.create(scope="beta", key="wip", value="normal")
        ConfigSetting.objects.create(scope="gamma", key="wip", value="full", seed_value="full")

        _MIGRATION.canonicalize_wip(apps, SimpleNamespace(connection=connection))
        _MIGRATION.canonicalize_wip(apps, SimpleNamespace(connection=connection))

        alpha = ConfigSetting.objects.get(scope="alpha", key="wip")
        assert (alpha.value, alpha.seed_value, alpha.seeded_by, alpha.written_by) == (
            "full",
            "full",
            "entrypoint",
            "owner",
        )
        assert ConfigSetting.objects.get(scope="beta", key="wip").value == "medium"
        gamma = ConfigSetting.objects.get(scope="gamma", key="wip")
        assert (gamma.value, gamma.seed_value) == ("full", "full")
        assert not ConfigSetting.objects.filter(key="speed").exists()

    def test_session_phase_json_is_canonicalized(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        session = Session.objects.create(ticket=ticket, overlay="t3-teatree")
        Session.objects.filter(pk=session.pk).update(
            visited_phases=["test", "testing", "review"],
            phase_visits={"review": {"agent_id": "old"}, "reviewing": {"agent_id": "current"}},
        )

        _MIGRATION.canonicalize_session_phases(apps, SimpleNamespace(connection=connection))

        session.refresh_from_db()
        assert session.visited_phases == ["testing", "reviewing"]
        assert session.phase_visits == {"reviewing": {"agent_id": "current"}}

    def test_current_session_phase_json_is_untouched(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        session = Session.objects.create(
            ticket=ticket,
            overlay="t3-teatree",
            visited_phases=["testing"],
            phase_visits={"testing": {"agent_id": "current"}},
        )
        before = (session.visited_phases, session.phase_visits)

        _MIGRATION.canonicalize_session_phases(apps, SimpleNamespace(connection=connection))
        _MIGRATION.canonicalize_session_phases(apps, SimpleNamespace(connection=connection))

        session.refresh_from_db()
        assert (session.visited_phases, session.phase_visits) == before

    def test_malformed_session_phase_row_is_left_untouched(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        session = Session.objects.create(ticket=ticket, overlay="t3-teatree")
        Session.objects.filter(pk=session.pk).update(visited_phases={"test": True})
        with self.assertLogs(_MIGRATION.__name__, level="INFO") as captured:
            _MIGRATION.canonicalize_session_phases(apps, SimpleNamespace(connection=connection))

        session.refresh_from_db()
        assert session.visited_phases == {"test": True}
        assert any("1 unresolved session phase row(s) left untouched" in line for line in captured.output)


class TestStoredFormatConversions(TestCase):
    def _schema(self) -> SimpleNamespace:
        return SimpleNamespace(connection=connection)

    def test_overlay_names_convert_in_registry_scope_and_columns_once(self) -> None:
        registry = ConfigSetting.objects.create(
            key="overlays", value={"teatree": {"slack_user_id": "U1"}, "t3-teatree": {"path": "/repo"}}
        )
        scoped = ConfigSetting.objects.create(scope="teatree", key="wip", value="slow")
        current = Ticket.objects.create(overlay="t3-teatree")
        old = Ticket.objects.create(overlay="teatree")
        _MIGRATION.convert_overlay_names(apps, self._schema())
        _MIGRATION.convert_overlay_names(apps, self._schema())
        registry.refresh_from_db()
        scoped.refresh_from_db()
        old.refresh_from_db()
        current.refresh_from_db()
        assert registry.value == {"t3-teatree": {"slack_user_id": "U1", "path": "/repo"}}
        assert scoped.scope == old.overlay == current.overlay == "t3-teatree"

    def test_list_scope_collision_merges_in_order(self) -> None:
        short = ConfigSetting.objects.create(scope="teatree", key="private_repos", value=["b", "a"])
        target = ConfigSetting.objects.create(scope="t3-teatree", key="private_repos", value=["a", "c"])

        _MIGRATION.convert_overlay_names(apps, self._schema())
        _MIGRATION.convert_overlay_names(apps, self._schema())

        assert not ConfigSetting.objects.filter(pk=short.pk).exists()
        assert ConfigSetting.objects.get(pk=target.pk).value == ["a", "c", "b"]

    def test_non_list_scope_collision_keeps_both_rows_and_counts(self) -> None:
        short = ConfigSetting.objects.create(scope="teatree", key="wip", value="slow")
        target = ConfigSetting.objects.create(scope="t3-teatree", key="wip", value="full")

        with self.assertLogs(_MIGRATION.__name__, level="INFO") as captured:
            _MIGRATION.convert_overlay_names(apps, self._schema())

        assert ConfigSetting.objects.get(pk=short.pk).value == "slow"
        assert ConfigSetting.objects.get(scope="t3-teatree", key="wip").pk == target.pk
        assert ConfigSetting.objects.get(pk=target.pk).value == "full"
        assert any("1 unresolved setting scope collision row(s)" in line for line in captured.output)

    def test_scalar_skill_policy_becomes_floor_list_and_routes_stay_untouched(self) -> None:
        routes = [{"harness": "codex_app_server", "model": "codex"}]
        row = ConfigSetting.objects.create(
            key="agent_skill_models", value={"review": "opus", "code": routes, "inherit": "inherit"}
        )
        _MIGRATION.convert_agent_skill_models(apps, self._schema())
        row.refresh_from_db()
        expected = {"review": [{"floor": "opus"}], "code": routes, "inherit": []}
        assert row.value == expected
        _MIGRATION.convert_agent_skill_models(apps, self._schema())
        row.refresh_from_db()
        assert row.value == expected

    def test_blank_work_overlays_inherit_ticket_and_current_values_remain(self) -> None:
        ConfigSetting.objects.create(key="overlays", value={"t3-teatree": {}})
        old = Ticket.objects.create(overlay="")
        current = Ticket.objects.create(overlay="t3-teatree")
        session = Session.objects.create(ticket=old, overlay="")
        worktree = Worktree.objects.create(ticket=old, overlay="", repo_path="backend", branch="feature")
        _MIGRATION.convert_blank_work_overlays(apps, self._schema())
        _MIGRATION.convert_blank_work_overlays(apps, self._schema())
        for row in (old, current, session, worktree):
            row.refresh_from_db()
            assert row.overlay == "t3-teatree"

    def test_blank_ticket_without_linked_rows_uses_single_configured_overlay(self) -> None:
        ConfigSetting.objects.create(key="overlays", value={"t3-acme": {}})
        ticket = Ticket.objects.create(overlay="")

        _MIGRATION.convert_blank_work_overlays(apps, self._schema())

        ticket.refresh_from_db()
        assert ticket.overlay == "t3-acme"

    def test_blank_ticket_uses_gitlab_repo_scope_with_two_overlays(self) -> None:
        ConfigSetting.objects.create(
            key="overlays",
            value={"t3-teatree": {"workspace_repos": ["teatree"]}, "t3-acme": {"workspace_repos": ["sample-app"]}},
        )
        ticket = Ticket.objects.create(
            overlay="", issue_url="https://gitlab.example.com/example-org/sample-app/-/issues/42"
        )

        _MIGRATION.convert_blank_work_overlays(apps, self._schema())

        ticket.refresh_from_db()
        assert ticket.overlay == "t3-acme"

    def test_blank_ticket_without_repo_scope_stays_untouched_when_ambiguous(self) -> None:
        ticket = Ticket.objects.create(overlay="", issue_url="https://example.com/unknown/issues/42")
        with (
            patch.dict("os.environ", {"T3_OVERLAY_NAME": "t3-teatree"}),
            self.assertLogs(_MIGRATION.__name__, level="INFO") as captured,
        ):
            _MIGRATION.convert_blank_work_overlays(apps, self._schema())

        ticket.refresh_from_db()
        assert ticket.overlay == ""
        assert any("1 unresolved work overlay row(s) left untouched" in line for line in captured.output)

    def test_blank_session_without_ticket_or_registry_stays_unresolved(self) -> None:
        session = SimpleNamespace(pk=1, ticket_id=None, overlay="")
        session_rows = MagicMock()
        session_rows.filter.return_value.iterator.return_value = iter([session])
        session_model = SimpleNamespace(objects=SimpleNamespace(using=lambda alias: session_rows))
        get_model = apps.get_model
        with (
            patch.object(
                apps,
                "get_model",
                side_effect=lambda app, name: session_model if name == "Session" else get_model(app, name),
            ),
            self.assertLogs(_MIGRATION.__name__, level="INFO") as captured,
        ):
            _MIGRATION.convert_blank_work_overlays(apps, self._schema())

        assert session.overlay == ""
        session_rows.filter.return_value.update.assert_not_called()
        assert any("1 unresolved work overlay row(s) left untouched" in line for line in captured.output)

    def test_unresolvable_pr_branch_stays_untouched(self) -> None:
        url = "https://example.com/owner/repo/pull/1"
        ticket = Ticket.objects.create(overlay="t3-teatree", extra={"pr_urls": [url]})

        with self.assertLogs(_MIGRATION.__name__, level="INFO") as captured:
            _MIGRATION.convert_branch_urls(apps, self._schema())

        ticket.refresh_from_db()
        assert ticket.extra == {"pr_urls": [url]}
        assert any("1 unresolved PR branch row(s) left untouched" in line for line in captured.output)

    def test_malformed_pr_and_issue_urls_do_not_abort_conversion(self) -> None:
        malformed = "https://[broken/pull/1"
        ticket = Ticket.objects.create(overlay="", issue_url="https://example.com/owner/repo/issues/1")
        Ticket.objects.filter(pk=ticket.pk).update(issue_url="https://[broken/issues/1", extra={"pr_urls": [malformed]})
        ConfigSetting.objects.create(key="overlays", value={"t3-teatree": {}, "t3-other": {}})

        with self.assertLogs(_MIGRATION.__name__, level="INFO") as captured:
            _MIGRATION.convert_branch_urls(apps, self._schema())
            _MIGRATION.convert_blank_work_overlays(apps, self._schema())

        ticket.refresh_from_db()
        assert ticket.overlay == ""
        assert ticket.extra == {"pr_urls": [malformed]}
        assert any("1 unresolved PR branch row(s)" in line for line in captured.output)
        assert any("1 unresolved work overlay row(s)" in line for line in captured.output)

    def test_flat_identity_aliases_become_one_group_without_replacing_current_group(self) -> None:
        row = ConfigSetting.objects.create(
            key="overlays",
            value={
                "t3-teatree": {},
                "t3-acme": {"identity_aliases": [["existing"]]},
                "third": {"identity_aliases": ["old-a", "old-b"]},
            },
        )
        ConfigSetting.objects.create(key="user_identity_aliases", value=["owner", "owner-bot"])
        _MIGRATION.convert_identity_groups(apps, self._schema())
        _MIGRATION.convert_identity_groups(apps, self._schema())
        row.refresh_from_db()
        assert row.value["t3-teatree"]["identity_aliases"] == [["owner", "owner-bot"]]
        assert row.value["t3-acme"]["identity_aliases"] == [["existing"]]
        assert row.value["third"]["identity_aliases"] == [["old-a", "old-b"]]

    def test_pr_urls_gain_branch_index_without_changing_existing_index(self) -> None:
        old_url = "https://example.com/owner/repo/pull/1"
        current_url = "https://example.com/owner/repo/pull/2"
        old = Ticket.objects.create(overlay="t3-teatree", extra={"pr_urls": [old_url], "branch": "feature"})
        current = Ticket.objects.create(
            overlay="t3-teatree", extra={"pr_urls": [current_url], "pr_url_by_branch": {"new": current_url}}
        )
        _MIGRATION.convert_branch_urls(apps, self._schema())
        _MIGRATION.convert_branch_urls(apps, self._schema())
        old.refresh_from_db()
        current.refresh_from_db()
        assert old.extra["pr_url_by_branch"] == {"feature": old_url}
        assert current.extra["pr_url_by_branch"] == {"new": current_url}

    def test_github_pr_url_uses_the_matching_worktree_branch(self) -> None:
        url = "https://github.com/owner/repo/pull/1"
        ticket = Ticket.objects.create(overlay="t3-teatree", extra={"pr_urls": [url]})
        Worktree.objects.create(ticket=ticket, overlay="t3-teatree", repo_path="owner/repo", branch="feature")

        _MIGRATION.convert_branch_urls(apps, self._schema())

        ticket.refresh_from_db()
        assert ticket.extra["pr_url_by_branch"] == {"feature": url}

    def test_single_gap_ticket_and_anchor_become_batch_state_once(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            short_description="Fix gap",
            extra={"dream_gap_key": "gap-1", "dream_memory_cluster_key": "cluster-1"},
        )
        memory = ConsolidatedMemory.objects.create(
            cluster_key="cluster-1",
            rule="Rule",
            member_count=1,
            max_member_weight=1,
            ticket_url="https://example.com/umbrella#dream-gap=gap-1",
        )
        _MIGRATION.convert_dream_tickets(apps, self._schema())
        _MIGRATION.convert_dream_tickets(apps, self._schema())
        ticket.refresh_from_db()
        memory.refresh_from_db()
        assert ticket.extra["dream_gap_batch"][0]["gap_key"] == "gap-1"
        assert ticket.extra["dream_gap_claimed_delivered"] == ["gap-1"]
        assert "dream_gap_key" not in ticket.extra
        assert "#dream-batch=" in memory.ticket_url

    def test_current_dream_batch_and_anchor_are_untouched(self) -> None:
        batch = [{"gap_key": "gap-1", "cluster_key": "cluster-1", "title": "Fix gap", "citation": "cited"}]
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            extra={"dream_gap_batch": batch, "dream_gap_claimed_delivered": ["gap-1"]},
        )
        url = "https://example.com/umbrella#dream-batch=abc123"
        memory = ConsolidatedMemory.objects.create(
            cluster_key="cluster-1", rule="Rule", member_count=1, max_member_weight=1, ticket_url=url
        )

        _MIGRATION.convert_dream_tickets(apps, self._schema())
        _MIGRATION.convert_dream_tickets(apps, self._schema())

        ticket.refresh_from_db()
        memory.refresh_from_db()
        assert ticket.extra == {"dream_gap_batch": batch, "dream_gap_claimed_delivered": ["gap-1"]}
        assert memory.ticket_url == url

    def test_legacy_gap_on_existing_batch_keeps_all_gaps(self) -> None:
        existing = {"gap_key": "gap-0", "cluster_key": "cluster-0", "title": "Old", "citation": "cited"}
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            short_description="New",
            extra={
                "dream_gap_key": "gap-1",
                "dream_memory_cluster_key": "cluster-1",
                "dream_gap_batch": [existing],
                "dream_gap_claimed_delivered": ["gap-0"],
            },
        )

        _MIGRATION.convert_dream_tickets(apps, self._schema())

        ticket.refresh_from_db()
        assert [entry["gap_key"] for entry in ticket.extra["dream_gap_batch"]] == ["gap-0", "gap-1"]
        assert ticket.extra["dream_gap_claimed_delivered"] == ["gap-0", "gap-1"]
        assert "dream_gap_key" not in ticket.extra
        assert "dream_memory_cluster_key" not in ticket.extra

    def test_dream_ticket_with_missing_owner_is_left_untouched(self) -> None:
        extra = {"dream_gap_key": "gap-1", "dream_gap_folded_into": 999999}
        ticket = Ticket.objects.create(overlay="t3-teatree", extra=extra)
        with self.assertLogs(_MIGRATION.__name__, level="INFO") as captured:
            _MIGRATION.convert_dream_tickets(apps, self._schema())

        ticket.refresh_from_db()
        assert ticket.extra == extra
        assert any("1 unresolved dream ticket row(s) left untouched" in line for line in captured.output)
