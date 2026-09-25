"""Upgrading a live box across the preset redesign keeps what that box actually runs.

The fixture is the deployed box's own shape: an operator-edited ``maintenance`` and
``away``, an untouched ``present``, ``off`` and ``low-token`` that disagree on
``tickets``, nine default-off loops, a forced-off ``ship``, the on-behalf dial set to
"immediate", and dream opt-ins in the nested ``loops`` table. Each assertion names a value
the pre-fix migrations changed: 0086 raised on the ``tickets`` split, the posture rewrite
replaced both edited presets and started the default-off loops under ``present``, 0109
closed the owner's voice after hours, the cadence fold left ``eval_local`` daily, and
nothing carried the dream opt-ins.

The second class runs the same upgrade with every ``ConfigSetting`` query routed to a
separate canonical store, the way ``ConfigSettingRouter`` routes it inside a worktree, and
proves the migrate never writes there.
"""

import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.apps import apps as live_apps
from django.core.management import call_command
from django.db import connection, connections
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.state import StateApps
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from teatree.config.db_router import CONFIG_MODEL_LABEL
from tests.db_alias import register_sqlite_alias, teardown_sqlite_alias

_BEFORE = ("core", "0084_red_mr_fix_attempt_review_findings")
_HOUR = 3600

_DISABLED = (
    "ci_eval_heal",
    "dm_sweep",
    "dogfood",
    "eval_local",
    "issue_disposition",
    "memory_skim",
    "outer_loop",
    "snapshot_warmer",
    "triage_assessor",
)
_ENABLED = (
    "arch_review",
    "audit",
    "backlog_sweep",
    "db_backup",
    "directive_loop",
    "dispatch",
    "dream",
    "followup",
    "housekeeping",
    "idle_stack_reaper",
    "inbox",
    "issue_implementer",
    "local_stack_queue",
    "news",
    "resource_pressure",
    "review",
    "ship",
    "tickets",
)
_DELAYS = {"eval_local": 86400, "triage_assessor": _HOUR}

_MAINTENANCE = {
    "inbox": True,
    "dispatch": True,
    "dream": True,
    "eval_local": True,
    "dogfood": True,
    "arch_review": True,
    "news": True,
    "snapshot_warmer": True,
    "housekeeping": True,
    "idle_stack_reaper": True,
    "local_stack_queue": True,
    "resource_pressure": True,
    "tickets": False,
    "ship": True,
    "review": True,
    "followup": False,
    "audit": False,
    "triage_assessor": True,
    "issue_implementer": False,
}
_MAINTENANCE_DESCRIPTION = "Nights: coding continues overnight, so merging must too."
_AWAY = {
    "inbox": True,
    "dispatch": True,
    "tickets": True,
    "ship": True,
    "review": False,
    "followup": False,
    "triage_assessor": True,
    "directive_loop": True,
}
_AWAY_DESCRIPTION = (
    "The factory keeps taking new work while the owner is unreachable; the colleague-facing loop is off."
)
_SHIPPED_PRESENT = dict.fromkeys(
    (
        "inbox",
        "dispatch",
        "tickets",
        "ship",
        "review",
        "followup",
        "audit",
        "news",
        "arch_review",
        "dream",
        "eval_local",
        "dogfood",
        "snapshot_warmer",
        "housekeeping",
        "idle_stack_reaper",
        "local_stack_queue",
        "resource_pressure",
    ),
    True,
)
_SHIPPED_PRESENT_DESCRIPTION = "Full working-hours mode: deliver, interact, keep improvement loops warm."
_TOKEN_GUARD = {"inbox": True, "housekeeping": True, "dispatch": False, "db_backup": True}
_DREAM_TABLE = {
    "compliance_escalate": True,
    "automation_asks": True,
    "memory_promote": True,
    "validate_live": True,
    "derive_evals": True,
    "promotion_cap": 5,
}


def _seed_the_live_box(state_apps: StateApps, db: str) -> None:
    loop = state_apps.get_model("core", "Loop").objects.using(db)
    names = (*_DISABLED, *_ENABLED)
    loop.all().delete()
    for name in names:
        loop.create(name=name, script=f"t3 loop {name}", enabled=name in _ENABLED, delay_seconds=_DELAYS.get(name, 600))
    loop.filter(name="inbox").update(last_run_at=timezone.now())
    state_apps.get_model("core", "LoopState").objects.using(db).create(
        name="ship", status="enabled", forced="off", forced_reason="merge pause"
    )
    mode = state_apps.get_model("core", "Mode").objects.using(db)
    mode.all().delete()
    mode.create(name="present", entries=_SHIPPED_PRESENT, description=_SHIPPED_PRESENT_DESCRIPTION, overlay_scope=[])
    mode.create(name="maintenance", entries=_MAINTENANCE, description=_MAINTENANCE_DESCRIPTION, overlay_scope=[])
    mode.create(name="away", entries=_AWAY, description=_AWAY_DESCRIPTION, overlay_scope=[])
    mode.create(name="off", entries={**_TOKEN_GUARD, "tickets": False}, description="off", overlay_scope=[])
    mode.create(name="low-token", entries={**_TOKEN_GUARD, "tickets": True}, description="low", overlay_scope=[])
    config = state_apps.get_model("core", "ConfigSetting").objects.using(db)
    config.all().delete()
    config.create(scope="", key="on_behalf_post_mode", value="immediate")
    config.create(scope="", key="loops", value={"dream": _DREAM_TABLE})


