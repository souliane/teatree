r"""Per-account routing selector + factory (``teatree.credential_config``).

Integration-first against the real ``ConfigSetting`` store + the ``AnthropicTokenUsage``
health cache + the ``AnthropicActivePick`` sticky pointer. The rate-limit READER is
injected (a fake mapping a token to a canned snapshot) so no probe hits the network;
the ``pass`` source is stubbed to ECHO the path it is handed and the ambient auth env
is cleared, so ``resolve()`` reveals exactly which ``pass`` entry the factory routed to
and the probe token equals the routed path.
"""

import datetime as dt
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

import pytest
from django.db.utils import OperationalError
from django.test import TestCase
from django.utils import timezone

from teatree.config import AgentHarnessProvider
from teatree.core.models import AnthropicActivePick, AnthropicTokenUsage, ConfigSetting
from teatree.core.models.anthropic_token_usage import HEALTH_TTL, REJECTED_STATUS, TokenHealthReading, Window
from teatree.core.models.config_setting import GLOBAL_SCOPE
from teatree.credential_config import (
    AccountProber,
    AllTokensExhaustedError,
    PassPathSelector,
    ReactiveLimit,
    TokenKind,
    reading_from,
    reading_from_metered,
    record_reactive_exhaustion_and_reselect,
    resolve_api_key_credential,
    resolve_eval_credential,
    resolve_subscription_credential,
)
from teatree.llm.credentials import AnthropicApiKeyCredential, AnthropicSubscriptionCredential, CredentialError
from teatree.llm.rate_limits import MeteredKeySnapshot, RateLimitProbeError, RateLimitSnapshot
from teatree.utils.eval_container import IN_CONTAINER_ENV_VAR

_OAUTH_SETTING = "anthropic_oauth_pass_paths"
_API_KEY_SETTING = "anthropic_api_key_pass_paths"


def _snapshot(
    *,
    u5: float | None = 0.1,
    u7: float | None = 0.1,
    s7: str = "allowed",
    reset: dt.datetime | None = None,
    org: str = "org-1",
) -> RateLimitSnapshot:
    return RateLimitSnapshot(
        organization_id=org,
        unified_5h_status="allowed",
        unified_5h_utilization=u5,
        unified_5h_reset=reset,
        unified_7d_status=s7,
        unified_7d_utilization=u7,
        unified_7d_reset=reset,
        retry_after=None,
    )


def _metered(*, out_of_credits: bool = False) -> MeteredKeySnapshot:
    return MeteredKeySnapshot(
        organization_id="org-1",
        out_of_credits=out_of_credits,
        requests_remaining=None if out_of_credits else 4999,
        requests_limit=None if out_of_credits else 5000,
        tokens_remaining=None if out_of_credits else 990000,
        input_tokens_remaining=None,
        output_tokens_remaining=None,
    )


class _FakeReader:
    """Maps a probe token (== the echoed ``pass_path``) to a canned snapshot; records calls."""

    def __init__(self, health: dict[str, RateLimitSnapshot]) -> None:
        self._health = health
        self.calls: list[str] = []

    def __call__(self, token: str, *, is_oauth: bool) -> RateLimitSnapshot:
        self.calls.append(token)
        return self._health[token]


@contextmanager
def _pass_echoes_path() -> Iterator[None]:
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("teatree.llm.credentials.read_pass", side_effect=lambda path: path),
    ):
        yield


def _seed_fresh_healthy_row(pass_path: str) -> AnthropicTokenUsage:
    reading = TokenHealthReading(
        organization_id="org-1",
        utilization_5h=0.1,
        utilization_7d=0.1,
        status_5h="allowed",
        status_7d="allowed",
        reset_5h=None,
        reset_7d=None,
    )
    return AnthropicTokenUsage.objects.record(pass_path, reading, now=timezone.now())


def _seed_row(
    pass_path: str,
    *,
    u5: float = 0.0,
    u7: float = 0.0,
    s7: str = "allowed",
    reset_7d: dt.datetime | None = None,
) -> AnthropicTokenUsage:
    reading = TokenHealthReading(
        organization_id="org-1",
        utilization_5h=u5,
        utilization_7d=u7,
        status_5h="allowed",
        status_7d=s7,
        reset_5h=None,
        reset_7d=reset_7d,
    )
    return AnthropicTokenUsage.objects.record(pass_path, reading, now=timezone.now())


class TestReadingTranslation:
    """The pure snapshot -> ``TokenHealthReading`` translations the selector applies."""

    def test_reading_from_maps_the_unified_snapshot_fields(self) -> None:
        reading = reading_from(_snapshot(u5=0.3, u7=0.8, s7="allowed_warning"))
        assert isinstance(reading, TokenHealthReading)
        assert reading.utilization_5h == pytest.approx(0.3)
        assert reading.utilization_7d == pytest.approx(0.8)
        assert reading.status_7d == "allowed_warning"
        assert not reading.is_exhausted

    def test_reading_from_metered_out_of_credits_maps_to_a_rejected_window(self) -> None:
        reading = reading_from_metered(_metered(out_of_credits=True))
        assert reading.status_7d == REJECTED_STATUS
        assert reading.is_exhausted, "an out-of-credits metered key is the same exhaustion signal routing refuses"

    def test_reading_from_metered_funded_is_not_exhausted(self) -> None:
        reading = reading_from_metered(_metered(out_of_credits=False))
        assert reading.status_7d == ""
        assert not reading.is_exhausted


class TestSelectorDefaultPath(TestCase):
    def test_no_configured_list_returns_no_override_and_never_probes(self) -> None:
        reader = _FakeReader({})
        with _pass_echoes_path():
            assert PassPathSelector(reader=reader).select(TokenKind.OAUTH) is None
        assert reader.calls == [], "selection reads the store, never the network"


def _seed_from_reader(reader: "_FakeReader") -> None:
    """Store the reader's canned health as the verdicts selection will actually read.

    These tests were written when selection probed, so the reader stub WAS the health.
    Selection now reads the store, so the same fixture data has to land there — this keeps
    each test's intent (which account is spent) while moving where that fact lives.
    """
    for pass_path, snapshot in reader._health.items():
        AnthropicTokenUsage.objects.record(pass_path, reading_from(snapshot), now=timezone.now())


