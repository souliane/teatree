"""Direct Codex dispatches inherit Codex's model default, not a Claude tier id."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.agents.runner as runner_mod
from teatree.agents._runner_env import DispatchCredential
from teatree.agents._runner_options import SpawnOverrides, _build_options
from teatree.agents.codex_app_server import codex_app_server_spec
from teatree.agents.codex_app_server_options import CodexAppServerError, CodexAppServerOptions
from teatree.agents.harness_dispatch import DispatchHarness
from teatree.agents.harness_registry import HarnessBuildContext, HarnessCapabilities
from teatree.agents.model_tiering import TIER_MODELS
from teatree.core.modelkit.phases import KNOWN_PHASES
from teatree.core.models import Session, Task
from tests.factories import planned_ticket


class TestCodexRunnerModelSelection(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = planned_ticket()

    def _task(self) -> Task:
        return Task.objects.create(ticket=self.ticket, session=Session.objects.create(ticket=self.ticket))

    def _prepared_model(
        self,
        *,
        harness_name: str,
        model: str | None,
        route_candidate_index: int | None,
    ) -> str | None:
        dispatch = DispatchHarness(
            harness=SimpleNamespace(capabilities=HarnessCapabilities()),
            name=harness_name,
            provider=None,
            model=model,
            route_candidate_index=route_candidate_index,
        )
        preflight = runner_mod._Preflight(stage_skills=[], dispatch=dispatch, skills=[])
        with (
            patch("teatree.agents.prompt.build_task_prompt", return_value="prompt"),
            patch("teatree.agents.prompt.build_system_context", return_value="context"),
        ):
            prepared = runner_mod._prepare_run(
                self._task(),
                preflight,
                phase="coding",
                handoff=None,
                credential=DispatchCredential(),
            )
        return prepared.options.model

    def test_explicit_none_is_a_real_backend_default_override(self) -> None:
        options = _build_options(
            self._task(),
            "context",
            phase="coding",
            skills=[],
            overrides=SpawnOverrides(model=None, model_is_resolved=True),
        )

        assert options.model is None
        assert options.fallback_model is None
        assert options.thinking is None

    def test_direct_codex_uses_backend_default(self) -> None:
        assert self._prepared_model(harness_name="codex_app_server", model=None, route_candidate_index=None) is None

    def test_route_selected_harness_scopes_the_effort_vocabulary(self) -> None:
        task = self._task()
        with patch("teatree.agents._runner_options.resolve_spawn_effort", return_value="xhigh") as resolve_effort:
            options = _build_options(
                task,
                "context",
                phase="coding",
                skills=[],
                overrides=SpawnOverrides(
                    model="gpt-5.6-sol",
                    model_is_resolved=True,
                    harness_name="codex_app_server",
                ),
            )

        assert options.effort == "xhigh"
        resolve_effort.assert_called_once_with("coding", harness="codex_app_server")

    def test_route_candidate_effort_overrides_the_global_phase_effort(self) -> None:
        with patch("teatree.agents._runner_options.resolve_spawn_effort") as resolve_effort:
            options = _build_options(
                self._task(),
                "context",
                phase="coding",
                skills=[],
                overrides=SpawnOverrides(
                    model="gpt-5.6-sol",
                    model_is_resolved=True,
                    effort="high",
                    harness_name="codex_app_server",
                ),
            )

        assert options.effort == "high"
        resolve_effort.assert_not_called()

    def test_routed_codex_uses_the_candidate_model(self) -> None:
        assert (
            self._prepared_model(
                harness_name="codex_app_server",
                model="gpt-5.6-sol",
                route_candidate_index=0,
            )
            == "gpt-5.6-sol"
        )

    def test_direct_claude_preserves_phase_model_resolution(self) -> None:
        assert (
            self._prepared_model(harness_name="claude_sdk", model=None, route_candidate_index=None)
            == TIER_MODELS["frontier"]
        )

    def test_every_dispatchable_phase_is_translatable_or_rejected_by_the_preflight_probe(self) -> None:
        spec = codex_app_server_spec()
        with patch("teatree.agents.codex_app_server.shutil.which", return_value="/usr/bin/codex"):
            for phase in sorted(KNOWN_PHASES):
                options = _build_options(
                    self._task(),
                    "context",
                    phase=phase,
                    skills=[],
                    overrides=SpawnOverrides(model="gpt-5.6-sol", model_is_resolved=True),
                )
                unavailable = spec.unavailable_reason(HarnessBuildContext(phase=phase))
                if unavailable is not None:
                    with pytest.raises(CodexAppServerError, match="cannot enforce"):
                        CodexAppServerOptions.from_sdk_options(options)
                else:
                    translated = CodexAppServerOptions.from_sdk_options(options)
                    assert translated.core.model == "gpt-5.6-sol", phase
