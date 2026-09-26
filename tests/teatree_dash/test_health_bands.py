"""Each health band fails open on its own — one raising reader never 500s the page (#3164).

SHOULD-FIX #3: the module docstring promises per-band fail-open, but only
``_spend_summary`` was guarded. A raising verdict/loops/mode reader used to
propagate and blank the whole ``/dash/health/`` page.
"""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from teatree.core.models import AnthropicActivePick, AnthropicTokenUsage
from teatree.core.models.anthropic_token_usage import TokenHealthReading
from teatree.core.models.config_setting import ConfigSetting
from teatree.dash import gate_state, health_bands


class PerBandFailOpenTestCase(TestCase):
    def test_raising_verdict_reader_degrades_only_its_band(self) -> None:
        with patch.object(health_bands, "read_health", side_effect=RuntimeError("verdict boom")):
            view = health_bands.build_health_view()
        # the failed band carries a visible error…
        assert view.verdict.error is not None
        assert view.verdict.status == "error"
        # …and the other three bands are unaffected.
        assert view.loops.error is None
        assert view.capacity.error is None
        assert view.mode.error is None

    def test_raising_mode_reader_degrades_only_its_band(self) -> None:
        with patch.object(health_bands, "resolve_active_mode", side_effect=RuntimeError("mode boom")):
            view = health_bands.build_health_view()
        assert view.mode.error is not None
        assert view.verdict.error is None
        assert view.loops.error is None
        assert view.capacity.error is None

    def test_gate_read_failure_fails_closed_to_false(self) -> None:
        with patch.object(ConfigSetting.objects, "get_effective", side_effect=RuntimeError("db down")):
            assert gate_state.dash_gate_fail_open() is False

    def test_health_page_survives_a_raising_band(self) -> None:
        # The page still renders 200 with the degraded band's error visible.
        with patch.object(health_bands, "read_health", side_effect=RuntimeError("verdict boom")):
            response = self.client.get(reverse("dash:health"))
        assert response.status_code == 200
        assert "verdict band unavailable" in response.content.decode()


class AccountRoutingIsVisibleTestCase(TestCase):
    """The page showed what each account had LEFT but never which one work is routed to."""

    @staticmethod
    def _seed(pass_path: str, *, u7: float) -> None:
        AnthropicTokenUsage.objects.record(
            pass_path,
            TokenHealthReading(
                organization_id="org-1",
                utilization_5h=0.0,
                utilization_7d=u7,
                status_5h="allowed",
                status_7d="allowed",
                reset_5h=None,
                reset_7d=None,
            ),
        )

    def test_each_account_names_the_scopes_currently_routed_to_it(self) -> None:
        self._seed("anthropic/a/oauth", u7=0.96)
        self._seed("anthropic/b/oauth", u7=0.05)
        AnthropicActivePick.objects.set_pick("oauth", "", "anthropic/a/oauth")
        AnthropicActivePick.objects.set_pick("oauth", "alpha", "anthropic/a/oauth")

        accounts = {account.pass_path: account for account in health_bands.build_health_view().capacity.accounts}

        assert accounts["anthropic/a/oauth"].pinned_scopes == ("oauth:global", "oauth:alpha")
        assert accounts["anthropic/b/oauth"].pinned_scopes == ()

    def test_an_unpinned_account_names_no_scope(self) -> None:
        self._seed("anthropic/a/oauth", u7=0.05)

        (account,) = health_bands.build_health_view().capacity.accounts

        assert account.pinned_scopes == ()

    def test_the_rendered_page_shows_the_routed_scope(self) -> None:
        self._seed("anthropic/a/oauth", u7=0.05)
        AnthropicActivePick.objects.set_pick("oauth", "alpha", "anthropic/a/oauth")

        body = self.client.get(reverse("dash:health")).content.decode()

        assert "routed for" in body
        assert "oauth:alpha" in body