def _seed_health(pass_path: str, *, exhausted: bool, hours_to_reset: float = 4.0) -> None:
    """Store a FRESH verdict for *pass_path* — what selection now reads instead of probing.

    Selection consults `AnthropicTokenUsage` and never the network, so a test that wants an
    account ruled out must say so in the store; a reader stub no longer reaches the decision.
    """
    reset = timezone.now() + dt.timedelta(hours=hours_to_reset)
    snapshot = _snapshot(u5=0.99, reset=reset) if exhausted else _snapshot(reset=reset)
    AnthropicTokenUsage.objects.record(pass_path, reading_from(snapshot), now=timezone.now())


class TestSelectorRouting(TestCase):
    def test_routes_to_first_healthy_account_and_pins_it_sticky(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])
        reader = _FakeReader({"anthropic/a/oauth": _snapshot()})
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        assert chosen == "anthropic/a/oauth"
        assert reader.calls == [], "selection reads the store, never the network"
        assert AnthropicActivePick.objects.pick_for("oauth", "") == "anthropic/a/oauth"

    def test_overlay_list_falls_back_to_global_when_overlay_has_none(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/global/oauth"])
        reader = _FakeReader({"anthropic/global/oauth": _snapshot()})
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH, scope="myoverlay")
        assert chosen == "anthropic/global/oauth"

    def test_skips_an_exhausted_account_for_the_next_healthy_one(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])
        reader = _FakeReader(
            {
                "anthropic/a/oauth": _snapshot(u5=0.97, reset=timezone.now() + dt.timedelta(hours=2)),
                "anthropic/b/oauth": _snapshot(),
            }
        )
        _seed_from_reader(reader)
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        assert chosen == "anthropic/b/oauth"

    def test_falls_back_to_another_overlays_account_when_own_is_exhausted(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/own/oauth"], scope="overlay-x")
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/other/oauth"], scope="overlay-y")
        reader = _FakeReader(
            {
                "anthropic/own/oauth": _snapshot(u7=0.995, reset=timezone.now() + dt.timedelta(days=2)),
                "anthropic/other/oauth": _snapshot(),
            }
        )
        _seed_from_reader(reader)
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH, scope="overlay-x")
        assert chosen == "anthropic/other/oauth", "own account exhausted → borrow another overlay's healthy account"

    def test_out_of_credits_api_key_is_treated_as_exhausted(self) -> None:
        # An out-of-credits metered key must not be routed to — routing collapses the
        # credit signal onto the same exhaustion refusal the selector already enforces.
        ConfigSetting.objects.set_value(_API_KEY_SETTING, ["anthropic/metered/api"])
        AnthropicTokenUsage.objects.record(
            "anthropic/metered/api", reading_from_metered(_metered(out_of_credits=True)), now=timezone.now()
        )
        reader = _FakeReader({})
        with _pass_echoes_path(), pytest.raises(AllTokensExhaustedError):
            PassPathSelector(reader=reader).select(TokenKind.API_KEY)
        assert reader.calls == [], "selection reads the store, never the network"

    def test_funded_api_key_is_routed(self) -> None:
        ConfigSetting.objects.set_value(_API_KEY_SETTING, ["anthropic/a/api", "anthropic/b/api"])
        with (
            _pass_echoes_path(),
            patch("teatree.credential_config.read_api_key_status", return_value=_metered()),
        ):
            assert PassPathSelector().select(TokenKind.API_KEY) == "anthropic/a/api"

    def test_all_accounts_exhausted_raises_naming_the_earliest_reset(self) -> None:
        soon = timezone.now() + dt.timedelta(hours=1)
        later = timezone.now() + dt.timedelta(hours=5)
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])
        reader = _FakeReader(
            {
                "anthropic/a/oauth": _snapshot(u5=0.97, reset=later),
                "anthropic/b/oauth": _snapshot(u5=0.98, reset=soon),
            }
        )
        _seed_from_reader(reader)
        with _pass_echoes_path(), pytest.raises(AllTokensExhaustedError) as caught:
            PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        message = str(caught.value)
        assert "exhausted" in message
        assert soon.isoformat() in message, "the loud error names the soonest an account frees up"
        assert caught.value.earliest_reset == soon, "the earliest reset is carried as a datetime for the C2 park"

    def test_earliest_reset_ignores_an_idle_windows_elapsed_reset(self) -> None:
        """The DM-flood signature: rejected on 7d, idle 5h whose reset has already rolled past.

        Pins the selector seam itself. Every other fixture here gives an account the SAME
        instant for both windows, so ``earliest_reset`` and ``frees_up_at`` coincide and the
        load-bearing ``_all_exhausted_error`` line could be reverted with the suite still
        green. Here they diverge: the naive min-of-resets answer is the PAST 5h instant, which
        is what parked a dead-on-arrival window and DM'd the owner once a minute.
        """
        now = timezone.now()
        stale_5h = now - dt.timedelta(minutes=1)
        blocking_7d = now + dt.timedelta(hours=14)
        far_7d = now + dt.timedelta(days=3)
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])
        reader = _FakeReader(
            {
                "anthropic/a/oauth": RateLimitSnapshot(
                    organization_id="org-1",
                    unified_5h_status="allowed",
                    unified_5h_utilization=0.0,  # idle — this window blocks NOTHING
                    unified_5h_reset=stale_5h,
                    unified_7d_status="rejected",
                    unified_7d_utilization=1.0,
                    unified_7d_reset=blocking_7d,
                    retry_after=None,
                ),
                "anthropic/b/oauth": RateLimitSnapshot(
                    organization_id="org-1",
                    unified_5h_status="allowed",
                    unified_5h_utilization=0.0,
                    unified_5h_reset=stale_5h,
                    unified_7d_status="rejected",
                    unified_7d_utilization=1.0,
                    unified_7d_reset=far_7d,
                    retry_after=None,
                ),
            }
        )

        _seed_from_reader(reader)
        with _pass_echoes_path(), pytest.raises(AllTokensExhaustedError) as caught:
            PassPathSelector(reader=reader).select(TokenKind.OAUTH)

        assert caught.value.earliest_reset == blocking_7d, "the soonest BLOCKING window, not the idle 5h one"
        assert caught.value.earliest_reset > now, "a park keyed on this must never be already elapsed"


