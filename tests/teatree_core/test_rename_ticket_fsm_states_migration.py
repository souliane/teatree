"""The ``0093`` data migration renames the Ticket FSM's misleading state values.

started/planned/reviewed/shipped/in_review/review_posted/retrospected each become
a name that says what it means (souliane/teatree#4779) — no dual-read shim, so
every stored row must move in this migration. ``extra['ignored_from']`` is
load-bearing: ``unignore()`` reads it and assigns it straight to ``self.state``,
bypassing FSM validation, so a stale pre-rename value there must be rewritten
too or a future unignore lands on an invalid state. Anti-vacuous: dropping the
``RunPython`` leaves every row on its old value and this goes RED.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.state import StateApps
from django.test import TransactionTestCase

_BEFORE = ("core", "0092_taskattempt_taskattempt_recent_ended")
_AFTER = ("core", "0093_rename_ticket_fsm_states")

_OLD_TO_NEW = {
    "not_started": "not_started",
    "scoped": "scoped",
    "started": "work_started",
    "planned": "plan_recorded",
    "coded": "coded",
    "tested": "tested",
    "reviewed": "self_reviewed",
    "shipped": "pr_opened",
    "in_review": "review_requested",
    "merged": "merged",
    "retrospected": "retro_recorded",
    "delivered": "delivered",
    "review_posted": "review_delivered",
    "ignored": "ignored",
}
_RENAMED = tuple(old for old, new in _OLD_TO_NEW.items() if old != new)


@pytest.mark.timeout(240)
class TestRenameTicketFsmStates(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    def test_every_live_state_value_renames_forward_and_reverses_cleanly(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        old_apps = executor.loader.project_state(_BEFORE).apps
        old_ticket = old_apps.get_model("core", "Ticket")

        rows = {
            old_state: old_ticket.objects.create(
                overlay="t3-teatree",
                issue_url=f"https://example.com/issues/{i}",
                state=old_state,
            )
            for i, old_state in enumerate(_OLD_TO_NEW)
        }
        ignored_ghost = old_ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://example.com/issues/ignored-ghost",
            state="ignored",
            extra={"ignored_from": "reviewed"},
        )
        reopened_audit = old_ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://example.com/issues/reopened-audit",
            state="started",
            extra={"reopened_from": "shipped"},
        )

        executor = MigrationExecutor(connection)
        executor.migrate([_AFTER])
        new_apps = executor.loader.project_state(_AFTER).apps
        new_ticket = new_apps.get_model("core", "Ticket")

        for old_state, new_state in _OLD_TO_NEW.items():
            assert new_ticket.objects.get(pk=rows[old_state].pk).state == new_state
        assert new_ticket.objects.get(pk=ignored_ghost.pk).extra["ignored_from"] == "self_reviewed"
        assert new_ticket.objects.get(pk=reopened_audit.pk).extra["reopened_from"] == "pr_opened"

        # Reverse: every value round-trips back to what it started as.
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        reverted_apps = executor.loader.project_state(_BEFORE).apps
        reverted_ticket = reverted_apps.get_model("core", "Ticket")

        for old_state in _OLD_TO_NEW:
            assert reverted_ticket.objects.get(pk=rows[old_state].pk).state == old_state
        assert reverted_ticket.objects.get(pk=ignored_ghost.pk).extra["ignored_from"] == "reviewed"
        assert reverted_ticket.objects.get(pk=reopened_audit.pk).extra["reopened_from"] == "shipped"

    @staticmethod
    def _apps_at(target: tuple[str, str]) -> StateApps:
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return executor.loader.project_state(target).apps

    def test_transition_history_renames_forward_and_reverses_cleanly(self) -> None:
        old_apps = self._apps_at(_BEFORE)
        old_transition = old_apps.get_model("core", "TicketTransition")
        ticket = old_apps.get_model("core", "Ticket").objects.create(
            overlay="t3-teatree", issue_url="https://example.com/issues/history", state="coded"
        )
        edges = {old: old_transition.objects.create(ticket=ticket, from_state=old, to_state=old).pk for old in _RENAMED}
        mixed = old_transition.objects.create(ticket=ticket, from_state="scoped", to_state="started").pk

        new_transition = self._apps_at(_AFTER).get_model("core", "TicketTransition")
        assert not new_transition.objects.filter(from_state__in=_RENAMED).exists()
        assert not new_transition.objects.filter(to_state__in=_RENAMED).exists()
        for old, pk in edges.items():
            assert new_transition.objects.filter(pk=pk, from_state=_OLD_TO_NEW[old], to_state=_OLD_TO_NEW[old]).exists()
        assert new_transition.objects.filter(pk=mixed, from_state="scoped", to_state="work_started").exists()

        reverted_transition = self._apps_at(_BEFORE).get_model("core", "TicketTransition")
        for old, pk in edges.items():
            assert reverted_transition.objects.filter(pk=pk, from_state=old, to_state=old).exists()
        assert reverted_transition.objects.filter(pk=mixed, from_state="scoped", to_state="started").exists()
