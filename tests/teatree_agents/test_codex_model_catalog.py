"""A Codex route naming a model its catalog does not list falls through before a turn is spent."""

import shutil
import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.agents.harness_dispatch import DispatchHarness, resolve_dispatch_harness
from teatree.config.agent_spawn import AgentConfig, AgentRouteCandidate
from teatree.core.models import Session, Task
from tests.factories import planned_ticket
from tests.teatree_agents._route_fakes import (
    CLAUDE_LIKE,
    make_codex_available,
    register_stub_harnesses,
    routed_by,
    write_codex_login,
)

_CODEX = "codex_app_server"
_CATALOG = Path(__file__).resolve().parents[1] / "fixtures" / "codex_app_server" / "0.155.1-models-cache.json"


class TestCodexModelCatalog(TestCase):
    def setUp(self) -> None:
        register_stub_harnesses(self, CLAUDE_LIKE)
        self.home = write_codex_login(Path(self.enterContext(tempfile.TemporaryDirectory())))
        make_codex_available(self, self.home)
        ticket = planned_ticket()
        self.task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="debugging")

    def _resolve(self, model: str) -> DispatchHarness:
        route = (AgentRouteCandidate(_CODEX, model), AgentRouteCandidate(CLAUDE_LIKE, "claude-model"))
        with routed_by(AgentConfig(skill_models={"route-skill": route})):
            return resolve_dispatch_harness(self.task, phase="debugging", skills=["route-skill"])

    def test_a_model_the_catalog_does_not_list_falls_through_to_the_next_candidate(self) -> None:
        shutil.copy(_CATALOG, self.home / "models_cache.json")

        dispatch = self._resolve("gpt-9-absent")

        assert (dispatch.name, dispatch.route_candidate_index) == (CLAUDE_LIKE, 1)
        assert "not in the Codex catalog" in dispatch.rejected[0].reason

    def test_a_listed_model_keeps_codex(self) -> None:
        shutil.copy(_CATALOG, self.home / "models_cache.json")

        assert self._resolve("gpt-6-sol").route_candidate_index == 0

    def test_a_hidden_model_still_counts_as_listed(self) -> None:
        shutil.copy(_CATALOG, self.home / "models_cache.json")

        assert self._resolve("gpt-reserve").route_candidate_index == 0

    def test_without_a_catalog_the_check_fails_open(self) -> None:
        assert self._resolve("gpt-9-absent").route_candidate_index == 0

    def test_an_unreadable_catalog_fails_open_and_says_so(self) -> None:
        (self.home / "models_cache.json").write_text('{"models": "reshaped"}')

        with self.assertLogs("teatree.agents.codex_app_server_options", level="WARNING") as logged:
            dispatch = self._resolve("gpt-9-absent")

        assert dispatch.route_candidate_index == 0
        assert "models_cache.json" in logged.output[0]