class TestSelectionNeverProbes(TestCase):
    """Selection reads the stored health and opens no socket (the #3406 stall, inverted).

    The old selector probed LIVE whenever a row was missing or stale, so the decision
    "can work start" depended on the rate-limit API being reachable and quick. When it
    was not, selection returned nothing and the factory idled with capacity to spare.
    Nothing probes on the selection path at all; a spent account records itself reactively
    after one refused call, and these pin that selection adds no probe of its own.
    """

    def test_absent_rows_are_offered_without_a_probe(self) -> None:
        # Fail-open: a cold health table must never be able to halt dispatch. A spent
        # account costs one refused call and records itself through the reactive path.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])
        reader = _FakeReader({"anthropic/a/oauth": _snapshot(), "anthropic/b/oauth": _snapshot()})
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        assert chosen == "anthropic/a/oauth", "an account with no stored verdict is offered, not skipped"
        assert reader.calls == [], "selection reads the store, never the network"

    def test_a_stale_exhausted_row_is_offered_without_a_probe(self) -> None:
        # A rolling window frees capacity before its recorded reset, so a verdict that has
        # aged out is not evidence of exhaustion — and re-probing to find out is what used
        # to stall the lane. Offering it costs at most one refused call.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth"])
        # An exhausted verdict is trusted until its BLOCKING WINDOW re-arms, not for a flat
        # TTL — so "stale" here means the reset has since passed and the account may well
        # have capacity again. Offering it is the point: the alternative is stranding a
        # recovered account until something probes it.
        aged = timezone.now() - dt.timedelta(hours=6)
        elapsed_reset = timezone.now() - dt.timedelta(minutes=1)
        AnthropicTokenUsage.objects.record(
            "anthropic/a/oauth", reading_from(_snapshot(u5=0.99, reset=elapsed_reset)), now=aged
        )
        reader = _FakeReader({"anthropic/a/oauth": _snapshot()})
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        assert chosen == "anthropic/a/oauth", "a stale verdict does not rule an account out"
        assert reader.calls == [], "selection reads the store, never the network"

    def test_a_fresh_exhausted_row_still_hard_fails_without_a_probe(self) -> None:
        # Anti-vacuous: if selection ignored stored health entirely both tests above would
        # pass while the selector routed to a known-spent account every time.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth"])
        reset = timezone.now() + dt.timedelta(hours=4)
        AnthropicTokenUsage.objects.record(
            "anthropic/a/oauth", reading_from(_snapshot(u5=0.99, reset=reset)), now=timezone.now()
        )
        reader = _FakeReader({"anthropic/a/oauth": _snapshot()})  # would read healthy IF probed
        with _pass_echoes_path(), pytest.raises(AllTokensExhaustedError):
            PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        assert reader.calls == [], "selection reads the store, never the network"


class TestSelectorCrossScopeFallback(TestCase):
    """A requested scope with no routing falls back to the cross-scope union.

    The bug: ``anthropic_oauth_pass_paths`` rows configured only at overlay scopes and a
    bare ``select(OAUTH, GLOBAL_SCOPE)`` (no active overlay) short-circuited ``None`` at
    the empty-requested-scope check, never reaching the cross-overlay routing — so a bare
    eval shell aborted even though a healthy account was configured. The fix falls back to
    the union while PRESERVING fail-loud-on-nothing-anywhere and never picking an
    exhausted account.
    """

    def test_empty_requested_scope_routes_the_overlay_account_and_pins_it_at_the_requested_scope(self) -> None:
        # Overlay-scoped row only; the global-scope request must still find it.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/overlay/oauth"], scope="some-overlay")
        reader = _FakeReader({"anthropic/overlay/oauth": _snapshot()})
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH, scope=GLOBAL_SCOPE)
        assert chosen == "anthropic/overlay/oauth"
        assert AnthropicActivePick.objects.pick_for("oauth", GLOBAL_SCOPE) == "anthropic/overlay/oauth", (
            "the cross-scope pick is pinned sticky under the REQUESTED (global) scope"
        )

    def test_cross_scope_union_skips_an_exhausted_member_for_the_next_healthy_one(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/spent/oauth"], scope="overlay-a")
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/healthy/oauth"], scope="overlay-b")
        reader = _FakeReader(
            {
                "anthropic/spent/oauth": _snapshot(u5=0.97, reset=timezone.now() + dt.timedelta(hours=2)),
                "anthropic/healthy/oauth": _snapshot(),
            }
        )
        _seed_from_reader(reader)
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH, scope=GLOBAL_SCOPE)
        assert chosen == "anthropic/healthy/oauth", "an exhausted union member is skipped for the next healthy account"

    def test_empty_union_returns_none_without_probing(self) -> None:
        # Nothing configured in ANY scope → fail-loud contract preserved: no override, no probe.
        reader = _FakeReader({})
        with _pass_echoes_path():
            assert PassPathSelector(reader=reader).select(TokenKind.OAUTH, scope=GLOBAL_SCOPE) is None
        assert reader.calls == [], "selection reads the store, never the network"

    def test_all_exhausted_union_raises_all_tokens_exhausted(self) -> None:
        soon = timezone.now() + dt.timedelta(hours=1)
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth"], scope="overlay-a")
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/b/oauth"], scope="overlay-b")
        reader = _FakeReader(
            {
                "anthropic/a/oauth": _snapshot(u5=0.97, reset=soon),
                "anthropic/b/oauth": _snapshot(u5=0.98, reset=soon + dt.timedelta(hours=1)),
            }
        )
        _seed_from_reader(reader)
        with _pass_echoes_path(), pytest.raises(AllTokensExhaustedError):
            PassPathSelector(reader=reader).select(TokenKind.OAUTH, scope=GLOBAL_SCOPE)


