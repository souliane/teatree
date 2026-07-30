"""The settings page's live readouts — resolved model pins, credential coordinates, self-repairs (#3664)."""

from collections.abc import Iterator
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import TestCase

from teatree.config.agent_spawn import AgentConfig
from teatree.core.config_self_repair import ConfigRepair
from teatree.core.models import ConfigSetting, Session, Task, Ticket
from teatree.dash.settings_readouts import (
    MASKED_ENTRY_NAME,
    CredentialEntry,
    _pass_entry_resolves,
    _self_repair_rows,
    build_readouts_view,
)


class TestCredentialEntry:
    """Never a secret value — only the entry NAME and whether it resolves."""

    def test_renders_the_entry_name_not_the_secret(self) -> None:
        entry = CredentialEntry(setting="openai_compatible_credential_entry", entry_name="router/key", resolves=True)
        assert entry.entry_name == "router/key"
        assert not hasattr(entry, "value")

    def test_a_private_setting_masks_even_its_entry_name(self) -> None:
        masked = CredentialEntry.mask_if_private("github_token_pass_key", "team/internal/token")
        assert masked.entry_name == MASKED_ENTRY_NAME == "<private>"

    def test_a_public_setting_keeps_its_entry_name(self) -> None:
        entry = CredentialEntry.mask_if_private("openai_compatible_credential_entry", "router/key")
        assert entry.entry_name == "router/key"


class TestBuildReadoutsView(TestCase):
    def test_surfaces_the_configured_model_and_effort(self) -> None:
        # ``resolve_agent_config`` reads the cold sqlite store directly, not the ORM,
        # so the pinned config is injected at the seam the surface consumes.
        patched = mock.patch(
            "teatree.dash.settings_readouts.resolve_agent_config",
            return_value=AgentConfig(
                session_model="opusplan",
                session_effort="xhigh",
                tier_effort={"verification": "high"},
            ),
        )
        patched.start()
        self.addCleanup(patched.stop)

        view = build_readouts_view()

        rendered = {row.name: row.value for row in view.models}
        assert rendered["session_model"] == "opusplan"
        assert rendered["session_effort"] == "xhigh"
        assert rendered["tier_effort[verification]"] == "high"

    def test_surfaces_the_real_resolved_model_pins_with_no_stub(self) -> None:
        assert {row.name for row in build_readouts_view().models} >= {
            "session_model",
            "session_effort",
            "honesty_model",
        }

    def test_a_configured_credential_coordinate_is_listed_by_entry_name(self) -> None:
        ConfigSetting.objects.set_value("openai_compatible_credential_entry", "router/key")

        view = build_readouts_view()

        assert {entry.entry_name for entry in view.credentials} >= {"router/key"}

    def test_never_renders_a_secret_value(self) -> None:
        ConfigSetting.objects.set_value("github_token_pass_key", "team/internal/token")

        view = build_readouts_view()

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

        view = build_readouts_view()

        assert [row.correction for row in view.self_repairs] == ["agent_harness=pydantic_ai"]

    def test_a_broken_reader_degrades_to_an_error_panel_not_a_500(self) -> None:
        patched = mock.patch(
            "teatree.dash.settings_readouts.get_effective_settings",
            side_effect=RuntimeError("boom"),
        )
        patched.start()
        self.addCleanup(patched.stop)

        view = build_readouts_view()

        assert view.error


class TestSelfRepairRowsAreResilient(TestCase):
    def test_a_failed_self_repair_read_omits_the_readout(self) -> None:
        # The readout is advisory — a DB read failure drops it rather than 500-ing.
        patched = mock.patch(
            "teatree.dash.settings_readouts.Task.objects.filter",
            side_effect=RuntimeError("db down"),
        )
        patched.start()
        self.addCleanup(patched.stop)

        assert _self_repair_rows() == ()


class TestPassEntryResolves:
    @pytest.fixture(autouse=True)
    def _isolate_probe_cache(self) -> Iterator[None]:
        """The probe memoises per entry name, and the locmem cache outlives one test."""
        cache.clear()
        yield
        cache.clear()

    def test_an_empty_entry_name_never_resolves(self) -> None:
        assert _pass_entry_resolves("") is False

    def test_a_pass_probe_failure_degrades_to_unresolved(self) -> None:
        with mock.patch("teatree.dash.settings_readouts.read_pass", side_effect=RuntimeError("no pass")):
            assert _pass_entry_resolves("team/token") is False

    def test_a_resolving_entry_reports_true(self) -> None:
        with mock.patch("teatree.dash.settings_readouts.read_pass", return_value="secret"):
            assert _pass_entry_resolves("team/token") is True


class TestCredentialProbeIsCached(TestCase):
    """The credential readout's ``pass show`` probe is a GPG decrypt — not once per poll.

    The readouts auto-poll every 15s and probe every configured credential entry.
    Uncached that is one decrypt per entry per poll, all day, with every decrypted value
    discarded — only "did it resolve" is ever rendered.
    """

    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)
        ConfigSetting.objects.set_value("openai_compatible_credential_entry", "router/key")
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", ["one/oauth", "two/oauth"])

    def test_a_second_render_does_not_re_probe_the_pass_store(self) -> None:
        with mock.patch("teatree.dash.settings_readouts.read_pass", return_value="secret") as probe:
            build_readouts_view()
            after_first_render = probe.call_count
            build_readouts_view()
            after_second_render = probe.call_count

        assert after_first_render > 0, "the readout probed nothing — the test proves nothing"
        assert after_second_render == after_first_render

    def test_a_probe_result_still_resolves_after_the_cache_serves_it(self) -> None:
        with mock.patch("teatree.dash.settings_readouts.read_pass", return_value="secret"):
            assert _pass_entry_resolves("some/entry") is True
            assert _pass_entry_resolves("some/entry") is True

    def test_each_entry_is_cached_under_its_own_name(self) -> None:
        with mock.patch("teatree.dash.settings_readouts.read_pass", side_effect=["", "found"]):
            assert _pass_entry_resolves("absent/entry") is False
            assert _pass_entry_resolves("present/entry") is True
