"""An ``agent_skill_models`` row the dispatcher could not parse is refused when it is written."""

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


def _set(value: object, stderr: StringIO) -> None:
    call_command("config_setting", "set", "agent_skill_models", json.dumps(value), stdout=StringIO(), stderr=stderr)


class TestAgentSkillModelsWrite(TestCase):
    def test_a_route_without_a_model_is_refused_naming_the_entry(self) -> None:
        stderr = StringIO()
        with pytest.raises(SystemExit) as refused:
            _set({"code": [{"harness": "codex_app_server", "tier": "frontier"}]}, stderr)

        assert refused.value.code == 2
        assert "agent_skill_models['code'][0].model" in stderr.getvalue()
        assert not ConfigSetting.objects.filter(key="agent_skill_models").exists()

    def test_a_codex_then_claude_route_is_stored(self) -> None:
        _set(_ROUTES, StringIO())

        assert ConfigSetting.objects.get_effective("agent_skill_models") == _ROUTES