class TestSelectorSkipsUnusableCandidates(TestCase):
    def test_cached_fresh_but_exhausted_candidate_is_skipped_without_a_probe(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])
        exhausted = TokenHealthReading(
            organization_id="org-1",
            utilization_5h=0.97,
            utilization_7d=0.1,
            status_5h="allowed",
            status_7d="allowed",
            reset_5h=timezone.now() + dt.timedelta(hours=2),
            reset_7d=None,
        )
        AnthropicTokenUsage.objects.record("anthropic/a/oauth", exhausted, now=timezone.now())
        reader = _FakeReader({"anthropic/b/oauth": _snapshot()})
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        assert chosen == "anthropic/b/oauth"
        assert reader.calls == [], "selection reads the store, never the network"

    def test_cached_fresh_healthy_candidate_is_returned_without_a_probe(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth"])
        _seed_fresh_healthy_row("anthropic/a/oauth")
        reader = _FakeReader({})
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        assert chosen == "anthropic/a/oauth"
        assert reader.calls == [], "selection reads the store, never the network"


class TestSelectorStickiness(TestCase):
    def test_fresh_sticky_pick_is_reused_without_any_probe(self) -> None:
        # The HOT path: a fresh, healthy sticky row is served from the cache — never the network.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])
        _seed_fresh_healthy_row("anthropic/a/oauth")
        AnthropicActivePick.objects.set_pick("oauth", "", "anthropic/a/oauth")
        reader = _FakeReader({})
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        assert chosen == "anthropic/a/oauth"
        assert reader.calls == [], "selection reads the store, never the network"

    def test_second_select_reuses_the_first_pick_from_cache(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth"])
        reader = _FakeReader({"anthropic/a/oauth": _snapshot()})
        selector = PassPathSelector(reader=reader)
        with _pass_echoes_path():
            first = selector.select(TokenKind.OAUTH)
            second = selector.select(TokenKind.OAUTH)
        assert first == second == "anthropic/a/oauth"
        assert reader.calls == [], "selection reads the store, never the network"

    def test_sticky_dropped_from_every_routing_list_is_not_reused(self) -> None:
        # The operator removed the account; a healthy sticky row for it is not a licence
        # to keep routing to an entry this install is no longer configured for.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/b/oauth"])
        _seed_fresh_healthy_row("anthropic/a/oauth")
        _seed_fresh_healthy_row("anthropic/b/oauth")
        AnthropicActivePick.objects.set_pick("oauth", "", "anthropic/a/oauth")
        reader = _FakeReader({})
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        assert chosen == "anthropic/b/oauth"

    def test_expired_sticky_row_is_re_probed(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth"])
        stale = TokenHealthReading(
            organization_id="org-1",
            utilization_5h=0.1,
            utilization_7d=0.1,
            status_5h="allowed",
            status_7d="allowed",
            reset_5h=None,
            reset_7d=None,
        )
        AnthropicTokenUsage.objects.record("anthropic/a/oauth", stale, now=timezone.now() - 2 * HEALTH_TTL)
        AnthropicActivePick.objects.set_pick("oauth", "", "anthropic/a/oauth")
        reader = _FakeReader({"anthropic/a/oauth": _snapshot()})
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        assert chosen == "anthropic/a/oauth"
        assert reader.calls == [], "selection reads the store, never the network"


class TestSelectorRanksOnHeadroom(TestCase):
    """The routing pick follows headroom, not list position — the #118 defect."""

    def test_a_sticky_pick_in_the_warning_band_is_replaced_by_a_richer_account(self) -> None:
        # The measured live case: the pin sits at 96% weekly (BELOW the 0.99 exhaustion
        # limit, so never "exhausted") while a sibling idles at 5%.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])
        _seed_row("anthropic/a/oauth", u7=0.96)
        _seed_row("anthropic/b/oauth", u7=0.05)
        AnthropicActivePick.objects.set_pick("oauth", "", "anthropic/a/oauth")
        reader = _FakeReader({})

        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)

        assert chosen == "anthropic/b/oauth"
        assert AnthropicActivePick.objects.pick_for("oauth", "") == "anthropic/b/oauth"
        assert reader.calls == [], "selection reads the store, never the network"

    def test_a_healthy_sticky_pick_is_reused_with_no_re_rank(self) -> None:
        # The anti-thrash guard: a strictly richer sibling does NOT displace a healthy pin,
        # so the warm prompt-cache prefix survives across the whole healthy range.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])
        _seed_row("anthropic/a/oauth", u5=0.10, u7=0.10)
        _seed_row("anthropic/b/oauth")
        AnthropicActivePick.objects.set_pick("oauth", "", "anthropic/a/oauth")
        reader = _FakeReader({})

        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)

        assert chosen == "anthropic/a/oauth"
        assert reader.calls == []

    def test_the_best_measured_account_wins_over_the_first_in_list_order(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])
        _seed_row("anthropic/a/oauth", u7=0.90)
        _seed_row("anthropic/b/oauth", u7=0.05)
        reader = _FakeReader({})

        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)

        assert chosen == "anthropic/b/oauth"

    def test_a_measured_healthy_account_outranks_an_uncached_one(self) -> None:
        # We KNOW the measured one is rich; we know nothing about the other. Fabricating a
        # zero utilization for the unmeasured candidate would be a lie.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/uncached/oauth", "anthropic/m/oauth"])
        _seed_row("anthropic/m/oauth", u7=0.05)
        reader = _FakeReader({})

        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)

        assert chosen == "anthropic/m/oauth"

    def test_a_fresh_all_null_row_does_not_win_over_a_measured_one(self) -> None:
        # reading_from_metered writes utilization_5h=None and utilization_7d=None with no
        # rejected status: a fresh, non-exhausted, all-NULL row. used_fraction(None) reads
        # as 0.0, so headroom_at fabricates full headroom for BOTH windows unless a null
        # row is excluded from ranking outright — the same NULL-vs-zero conflation
        # migration 0097/0099 exists to remove, reintroduced here in the selector.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/null/oauth", "anthropic/measured/oauth"])
        unmeasured = TokenHealthReading(
            organization_id="org-1",
            utilization_5h=None,
            utilization_7d=None,
            status_5h="",
            status_7d="",
            reset_5h=None,
            reset_7d=None,
        )
        AnthropicTokenUsage.objects.record("anthropic/null/oauth", unmeasured, now=timezone.now())
        _seed_row("anthropic/measured/oauth", u5=0.5, u7=0.5)
        reader = _FakeReader({})

        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)

        assert chosen == "anthropic/measured/oauth"

    def test_a_strained_pin_holds_when_no_sibling_carries_a_fresh_measured_row(self) -> None:
        # The band re-ranks, but it may only move to a candidate we have actually measured.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/uncached/oauth"])
        _seed_row("anthropic/a/oauth", u7=0.96)
        AnthropicActivePick.objects.set_pick("oauth", "", "anthropic/a/oauth")
        reader = _FakeReader({})

        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)

        assert chosen == "anthropic/a/oauth"

    def test_all_uncached_candidates_keep_the_declared_order_and_never_probe(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])
        assert not AnthropicTokenUsage.objects.exists()
        reader = _FakeReader({})

        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)

        assert chosen == "anthropic/a/oauth"
        assert reader.calls == [], "an unmeasured list must never trigger a probe sweep"
        assert AnthropicActivePick.objects.pick_for("oauth", "") == "anthropic/a/oauth"

    def test_a_stale_exhausted_row_is_still_offered_when_nothing_is_measured(self) -> None:
        # The exhaustion gate is unchanged: only a FRESH exhausted verdict rules a
        # candidate out, so a cold or lagging table can never halt dispatch.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth"])
        spent = TokenHealthReading(
            organization_id="org-1",
            utilization_5h=0.1,
            utilization_7d=1.0,
            status_5h="allowed",
            status_7d=REJECTED_STATUS,
            reset_5h=None,
            reset_7d=None,
        )
        AnthropicTokenUsage.objects.record("anthropic/a/oauth", spent, now=timezone.now() - 30 * HEALTH_TTL)
        reader = _FakeReader({})

        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)

        assert chosen == "anthropic/a/oauth"

    def test_every_account_freshly_exhausted_still_raises_naming_the_earliest_reset(self) -> None:
        # Ranking must not weaken the loud refusal into a "least bad" pick.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])
        soonest = timezone.now() + dt.timedelta(hours=2)
        _seed_row("anthropic/a/oauth", u7=1.0, s7=REJECTED_STATUS, reset_7d=soonest)
        _seed_row("anthropic/b/oauth", u7=1.0, s7=REJECTED_STATUS, reset_7d=soonest + dt.timedelta(hours=5))
        reader = _FakeReader({})

        with _pass_echoes_path(), pytest.raises(AllTokensExhaustedError) as exc_info:
            PassPathSelector(reader=reader).select(TokenKind.OAUTH)

        assert exc_info.value.earliest_reset == soonest


