"""An ``agent_skill_models`` row the dispatcher could not route is refused when it is written."""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config.write_validation import ConfigWriteError, validate_config_write
from teatree.core.models import ConfigSetting

_ROUTES = {
    "code": [
        {"harness": "codex_app_server", "model": "gpt-6-sol"},
        {"harness": "claude_sdk", "model": "claude-opus-5-5"},
    ]
}


class TestAgentSkillModelsWrite(TestCase):
    def _set(self, value: object) -> str:
        stderr = StringIO()
        call_command("config_setting", "set", "agent_skill_models", json.dumps(value), stdout=StringIO(), stderr=stderr)
        return stderr.getvalue()

    def test_a_route_without_a_model_is_refused_naming_the_entry(self) -> None:
        with pytest.raises(SystemExit) as refused:
            self._set({"code": [{"harness": "codex_app_server", "tier": "frontier"}]})

        assert refused.value.code == 2
        assert not ConfigSetting.objects.filter(key="agent_skill_models").exists()

    def test_the_refusal_names_the_offending_key(self) -> None:
        with pytest.raises(ConfigWriteError, match=r"agent_skill_models\['code'\]\[0\]\.model"):
            validate_config_write("agent_skill_models", {"code": [{"harness": "codex_app_server", "tier": "frontier"}]})

    def test_a_codex_then_claude_route_is_stored(self) -> None:
        self._set(_ROUTES)

        assert ConfigSetting.objects.get_effective("agent_skill_models") == _ROUTES
