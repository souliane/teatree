"""``config_setting set`` refuses an agent route or phase-harness row the dispatcher could not parse."""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ConfigSetting

_ROUTES = {
    "code": [
        {"harness": "codex_app_server", "model": "gpt-6-sol"},
        {"harness": "claude_sdk", "model": "claude-opus-5-5"},
    ]
}


def _set(key: str, value: object, stderr: StringIO) -> None:
    call_command("config_setting", "set", key, json.dumps(value), stdout=StringIO(), stderr=stderr)


class TestAgentSkillModelsWrite(TestCase):
    def test_a_route_without_a_model_is_refused_naming_the_entry(self) -> None:
        stderr = StringIO()
        with pytest.raises(SystemExit) as refused:
            _set("agent_skill_models", {"code": [{"harness": "codex_app_server", "tier": "frontier"}]}, stderr)

        assert refused.value.code == 2
        assert "agent_skill_models['code'][0].model" in stderr.getvalue()
        assert not ConfigSetting.objects.filter(key="agent_skill_models").exists()

    def test_a_codex_then_claude_route_is_stored(self) -> None:
        _set("agent_skill_models", _ROUTES, StringIO())

        assert ConfigSetting.objects.get_effective("agent_skill_models") == _ROUTES


class TestAgentPhaseHarnessWrite(TestCase):
    def test_a_harness_outside_the_closed_set_is_refused_naming_the_valid_values(self) -> None:
        stderr = StringIO()
        with pytest.raises(SystemExit):
            _set("agent_phase_harness", {"coding": "codex_app_server"}, stderr)

        assert "valid values: claude_sdk, pydantic_ai" in stderr.getvalue()
        assert not ConfigSetting.objects.filter(key="agent_phase_harness").exists()

    def test_an_unpin_is_stored(self) -> None:
        _set("agent_phase_harness", {"testing": "inherit"}, StringIO())

        assert ConfigSetting.objects.get_effective("agent_phase_harness") == {"testing": "inherit"}