class TestSpentAccountReleasesEveryScope(TestCase):
    """Recording exhaustion frees the sibling scopes too — verified by re-selecting."""

    def test_a_sibling_scope_re_selects_after_the_shared_account_is_spent(self) -> None:
        for scope in ("alpha", "beta"):
            ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"], scope=scope)
            AnthropicActivePick.objects.set_pick("oauth", scope, "anthropic/a/oauth")
        _seed_row("anthropic/b/oauth", u7=0.05)
        spent = TokenHealthReading(
            organization_id="org-1",
            utilization_5h=0.0,
            utilization_7d=1.0,
            status_5h="allowed",
            status_7d=REJECTED_STATUS,
            reset_5h=None,
            reset_7d=timezone.now() + dt.timedelta(days=2),
        )

        AnthropicTokenUsage.objects.record("anthropic/a/oauth", spent, now=timezone.now())

        reader = _FakeReader({})
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH, "beta")

        assert chosen == "anthropic/b/oauth"
        assert AnthropicActivePick.objects.pick_for("oauth", "alpha") is None


class TestFactoryWiring(TestCase):
    def test_default_no_config_fails_loud_for_the_api_key_credential(self) -> None:
        # No credential has a built-in default: with no routing list and no env var, the
        # metered API-key resolver fails loud naming its setting, never a dead default.
        with _pass_echoes_path(), pytest.raises(CredentialError) as caught:
            resolve_api_key_credential().resolve()
        assert _API_KEY_SETTING in str(caught.value)

    def test_configured_list_routes_the_resolved_credential(self) -> None:
        ConfigSetting.objects.set_value(_API_KEY_SETTING, ["anthropic/metered/api"])
        with (
            _pass_echoes_path(),
            patch("teatree.credential_config.read_api_key_status", return_value=_metered()),
        ):
            assert resolve_api_key_credential().resolve() == "anthropic/metered/api"

    def test_resolvers_return_the_expected_credential_classes(self) -> None:
        assert isinstance(resolve_api_key_credential(), AnthropicApiKeyCredential)
        assert isinstance(resolve_subscription_credential(), AnthropicSubscriptionCredential)


class TestInContainerSniffsTheForwardedCredential(TestCase):
    """In-container the eval credential is read off the ONE var the host forwarded.

    Inside the ephemeral eval Docker container the SQLite DB has zero tables
    (never migrated), so both a ``ConfigSetting`` read and a settings read are a
    guaranteed crash rather than a degraded path. The host resolves the credential,
    ``export``s it (setting its own var and STRIPPING the conflicting one), and
    forwards that single var via ``docker run -e`` — so which var is present IS the
    host's choice, and sniffing it needs no config read and no forwarded knob.
    """

    @contextmanager
    def _in_container(self, **extra_env: str) -> Iterator[None]:
        # A DB read that would raise exactly CI's crash ("no such table:
        # teatree_config_setting") proves the short-circuit never reaches it —
        # a raise here means the fix regressed, not that the DB was "empty".
        db_has_no_tables = OperationalError("no such table: teatree_config_setting")
        with (
            patch.dict(os.environ, {IN_CONTAINER_ENV_VAR: "1", **extra_env}, clear=True),
            patch("teatree.credential_config.ConfigSetting.objects.get_effective", side_effect=db_has_no_tables),
        ):
            yield

    def test_a_forwarded_oauth_token_resolves_the_subscription_credential(self) -> None:
        with self._in_container(CLAUDE_CODE_OAUTH_TOKEN="host-forwarded-oauth-token"):
            credential = resolve_eval_credential()
            assert isinstance(credential, AnthropicSubscriptionCredential)
            assert credential.resolve() == "host-forwarded-oauth-token", "the host-forwarded env var must win"

    def test_a_forwarded_api_key_resolves_the_metered_credential(self) -> None:
        with self._in_container(ANTHROPIC_API_KEY="host-forwarded-api-key"):
            credential = resolve_eval_credential()
            assert isinstance(credential, AnthropicApiKeyCredential)
            assert credential.resolve() == "host-forwarded-api-key"

    def test_the_sniff_ignores_a_stored_contradicting_provider(self) -> None:
        # The container's own config plane is unreadable, so the forwarded var — not a
        # provider pin — decides. A stored api_key provider must not flip a container
        # the host forwarded an OAuth token into.
        ConfigSetting.objects.set_value("agent_harness_provider", "api_key")
        with self._in_container(CLAUDE_CODE_OAUTH_TOKEN="host-forwarded-oauth-token"):
            assert isinstance(resolve_eval_credential(), AnthropicSubscriptionCredential)

    def test_outside_the_container_the_selector_still_runs_and_would_crash(self) -> None:
        # Control: WITHOUT the in-container marker, the same "no such table" DB
        # failure surfaces — proving the short-circuit above is container-gated,
        # not an accidental universal bypass of the selector.
        db_has_no_tables = OperationalError("no such table: teatree_config_setting")
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth"])
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("teatree.credential_config.ConfigSetting.objects.get_effective", side_effect=db_has_no_tables),
            pytest.raises(OperationalError),
        ):
            resolve_subscription_credential()