def _migrate_to_head() -> StateApps:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    targets = executor.loader.graph.leaf_nodes("core")
    executor.migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


class _UpgradeCase(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        _seed_the_live_box(executor.loader.project_state(_BEFORE).apps, connection.alias)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)


@pytest.mark.timeout(300)
class TestTheUpgradeKeepsTheLiveBoxBehaviour(_UpgradeCase):
    def test_the_upgrade_lands_what_the_box_ran_before_it(self) -> None:
        head = _migrate_to_head()
        loop = head.get_model("core", "Loop").objects
        mode = {row.name: row for row in head.get_model("core", "Mode").objects.all()}
        config = {row.key: row.value for row in head.get_model("core", "ConfigSetting").objects.filter(scope="")}

        assert {name: mode["maintenance"].entries[name] for name in _MAINTENANCE} == _MAINTENANCE
        assert mode["maintenance"].description == _MAINTENANCE_DESCRIPTION
        assert {name: mode["afk"].entries[name] for name in _AWAY} == _AWAY
        assert mode["afk"].entries["dogfood"] is False
        assert mode["token-outage"].entries["tickets"] is False
        assert "low-token" not in mode

        assert dict(loop.exclude(enabled=None).values_list("name", "enabled")) == {"ship": False}

        started = {name for name in _DISABLED if mode["present"].entries[name]}
        assert started == {"dogfood", "eval_local", "snapshot_warmer"}, "only what present already named on"
        assert mode["present"].entries["directive_loop"] is True
        assert mode["present"].description != _SHIPPED_PRESENT_DESCRIPTION

        assert {row.name: row.egress for row in mode.values()} == dict.fromkeys(mode, "allow")
        assert "on_behalf_post_mode" not in config

        assert loop.get(name="eval_local").delay_seconds == 168 * _HOUR
        assert loop.get(name="triage_assessor").delay_seconds == 24 * _HOUR

        carried = {key: value for key, value in config.items() if key.startswith("dream_")}
        assert carried == {
            "dream_compliance_escalate": True,
            "dream_automation_asks": True,
            "dream_validate_live": True,
            "dream_derive_evals": True,
        }


class _PinConfigTo:
    def __init__(self, alias: str) -> None:
        self.alias = alias

    def db_for_read(self, model: type, **hints: object) -> str | None:
        _ = hints
        return self.alias if model._meta.label_lower == CONFIG_MODEL_LABEL else None

    db_for_write = db_for_read


@pytest.fixture
def canonical_alias(django_db_blocker: pytest.FixtureRequest) -> Iterator[str]:
    alias = f"canon_{uuid.uuid4().hex}"
    with django_db_blocker.unblock(), tempfile.TemporaryDirectory() as tmp:
        register_sqlite_alias(alias, Path(tmp) / f"{alias}.sqlite3")
        with connections[alias].schema_editor() as editor:
            editor.create_model(live_apps.get_model("core", "ConfigSetting"))
        try:
            yield alias
        finally:
            teardown_sqlite_alias(alias)


@pytest.mark.timeout(300)
class TestAWorktreeMigrateNeverWritesTheCanonicalStore(_UpgradeCase):
    canonical: str

    @pytest.fixture(autouse=True)
    def _bind(self, canonical_alias: str) -> None:
        self.canonical = canonical_alias

    def _canonical_rows(self) -> set[tuple[str, str, str]]:
        rows = live_apps.get_model("core", "ConfigSetting").objects.using(self.canonical)
        return {(row.scope, row.key, repr(row.value)) for row in rows.all()}

    def test_every_config_write_lands_on_the_connection_being_migrated(self) -> None:
        canonical = live_apps.get_model("core", "ConfigSetting").objects.using(self.canonical)
        for key, value in (
            ("on_behalf_post_mode", "immediate"),
            ("loops", {"dream": _DREAM_TABLE}),
            ("low_power_preset_name", "low-token"),
            ("loop_runner_enabled", False),
            ("eval_local_cadence_hours", 9),
            ("dream_promotion_cap", 5),
        ):
            canonical.create(scope="", key=key, value=value)
        before = self._canonical_rows()

        with override_settings(DATABASE_ROUTERS=[_PinConfigTo(self.canonical)]):
            _migrate_to_head()

        assert self._canonical_rows() == before
        migrated = live_apps.get_model("core", "ConfigSetting").objects.using(connection.alias)
        assert not migrated.filter(key="on_behalf_post_mode").exists()
        assert migrated.filter(key="dream_derive_evals", value=True).exists()
