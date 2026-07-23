"""The dashboard answers "what is this box configured to do?" without an SSH session (#3664)."""

from unittest import mock

import pytest
from django.test import TestCase

from teatree.config.agent_spawn import AgentConfig
from teatree.core.config_self_repair import ConfigRepair
from teatree.core.models import ConfigSetting, Session, Task, Ticket
from teatree.dash.config_surface import CredentialEntry, build_config_view, classify_setting_band


class TestSettingBands:
    """Every setting the owner tuned this session lands in a band, none in two."""

    @pytest.mark.parametrize(
        ("name", "band"),
        [
            ("agent_harness", "agent"),
            ("agent_harness_provider", "agent"),
            ("agent_runtime", "agent"),
            ("loop_runner_enabled", "kill_switches"),
            ("scanning_news_disabled", "kill_switches"),
            ("danger_gate_fail_open", "kill_switches"),
            ("boost_concurrency", "concurrency"),
            ("max_concurrent_local_stacks", "concurrency"),
            ("provision_max_concurrency", "concurrency"),
            ("provision_ram_ceiling_percent", "memory"),
            ("anthropic_oauth_pass_paths", "credentials"),
            ("openai_compatible_credential_entry", "credentials"),
        ],
    )
    def test_setting_lands_in_its_band(self, name: str, band: str) -> None:
        assert classify_setting_band(name) == band

    def test_an_unclassified_setting_is_omitted(self) -> None:
        assert classify_setting_band("billing_cycle_anchor_day") == ""


class TestCredentialEntry:
    """Never a secret value — only the entry NAME and whether it resolves."""

    def test_renders_the_entry_name_not_the_secret(self) -> None:
        entry = CredentialEntry(setting="openai_compatible_credential_entry", entry_name="router/key", resolves=True)
        assert entry.entry_name == "router/key"
        assert not hasattr(entry, "value")

    def test_a_private_setting_masks_even_its_entry_name(self) -> None:
        assert CredentialEntry.mask_if_private("github_token_pass_key", "team/internal/token").entry_name == "<private>"

    def test_a_public_setting_keeps_its_entry_name(self) -> None:
        entry = CredentialEntry.mask_if_private("openai_compatible_credential_entry", "router/key")
        assert entry.entry_name == "router/key"


class TestBuildConfigView(TestCase):
    def test_surfaces_the_configured_model_and_effort(self) -> None:
        # ``resolve_agent_config`` reads the cold sqlite store directly, not the ORM,
        # so the pinned config is injected at the seam the surface consumes.
        patched = mock.patch(
            "teatree.dash.config_surface.resolve_agent_config",
            return_value=AgentConfig(
                session_model="opusplan",
                session_effort="xhigh",
                tier_effort={"verification": "high"},
            ),
        )
        patched.start()
        self.addCleanup(patched.stop)

        view = build_config_view()

        rendered = {row.name: row.value for row in view.models}
        assert rendered["session_model"] == "opusplan"
        assert rendered["session_effort"] == "xhigh"
        assert rendered["tier_effort[verification]"] == "high"

    def test_surfaces_the_real_resolved_model_pins_with_no_stub(self) -> None:
        assert {row.name for row in build_config_view().models} >= {
            "session_model",
            "session_effort",
            "honesty_model",
        }

    def test_surfaces_the_tuned_concurrency_and_memory_caps(self) -> None:
        ConfigSetting.objects.set_value("boost_concurrency", 4)
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 75)

        view = build_config_view()

        assert {row.name: row.value for row in view.concurrency}["boost_concurrency"] == "4"
        assert {row.name: row.value for row in view.memory}["provision_ram_ceiling_percent"] == "75"

    def test_surfaces_kill_switches(self) -> None:
        ConfigSetting.objects.set_value("loop_runner_enabled", value=False)

        view = build_config_view()

        assert {row.name: row.value for row in view.kill_switches}["loop_runner_enabled"] == "off"

    def test_never_renders_a_secret_value(self) -> None:
        ConfigSetting.objects.set_value("github_token_pass_key", "team/internal/token")

        view = build_config_view()

        assert all("team/internal/token" not in entry.entry_name for entry in view.credentials)

    def test_surfaces_a_self_repair_so_it_is_visible_without_paging(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        repair = ConfigRepair(setting="agent_harness", value="pydantic_ai", detail="d")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            execution_reason=f"dispatch\n{repair.stamp()}",
        )

        view = build_config_view()

        assert [row.correction for row in view.self_repairs] == ["agent_harness=pydantic_ai"]

    def test_a_broken_reader_degrades_the_page_to_an_error_not_a_500(self) -> None:
        patched = mock.patch(
            "teatree.dash.config_surface.get_effective_settings",
            side_effect=RuntimeError("boom"),
        )
        patched.start()
        self.addCleanup(patched.stop)

        view = build_config_view()

        assert view.error