class TestSubscriptionRequiresConfiguredOAuthAccount(TestCase):
    """The subscription OAuth credential has NO default ``pass`` path.

    It resolves ONLY from the ``CLAUDE_CODE_OAUTH_TOKEN`` env var or a per-account
    entry selected from the ``anthropic_oauth_pass_paths`` routing list. With neither,
    resolution fails LOUD — it never silently reads the removed built-in
    ``anthropic/oauth-token`` entry.
    """

    def test_empty_routing_and_no_env_fails_loud_naming_the_setting(self) -> None:
        # Anti-vacuity: no routing list + no OAuth env → the loud error must point the
        # operator at the setting to configure, not at a dead default `pass` path.
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("teatree.llm.credentials.read_pass", return_value=""),
            pytest.raises(CredentialError) as caught,
        ):
            resolve_subscription_credential().resolve()
        assert _OAUTH_SETTING in str(caught.value), "the loud error must name anthropic_oauth_pass_paths to configure"

    def test_empty_routing_and_no_env_names_the_scope(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("teatree.llm.credentials.read_pass", return_value=""),
            pytest.raises(CredentialError) as caught,
        ):
            resolve_subscription_credential(scope="myoverlay").resolve()
        assert "myoverlay" in str(caught.value), "the loud error names the scope whose routing list is empty"

    def test_configured_routing_resolves_the_selected_accounts_token(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/acct/oauth"])
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("teatree.llm.credentials.read_pass", side_effect=lambda path: f"oauth-token-for::{path}"),
            patch("teatree.credential_config.read_rate_limits", return_value=_snapshot()),
        ):
            resolved = resolve_subscription_credential().resolve()
        assert resolved == "oauth-token-for::anthropic/acct/oauth"

    def test_env_token_resolves_without_reading_pass(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "env-oauth-token"}, clear=True):
            with patch("teatree.llm.credentials.read_pass") as read_pass_mock:
                resolved = resolve_subscription_credential().resolve()
            read_pass_mock.assert_not_called()
        assert resolved == "env-oauth-token", "the env token wins and no `pass` entry is read"


class TestResolveEvalCredential(TestCase):
    """``resolve_eval_credential`` derives the eval lane's credential from the provider.

    THE single seam every eval chokepoint routes through — one provider change must
    switch the whole lane at once. With no provider pinned the eval lane rides the
    subscription OAuth token.
    """

    @pytest.fixture(autouse=True)
    def _isolate_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_AGENT_HARNESS_PROVIDER", raising=False)

    def test_unpinned_provider_rides_the_subscription_oauth_token(self) -> None:
        credential = resolve_eval_credential()
        assert isinstance(credential, AnthropicSubscriptionCredential)
        assert credential.spec.env_var == "CLAUDE_CODE_OAUTH_TOKEN"
        assert credential.spec.conflicting_vars == ("ANTHROPIC_API_KEY",)

    def test_stored_api_key_provider_rides_the_api_key(self) -> None:
        ConfigSetting.objects.set_value("agent_harness_provider", "api_key")
        credential = resolve_eval_credential()
        assert isinstance(credential, AnthropicApiKeyCredential)
        assert credential.spec.env_var == "ANTHROPIC_API_KEY"
        assert credential.spec.conflicting_vars == ("CLAUDE_CODE_OAUTH_TOKEN",)

    def test_explicit_kind_wins_over_the_setting(self) -> None:
        ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")
        assert isinstance(resolve_eval_credential(kind=AgentHarnessProvider.API_KEY), AnthropicApiKeyCredential)

    def test_env_var_wins_over_the_store(self) -> None:
        ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")
        with patch.dict(os.environ, {"T3_AGENT_HARNESS_PROVIDER": "api_key"}):
            assert isinstance(resolve_eval_credential(), AnthropicApiKeyCredential)


class TestResolveEvalCredentialUsesActiveOverlayScope(TestCase):
    """The eval credential resolves at the ACTIVE OVERLAY's routing scope, not global.

    Regression for the harness gap: ``anthropic_oauth_pass_paths`` configured only at
    the active overlay's scope (``T3_OVERLAY_NAME``) with an EMPTY global list must still
    route the eval's subscription OAuth account — the resolver reads the active overlay
    from the env and the selector's overlay→global fallback finds the overlay list.
    Pre-fix the eval resolver defaulted to ``GLOBAL_SCOPE`` regardless of the active
    overlay, so the empty global list left ``resolve()`` failing loud even though the
    per-overlay routing was configured.
    """

    @pytest.fixture(autouse=True)
    def _isolate_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS_PROVIDER", raising=False)

    def test_overlay_scoped_routing_resolves_when_global_is_empty(self) -> None:
        # anthropic_oauth_pass_paths set ONLY at the overlay scope; the global list is empty.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/t3-teatree/oauth"], scope="t3-teatree")
        with (
            patch.dict(os.environ, {"T3_OVERLAY_NAME": "t3-teatree"}, clear=True),
            patch("teatree.llm.credentials.read_pass", side_effect=lambda path: f"oauth-token-for::{path}"),
            patch("teatree.credential_config.read_rate_limits", return_value=_snapshot()),
        ):
            resolved = resolve_eval_credential().resolve()
        assert resolved == "oauth-token-for::anthropic/t3-teatree/oauth", (
            "the eval credential must route the overlay-scoped OAuth account, not fail on the empty global list"
        )

    def test_no_active_overlay_still_resolves_the_global_list(self) -> None:
        # Control: with no active overlay the global routing list is used unchanged.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/global/oauth"])
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("teatree.llm.credentials.read_pass", side_effect=lambda path: f"oauth-token-for::{path}"),
            patch("teatree.credential_config.read_rate_limits", return_value=_snapshot()),
        ):
            resolved = resolve_eval_credential().resolve()
        assert resolved == "oauth-token-for::anthropic/global/oauth"

    def test_no_active_overlay_auto_resolves_the_overlay_only_account(self) -> None:
        # The headline fix: NO active overlay + routing configured ONLY at an overlay scope
        # + an EMPTY global list. A bare eval must auto-resolve the overlay-scoped account
        # via the cross-scope fallback — no manual CLAUDE_CODE_OAUTH_TOKEN export.
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/t3-teatree/oauth"], scope="t3-teatree")
        with (
            patch.dict(os.environ, {}, clear=True),  # no T3_OVERLAY_NAME → the request scope is GLOBAL
            patch("teatree.llm.credentials.read_pass", side_effect=lambda path: f"oauth-token-for::{path}"),
            patch("teatree.credential_config.read_rate_limits", return_value=_snapshot()),
        ):
            resolved = resolve_eval_credential().resolve()
        assert resolved == "oauth-token-for::anthropic/t3-teatree/oauth", (
            "a bare eval (no active overlay) must route the overlay-scoped OAuth account, not fail loud"
        )

    def test_nothing_configured_anywhere_fails_loud_naming_the_setting(self) -> None:
        # The fail-loud contract is preserved: with NO OAuth account in any scope, a bare
        # eval still fails loud naming the setting to configure — never a dead default.
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("teatree.llm.credentials.read_pass", return_value=""),
            pytest.raises(CredentialError) as caught,
        ):
            resolve_eval_credential().resolve()
        assert _OAUTH_SETTING in str(caught.value), "the loud error names anthropic_oauth_pass_paths to configure"


