"""Lane B resolves its config at the TASK's overlay scope, not global/active only.

The Lane-B settings (``openai_compatible_base_url``, ``pydantic_ai_request_limit``,
``agent_harness``, …) are all per-overlay overridable, but ``resolve_harness`` read
them with no overlay — so an overlay-scoped override for a NON-active overlay was
ignored during headless dispatch. The dispatch is per-task and the task's overlay is
authoritative (``task.ticket.overlay``), so the harness must resolve there.
"""

from django.test import TestCase

from teatree.agents.harness import PydanticAiHarness, resolve_harness
from teatree.core.models import ConfigSetting, Session, Task, Ticket

_OVERLAY = "beta-overlay"
_GLOBAL_URL = "https://global.example/v1"
_OVERLAY_URL = "https://beta.example/v1"


class TestLaneBConfigScope(TestCase):
    def setUp(self) -> None:
        # Select Lane B for every overlay (global), then differ the endpoint + cap
        # per overlay so a global-only read is distinguishable from an overlay read.
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ConfigSetting.objects.set_value("agent_harness_provider", "openai_compatible")
        ConfigSetting.objects.set_value("openai_compatible_base_url", _GLOBAL_URL)
        ConfigSetting.objects.set_value("openai_compatible_base_url", _OVERLAY_URL, scope=_OVERLAY)
        ConfigSetting.objects.set_value("pydantic_ai_request_limit", 99, scope=_OVERLAY)

    def _task_on(self, overlay: str) -> Task:
        ticket = Ticket.objects.create(overlay=overlay)
        session = Session.objects.create(ticket=ticket)
        return Task.objects.create(ticket=ticket, session=session)

    def test_harness_resolves_the_task_overlays_endpoint(self) -> None:
        harness = resolve_harness(self._task_on(_OVERLAY))
        assert isinstance(harness, PydanticAiHarness)
        assert harness._backend.base_url == _OVERLAY_URL

    def test_harness_resolves_the_task_overlays_request_limit(self) -> None:
        harness = resolve_harness(self._task_on(_OVERLAY))
        assert isinstance(harness, PydanticAiHarness)
        assert harness._backend.request_limit == 99

    def test_a_different_overlay_still_reads_the_global_default(self) -> None:
        # An overlay with no per-overlay row falls back to the global endpoint —
        # proving the scope is the TASK's overlay, not a blanket override.
        harness = resolve_harness(self._task_on("other-overlay"))
        assert isinstance(harness, PydanticAiHarness)
        assert harness._backend.base_url == _GLOBAL_URL
