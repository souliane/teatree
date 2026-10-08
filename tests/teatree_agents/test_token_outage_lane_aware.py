"""A spent subscription window engages the token-outage preset only when no other lane can take the work."""

import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

import teatree.agents.harness_dispatch as harness_dispatch_mod
from teatree.agents.usage_window import park_task_on_limit
from teatree.config.agent_spawn import AgentConfig, AgentRouteCandidate, InvalidAgentConfigError
from teatree.core.models import ConfigSetting, Mode, ModeOverride, Session, Task, TaskAttempt, UsageWindowState
from teatree.llm.anthropic_limits import LimitCause, LimitMatch
from tests.factories import planned_ticket
from tests.teatree_agents._route_fakes import make_codex_available, routed_by, write_codex_login

_SESSION_LIMIT = LimitMatch(phrase="five_hour", cause=LimitCause.SUBSCRIPTION_SESSION)
_CODEX_THEN_CLAUDE = AgentConfig(
    skill_models={
        "code": (
            AgentRouteCandidate("codex_app_server", "gpt-6-sol"),
            AgentRouteCandidate("claude_sdk", "claude-opus-5-5"),
        )
    }
)


class TestTokenOutageIsLaneAware(TestCase):
    def setUp(self) -> None:
        Mode.objects.create(name="token-outage", entries={"inbox": True})
        ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        make_codex_available(self, self.home)
        ticket = planned_ticket()
        self.task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")

    def _park_the_subscription_lane(self, config: AgentConfig = _CODEX_THEN_CLAUDE) -> None:
        with routed_by(config):
            park_task_on_limit(self.task, _SESSION_LIMIT, lane=TaskAttempt.Lane.SUBSCRIPTION)

    def test_an_available_codex_route_keeps_the_factory_running(self) -> None:
        write_codex_login(self.home)

        self._park_the_subscription_lane()

        assert ModeOverride.objects.current() is None

    def test_with_no_route_the_preset_engages(self) -> None:
        write_codex_login(self.home)

        self._park_the_subscription_lane(AgentConfig())

        assert ModeOverride.objects.current().preset_name == "token-outage"

    def test_a_windowed_managed_lane_engages_the_preset(self) -> None:
        write_codex_login(self.home)
        UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.MANAGED, cause="subscription_weekly", resets_at=timezone.now() + timedelta(days=2)
        )

        self._park_the_subscription_lane()

        assert ModeOverride.objects.current().preset_name == "token-outage"

    def test_a_codex_route_without_a_login_engages_the_preset(self) -> None:
        self._park_the_subscription_lane()

        assert ModeOverride.objects.current().preset_name == "token-outage"

    def test_an_unreadable_route_table_engages_the_preset(self) -> None:
        write_codex_login(self.home)

        with patch.object(harness_dispatch_mod, "resolve_agent_config", side_effect=InvalidAgentConfigError("bad")):
            park_task_on_limit(self.task, _SESSION_LIMIT, lane=TaskAttempt.Lane.SUBSCRIPTION)

        assert ModeOverride.objects.current().preset_name == "token-outage"