class TestReactiveExhaustionRecordsProbedTruth(TestCase):
    """The reactive writer records the account's OWN measured health, never a synthesis.

    The SDK's mid-run limit signal carries only a reset instant and a weekly flag, and it
    names neither the binding window reliably nor the account that signed the failing
    request. A verdict built from those constants can be wrong in every field — including
    stamping a DIFFERENT account's reset onto this account's row.
    """

    _SPENT = "anthropic/spent/oauth"
    _OTHER = "anthropic/other/oauth"

    def setUp(self) -> None:
        # Every instant is anchored to ONE moment and lies in its future. Absolute dates
        # stop binding the day the wall clock walks past them: the recorded block reads
        # spent, the cached verdict reads stale, and every assertion below goes vacuous.
        self.now = timezone.now()
        # A reset that belongs to a DIFFERENT account — what the SDK signal supplies.
        self.foreign_reset = self.now + dt.timedelta(days=5)
        self.own_5h_reset = self.now + dt.timedelta(hours=1)
        self.own_7d_reset = self.now + dt.timedelta(days=7)

    def _route(self, paths: list[str], sticky: str) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, paths, scope=GLOBAL_SCOPE)
        AnthropicActivePick.objects.set_pick(TokenKind.OAUTH.value, GLOBAL_SCOPE, sticky)

    def _record(
        self,
        snapshots: dict[str, RateLimitSnapshot],
        *,
        tokens: dict[str, str] | None = None,
        unreachable: set[str] | None = None,
        weekly: bool = True,
        now: dt.datetime | None = None,
    ) -> str | None:
        reader = _FakeReader(snapshots)
        blocked = unreachable or set()

        def probing_reader(token: str, *, is_oauth: bool) -> RateLimitSnapshot:
            if token in blocked:
                msg = "probe failed"
                raise RateLimitProbeError(msg)
            return reader(token, is_oauth=is_oauth)

        # `snapshots` is keyed by TOKEN and the secret reader is asked for a PASS PATH, so
        # the map is built by inverting the prefix. Keying it on `snapshots` directly made
        # every read miss, and every case below silently took the unprobeable fallback.
        resolved = tokens if tokens is not None else {token.removeprefix("T-"): token for token in snapshots}
        return record_reactive_exhaustion_and_reselect(
            scope=GLOBAL_SCOPE,
            limit=ReactiveLimit(resets_at=self.foreign_reset, weekly=weekly),
            now=now or self.now,
            prober=AccountProber(reader=probing_reader, secret_reader=lambda path: resolved.get(path, "")),
        )

    def _record_the_last_account(self, snapshots: dict[str, RateLimitSnapshot], **kwargs: object) -> None:
        """Record the exhaustion of the ONLY routed account, whose re-selection then parks.

        The park is the documented outcome once nothing healthy is left, and it happens
        AFTER the row is written — which is what these cases are about. Letting it
        propagate would end the test before a single assertion ran.
        """
        with pytest.raises(AllTokensExhaustedError):
            self._record(snapshots, **kwargs)  # type: ignore[arg-type]

    def test_the_recorded_reset_is_the_probed_accounts_own_never_the_signals(self) -> None:
        self._route([self._SPENT], self._SPENT)
        self._record_the_last_account(
            {
                f"T-{self._SPENT}": replace(
                    _snapshot(org="org-spent", u5=1.0, u7=0.0),
                    unified_5h_status=REJECTED_STATUS,
                    unified_5h_reset=self.own_5h_reset,
                    unified_7d_reset=self.own_7d_reset,
                )
            },
        )

        row = AnthropicTokenUsage.objects.get(pass_path=self._SPENT)
        assert row.reset_5h == self.own_5h_reset
        assert row.reset_7d == self.own_7d_reset
        assert self.foreign_reset not in {row.reset_5h, row.reset_7d}, (
            "another account's reset must be impossible to write onto this row"
        )
        assert row.organization_id == "org-spent", "a probed row identifies the account it measured"

    def test_the_recorded_windows_are_measured_not_forced_to_zero_and_one(self) -> None:
        self._route([self._SPENT], self._SPENT)
        self._record_the_last_account(
            {f"T-{self._SPENT}": _snapshot(org="org-spent", u5=0.62, u7=0.995, s7=REJECTED_STATUS)}
        )

        row = AnthropicTokenUsage.objects.get(pass_path=self._SPENT)
        assert row.utilization_5h == pytest.approx(0.62), "a weekly hit must not zero the 5h window"
        assert row.utilization_7d == pytest.approx(0.995)
        assert row.is_exhausted

    def test_a_probe_that_reports_the_five_hour_claim_blocks_that_window(self) -> None:
        self._route([self._SPENT], self._SPENT)
        self._record_the_last_account(
            {
                f"T-{self._SPENT}": replace(
                    _snapshot(org="org-spent", u5=0.10, u7=0.10),
                    unified_5h_status=REJECTED_STATUS,
                    unified_5h_reset=self.own_5h_reset,
                    unified_7d_reset=self.own_7d_reset,
                )
            },
            weekly=True,
        )

        row = AnthropicTokenUsage.objects.get(pass_path=self._SPENT)
        assert row.blocking == {Window.FIVE_HOUR}, "the measured 5h refusal wins over the signal's weekly flag"
        assert row.frees_up_at == self.own_5h_reset

    def test_an_unprobeable_account_records_an_unverified_verdict_capped_at_the_ttl(self) -> None:
        self._route([self._SPENT], self._SPENT)
        self._record_the_last_account(
            {f"T-{self._SPENT}": _snapshot(org="org-spent")}, unreachable={f"T-{self._SPENT}"}
        )

        row = AnthropicTokenUsage.objects.get(pass_path=self._SPENT)
        assert row.is_exhausted, "the account still hit a limit, so it is still routed off"
        assert row.valid_until == self.now + HEALTH_TTL, (
            "an unmeasured verdict self-corrects in minutes instead of stranding the account"
        )

    def test_an_account_with_no_stored_token_records_an_unverified_verdict(self) -> None:
        self._route([self._SPENT], self._SPENT)
        self._record_the_last_account({}, tokens={})

        row = AnthropicTokenUsage.objects.get(pass_path=self._SPENT)
        assert row.is_exhausted
        assert row.valid_until == self.now + HEALTH_TTL

    def test_a_healthy_sibling_account_is_returned_for_rotation(self) -> None:
        self._route([self._SPENT, self._OTHER], self._SPENT)
        chosen = self._record(
            {
                f"T-{self._SPENT}": _snapshot(org="org-spent", u7=0.995, s7=REJECTED_STATUS),
                f"T-{self._OTHER}": _snapshot(org="org-other", u5=0.05, u7=0.05),
            }
        )
        assert chosen == self._OTHER

    def test_nothing_is_recorded_when_no_account_is_routed(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, [self._SPENT], scope=GLOBAL_SCOPE)
        assert self._record({}, tokens={}) is None
        assert AnthropicTokenUsage.objects.count() == 0


