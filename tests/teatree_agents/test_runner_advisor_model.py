"""Advisor settings on the headless Claude child dispatch path."""

import json

from django.test import TestCase

from teatree.agents._runner_env import DispatchCredential
from teatree.agents._runner_options import SpawnOverrides, _build_options
from teatree.agents.claude_cli_spawn import prepared_spawn
from teatree.agents.harness import ClaudeSdkHarness, PydanticAiHarness
from teatree.agents.harness_dispatch import DispatchHarness
from teatree.agents.runner_preparation import Preflight, prepare_run
from teatree.config import UserSettings, get_effective_settings
from teatree.core.models import ConfigSetting, Session, Task, Ticket


class TestRunnerAdvisorModel(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def _task(self) -> Task:
        session = Session.objects.create(ticket=self.ticket)
        return Task.objects.create(ticket=self.ticket, session=session, phase="coding")

    def _options(self, *, harness_name: str = "claude_sdk"):
        return _build_options(
            self._task(),
            "ctx",
            phase="coding",
            skills=[],
            overrides=SpawnOverrides(harness_name=harness_name),
        )

    def test_default_is_empty_for_global_and_named_overlay_resolution(self) -> None:
        assert UserSettings().agent_advisor_model == ""
        assert get_effective_settings().agent_advisor_model == ""
        assert get_effective_settings("some-overlay").agent_advisor_model == ""
        assert self._options().settings is None

    def test_whitespace_only_advisor_keeps_settings_absent(self) -> None:
        for value in (" ", "\t", "\n"):
            with self.subTest(value=repr(value)):
                ConfigSetting.objects.set_value("agent_advisor_model", value)
                assert self._options().settings is None

    def test_prepared_spawn_twice_keeps_advisor_beside_account_sync_keys(self) -> None:
        ConfigSetting.objects.set_value("agent_advisor_model", "advisor-model")

        options = self._options()
        assert json.loads(options.settings) == {"advisorModel": "advisor-model"}
        with prepared_spawn(options) as first:
            first_settings = json.loads(first.settings)
            with prepared_spawn(first) as second:
                assert (
                    json.loads(second.settings)
                    == first_settings
                    == {
                        "advisorModel": "advisor-model",
                        "syncClaudeAiSkills": False,
                        "syncClaudeAiPlugins": False,
                    }
                )

    def test_dispatch_passes_selected_harness_to_options(self) -> None:
        ConfigSetting.objects.set_value("agent_advisor_model", "advisor-model")
        for name, harness in (("claude_sdk", ClaudeSdkHarness()), ("pydantic_ai", PydanticAiHarness())):
            with self.subTest(harness_name=name):
                dispatch = DispatchHarness(
                    harness=harness,
                    name=name,
                    provider=None,
                    model="claude-sonnet-4-6",
                    route_candidate_index=None,
                )
                prepared = prepare_run(
                    self._task(),
                    Preflight(stage_skills=[], dispatch=dispatch, skills=[]),
                    phase="coding",
                    handoff=None,
                    credential=DispatchCredential(env={"ANTHROPIC_API_KEY": "key-x"}),
                )
                if name == "claude_sdk":
                    assert json.loads(prepared.options.settings) == {"advisorModel": "advisor-model"}
                else:
                    assert prepared.options.settings is None

    def test_other_harnesses_do_not_receive_advisor(self) -> None:
        ConfigSetting.objects.set_value("agent_advisor_model", "advisor-model")

        for harness_name in ("pydantic_ai", "codex_app_server"):
            with self.subTest(harness_name=harness_name):
                assert self._options(harness_name=harness_name).settings is None
