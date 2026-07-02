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
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.config import EvalCredential
from teatree.core.models import AnthropicActivePick, AnthropicTokenUsage, ConfigSetting
from teatree.core.models.anthropic_token_usage import HEALTH_TTL, REJECTED_STATUS, TokenHealthReading
from teatree.credential_config import (
    AllTokensExhaustedError,
    PassPathSelector,
    TokenKind,
    reading_from,
    reading_from_metered,
    resolve_api_key_credential,
    resolve_eval_credential,
    resolve_subscription_credential,
)
from teatree.llm.credentials import AnthropicApiKeyCredential, AnthropicSubscriptionCredential
from teatree.llm.rate_limits import MeteredKeySnapshot, RateLimitProbeError, RateLimitSnapshot

_OAUTH_BUILTIN = "anthropic/oauth-token"
_API_KEY_BUILTIN = "anthropic/api-key"
_OAUTH_SETTING = "anthropic_oauth_pass_paths"
_API_KEY_SETTING = "anthropic_api_key_pass_paths"


def _snapshot(
    *, u5: float = 0.1, u7: float = 0.1, s7: str = "allowed", reset: dt.datetime | None = None
) -> RateLimitSnapshot:
    return RateLimitSnapshot(
        organization_id="org-1",
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
        assert reader.calls == [], "an unconfigured kind must not probe"


class TestSelectorRouting(TestCase):
    def test_routes_to_first_healthy_account_and_pins_it_sticky(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])
        reader = _FakeReader({"anthropic/a/oauth": _snapshot()})
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        assert chosen == "anthropic/a/oauth"
        assert reader.calls == ["anthropic/a/oauth"], "the first healthy account short-circuits the rest"
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
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH, scope="overlay-x")
        assert chosen == "anthropic/other/oauth", "own account exhausted → borrow another overlay's healthy account"

    def test_out_of_credits_api_key_is_treated_as_exhausted(self) -> None:
        # An out-of-credits metered key must not be routed to — routing collapses the
        # credit signal onto the same exhaustion refusal the selector already enforces.
        ConfigSetting.objects.set_value(_API_KEY_SETTING, ["anthropic/metered/api"])
        with (
            _pass_echoes_path(),
            patch("teatree.credential_config.read_api_key_status", return_value=_metered(out_of_credits=True)),
            pytest.raises(AllTokensExhaustedError),
        ):
            PassPathSelector().select(TokenKind.API_KEY)

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
        with _pass_echoes_path(), pytest.raises(AllTokensExhaustedError) as caught:
            PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        message = str(caught.value)
        assert "exhausted" in message
        assert soon.isoformat() in message, "the loud error names the soonest an account frees up"


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
        assert reader.calls == ["anthropic/b/oauth"], "the cached exhausted account is skipped, not re-probed"

    def test_candidate_whose_credential_cannot_resolve_is_skipped(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])
        reader = _FakeReader({"anthropic/b/oauth": _snapshot()})
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "teatree.llm.credentials.read_pass",
                side_effect=lambda path: "" if path == "anthropic/a/oauth" else path,
            ),
        ):
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        assert chosen == "anthropic/b/oauth", "an unresolvable credential is skipped for the next candidate"
        assert reader.calls == ["anthropic/b/oauth"], "the unresolvable account is never probed"

    def test_cached_fresh_healthy_candidate_is_returned_without_a_probe(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth"])
        _seed_fresh_healthy_row("anthropic/a/oauth")
        reader = _FakeReader({})
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        assert chosen == "anthropic/a/oauth"
        assert reader.calls == [], "a fresh healthy cached candidate is returned from the cache, never re-probed"

    def test_candidate_whose_probe_transport_fails_is_skipped(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth", "anthropic/b/oauth"])

        class _RaisingReader:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def __call__(self, token: str, *, is_oauth: bool) -> RateLimitSnapshot:
                self.calls.append(token)
                if token == "anthropic/a/oauth":
                    msg = "probe failed"
                    raise RateLimitProbeError(msg)
                return _snapshot()

        reader = _RaisingReader()
        with _pass_echoes_path():
            chosen = PassPathSelector(reader=reader).select(TokenKind.OAUTH)
        assert chosen == "anthropic/b/oauth", "a probe transport failure makes the candidate unusable, not fatal"
        assert reader.calls == ["anthropic/a/oauth", "anthropic/b/oauth"]


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
        assert reader.calls == [], "a fresh sticky pick must be read from the cache, not re-probed"

    def test_second_select_reuses_the_first_pick_from_cache(self) -> None:
        ConfigSetting.objects.set_value(_OAUTH_SETTING, ["anthropic/a/oauth"])
        reader = _FakeReader({"anthropic/a/oauth": _snapshot()})
        selector = PassPathSelector(reader=reader)
        with _pass_echoes_path():
            first = selector.select(TokenKind.OAUTH)
            second = selector.select(TokenKind.OAUTH)
        assert first == second == "anthropic/a/oauth"
        assert reader.calls == ["anthropic/a/oauth"], "the second select is a cache hit — exactly one probe total"

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
        assert reader.calls == ["anthropic/a/oauth"], "an expired sticky row re-probes"


class TestFactoryWiring(TestCase):
    def test_default_no_config_resolves_the_builtin_pass_path(self) -> None:
        with _pass_echoes_path():
            assert resolve_api_key_credential().resolve() == _API_KEY_BUILTIN
            assert resolve_subscription_credential().resolve() == _OAUTH_BUILTIN

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


class TestResolveEvalCredential(TestCase):
    """``resolve_eval_credential`` picks the credential KIND the eval lane rides (#2707 reversal).

    THE single seam every eval chokepoint routes through — flipping the knob must
    switch the whole lane at once. The default reverses #2707: the eval lane rides
    the subscription OAuth token, not the metered API key.
    """

    @pytest.fixture(autouse=True)
    def _isolate_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.config.CONFIG_PATH", tmp_path / ".teatree.toml")
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_EVAL_CREDENTIAL", raising=False)

    def test_default_setting_rides_the_subscription_oauth_token(self) -> None:
        credential = resolve_eval_credential()
        assert isinstance(credential, AnthropicSubscriptionCredential)
        assert credential.spec.env_var == "CLAUDE_CODE_OAUTH_TOKEN"
        assert credential.spec.conflicting_vars == ("ANTHROPIC_API_KEY",)

    def test_stored_metered_setting_rides_the_api_key(self) -> None:
        ConfigSetting.objects.set_value("eval_credential", "metered_api_key")
        credential = resolve_eval_credential()
        assert isinstance(credential, AnthropicApiKeyCredential)
        assert credential.spec.env_var == "ANTHROPIC_API_KEY"
        assert credential.spec.conflicting_vars == ("CLAUDE_CODE_OAUTH_TOKEN",)

    def test_explicit_kind_wins_over_the_setting(self) -> None:
        ConfigSetting.objects.set_value("eval_credential", "subscription_oauth")
        assert isinstance(resolve_eval_credential(kind=EvalCredential.METERED_API_KEY), AnthropicApiKeyCredential)

    def test_env_var_wins_over_the_store(self) -> None:
        ConfigSetting.objects.set_value("eval_credential", "subscription_oauth")
        with patch.dict(os.environ, {"T3_EVAL_CREDENTIAL": "metered_api_key"}):
            assert isinstance(resolve_eval_credential(), AnthropicApiKeyCredential)