class TestReactiveExhaustionBindsTheSigningAccount(TestCase):
    """The verdict lands on the account that SIGNED the failing request, not the sticky one.

    The sticky pointer is shared per scope and moves whenever another dispatch re-selects,
    so reading it at WRITE time attributes one account's limit — and its reset — to
    whichever account happens to be pinned minutes later. The dispatch resolves an account
    before the turn runs; that is the account the limit belongs to.
    """

    _SIGNED = "anthropic/signed/oauth"
    _MOVED_TO = "anthropic/moved-to/oauth"

    def setUp(self) -> None:
        self.now = timezone.now()
        self.signal_reset = self.now + dt.timedelta(days=5)
        self.own_5h_reset = self.now + dt.timedelta(hours=1)
        ConfigSetting.objects.set_value(_OAUTH_SETTING, [self._SIGNED, self._MOVED_TO], scope=GLOBAL_SCOPE)
        # The pointer has MOVED off the signing account since the failing request was sent.
        AnthropicActivePick.objects.set_pick(TokenKind.OAUTH.value, GLOBAL_SCOPE, self._MOVED_TO)

    def _prober(self) -> AccountProber:
        health = {
            self._SIGNED: replace(
                _snapshot(org="org-signed", u5=1.0, u7=0.0),
                unified_5h_status=REJECTED_STATUS,
                unified_5h_reset=self.own_5h_reset,
            ),
            self._MOVED_TO: _snapshot(org="org-moved-to", u5=0.05, u7=0.05),
        }
        return AccountProber(reader=_FakeReader(health), secret_reader=lambda path: path)

    def test_the_verdict_is_written_on_the_signing_account(self) -> None:
        record_reactive_exhaustion_and_reselect(
            scope=GLOBAL_SCOPE,
            limit=ReactiveLimit(resets_at=self.signal_reset, weekly=True, pass_path=self._SIGNED),
            now=self.now,
            prober=self._prober(),
        )

        row = AnthropicTokenUsage.objects.get(pass_path=self._SIGNED)
        assert row.organization_id == "org-signed", "the row must measure the account that hit the limit"
        assert row.is_exhausted

    def test_the_account_the_pointer_moved_to_is_left_untouched(self) -> None:
        record_reactive_exhaustion_and_reselect(
            scope=GLOBAL_SCOPE,
            limit=ReactiveLimit(resets_at=self.signal_reset, weekly=True, pass_path=self._SIGNED),
            now=self.now,
            prober=self._prober(),
        )

        assert not AnthropicTokenUsage.objects.filter(pass_path=self._MOVED_TO).exists(), (
            "a healthy bystander must not be marked by another account's limit"
        )

    def test_without_an_attributed_account_the_sticky_pointer_still_decides(self) -> None:
        record_reactive_exhaustion_and_reselect(
            scope=GLOBAL_SCOPE,
            limit=ReactiveLimit(resets_at=self.signal_reset, weekly=True),
            now=self.now,
            prober=self._prober(),
        )

        assert AnthropicTokenUsage.objects.filter(pass_path=self._MOVED_TO).exists(), (
            "a caller with no routed account (an unrouted harness) keeps today's behaviour"
        )


class TestReactiveExhaustionNeverInventsAWeeklyRefusal(TestCase):
    """A 5h-only limit must never be recorded as a weekly one, whatever the SDK's cause says.

    The SDK's ``weekly`` flag and its ``resets_at`` are the only facts the old synthesis
    had, and a weekly verdict is trusted for up to seven days — so one misclassified 5h
    limit refused an account that was re-arming the same day.
    """

    _SPENT = "anthropic/spent/oauth"

    def setUp(self) -> None:
        self.now = timezone.now()
        self.foreign_weekly_reset = self.now + dt.timedelta(days=5)
        self.own_5h_reset = self.now + dt.timedelta(hours=1)
        ConfigSetting.objects.set_value(_OAUTH_SETTING, [self._SPENT], scope=GLOBAL_SCOPE)
        AnthropicActivePick.objects.set_pick(TokenKind.OAUTH.value, GLOBAL_SCOPE, self._SPENT)

    def _record_five_hour_truth_under_a_weekly_signal(self) -> None:
        health = {
            self._SPENT: replace(
                _snapshot(org="org-spent", u5=1.0, u7=0.0),
                unified_5h_status=REJECTED_STATUS,
                unified_5h_reset=self.own_5h_reset,
                unified_7d_status="allowed",
                unified_status=REJECTED_STATUS,
                representative_claim=Window.FIVE_HOUR.value,
            )
        }
        with pytest.raises(AllTokensExhaustedError):
            record_reactive_exhaustion_and_reselect(
                scope=GLOBAL_SCOPE,
                limit=ReactiveLimit(resets_at=self.foreign_weekly_reset, weekly=True, pass_path=self._SPENT),
                now=self.now,
                prober=AccountProber(reader=_FakeReader(health), secret_reader=lambda path: path),
            )

    def test_only_the_measured_five_hour_window_blocks(self) -> None:
        self._record_five_hour_truth_under_a_weekly_signal()

        row = AnthropicTokenUsage.objects.get(pass_path=self._SPENT)
        assert row.blocking == {Window.FIVE_HOUR}
        assert row.status_7d != REJECTED_STATUS, "the SDK's weekly flag must not reject an allowed week"
        assert row.utilization_7d == pytest.approx(0.0), "an idle week must not be forced to 100%"

    def test_the_account_re_arms_on_its_own_five_hour_reset(self) -> None:
        self._record_five_hour_truth_under_a_weekly_signal()

        row = AnthropicTokenUsage.objects.get(pass_path=self._SPENT)
        assert row.frees_up_at == self.own_5h_reset
        assert row.reset_7d != self.foreign_weekly_reset, "the signal's instant must never reach the row"
