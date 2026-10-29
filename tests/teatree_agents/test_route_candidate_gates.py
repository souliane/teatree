"""The per-candidate gates of an ordered route: the scalar model floor and a pinned provider."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase

from teatree.agents.harness_dispatch import DispatchHarness, resolve_dispatch_harness
from teatree.config import AgentHarnessProvider
from teatree.config.agent_spawn import AgentConfig, AgentRouteCandidate
from teatree.core.models import Session, Task
from tests.factories import planned_ticket
from tests.teatree_agents._route_fakes import CLAUDE_LIKE, CLAUDE_SDK, register_stub_harnesses, routed_by


class TestRouteCandidateGates(TestCase):
    def setUp(self) -> None:
        register_stub_harnesses(self, CLAUDE_LIKE)
        ticket = planned_ticket()
        self.task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")

    def _resolve(self, config: AgentConfig, *skills: str) -> DispatchHarness:
        with routed_by(config):
            return resolve_dispatch_harness(self.task, phase="coding", skills=list(skills))

    def _raw_routes(self, *, overlay_row: bool) -> None:
        root = Path(self.enterContext(TemporaryDirectory()))
        db = root / "config.sqlite3"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute(
                "CREATE TABLE teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)"
            )
            routes = {
                "route-skill": [
                    {"harness": CLAUDE_LIKE, "model": "haiku"},
                    {"harness": CLAUDE_LIKE, "model": "opus"},
                ],
                "floor-skill": [{"floor": "opus"}],
            }
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
                ("", "agent_skill_models", json.dumps(routes)),
            )
            if overlay_row:
                routes = {
                    "route-skill": [{"harness": CLAUDE_LIKE, "model": "haiku"}],
                    "floor-skill": [{"floor": "haiku"}],
                }
                conn.execute(
                    "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
                    ("t3-teatree", "agent_skill_models", json.dumps(routes)),
                )
        self.enterContext(patch.dict("os.environ", {"T3_CONFIG_DB": str(db)}))

    def test_a_global_route_wins_over_a_stray_overlay_scoped_row(self) -> None:
        self._raw_routes(overlay_row=True)
        self.task.ticket.overlay = "t3-teatree"
        self.task.ticket.save(update_fields=["overlay"])

        dispatch = resolve_dispatch_harness(self.task, phase="coding", skills=["route-skill", "floor-skill"])

        assert dispatch.model == "opus"
        assert dispatch.route_candidate_index == 1
        assert dispatch.rejected[0].reason == "model is below a loaded skill's scalar floor"

    def test_an_overlay_less_task_resolves_the_global_route(self) -> None:
        self._raw_routes(overlay_row=False)

        dispatch = resolve_dispatch_harness(self.task, phase="coding", skills=["route-skill", "floor-skill"])

        assert dispatch.model == "opus"
        assert dispatch.route_candidate_index == 1

    def test_an_overlay_task_without_an_overlay_row_resolves_the_global_route(self) -> None:
        self._raw_routes(overlay_row=False)
        self.task.ticket.overlay = "t3-teatree"
        self.task.ticket.save(update_fields=["overlay"])

        dispatch = resolve_dispatch_harness(self.task, phase="coding", skills=["route-skill", "floor-skill"])

        assert dispatch.model == "opus"
        assert dispatch.route_candidate_index == 1
        assert dispatch.rejected[0].reason == "model is below a loaded skill's scalar floor"

    def test_a_candidate_below_a_loaded_skills_model_floor_is_skipped(self) -> None:
        config = AgentConfig(
            skill_models={
                "floor-skill": "opus",
                "route-skill": (AgentRouteCandidate(CLAUDE_LIKE, "haiku"), AgentRouteCandidate(CLAUDE_LIKE, "opus")),
            }
        )

        dispatch = self._resolve(config, "route-skill", "floor-skill")

        assert dispatch.route_candidate_index == 1
        assert dispatch.rejected[0].reason == "model is below a loaded skill's scalar floor"

    def test_a_candidate_naming_a_provider_keeps_it_on_the_dispatch(self) -> None:
        candidate = AgentRouteCandidate(CLAUDE_SDK, "opus", provider="subscription_oauth")

        dispatch = self._resolve(AgentConfig(skill_models={"route-skill": (candidate,)}), "route-skill")

        assert dispatch.provider is AgentHarnessProvider.SUBSCRIPTION_OAUTH
        assert dispatch.availability_provider == "subscription_oauth"
