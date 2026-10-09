"""A stored route the dispatcher cannot parse fails its task once, by name, instead of crashing the worker."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.agents.runner as runner_mod
from teatree.agents.runner import TaskUsage, run_agent
from teatree.core.models import Session, Task, TaskAttempt
from teatree.types import SkillMetadata
from tests.factories import planned_ticket

_LEGACY_TIER_ONLY_ROUTE = {"code": [{"harness": "codex_app_server", "tier": "frontier"}]}


class TestALegacyRouteRow(TestCase):
    @pytest.fixture(autouse=True)
    def _legacy_row(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "config.sqlite3"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute(
                "CREATE TABLE teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)"
            )
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'agent_skill_models', ?)",
                (json.dumps(_LEGACY_TIER_ONLY_ROUTE),),
            )
        monkeypatch.setenv("T3_CONFIG_DB", str(db))

    def test_the_task_fails_once_naming_the_unparseable_key(self) -> None:
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="debugging")

        with (
            patch("teatree.agents.runner_skill_staging.resolve_skill_bundle", return_value=["code"]),
            patch.object(Task, "renew_lease", lambda self, **_kw: None),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            attempt = run_agent(task, phase="debugging", overlay_skill_metadata=SkillMetadata())

        task.refresh_from_db()
        assert TaskAttempt.objects.filter(task=task).count() == 1
        assert attempt.exit_code == 1
        assert "agent_skill_models['code'][0].model" in attempt.error
        assert task.status == Task.Status.FAILED
