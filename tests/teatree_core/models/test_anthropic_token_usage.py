r"""The Anthropic per-account health cache (``teatree.core.models.anthropic_token_usage``).

The exhaustion rule and the ``valid_until`` policy are pure (no DB), so they are
parametrized in plain pytest classes; the ``record`` upsert and the row-level
freshness/reset accessors are DB-backed and use ``django.test.TestCase``.
"""

import datetime as dt
from dataclasses import replace

import pytest
from django.test import TestCase

from teatree.core.admission_governor import read_quota_signal
from teatree.core.models import AnthropicActivePick, AnthropicTokenUsage
from teatree.core.models.anthropic_token_usage import (
    HEALTH_TTL,
    REJECTED_STATUS,
    UTILIZATION_5H_LIMIT,
    UTILIZATION_7D_LIMIT,
    WARNING_5H,
    WARNING_7D,
    AnthropicTokenUsageManager,
    TokenHealthReading,
    UnifiedVerdict,
    Window,
    fingerprint_token,
)

_NOW = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)


def _reading(
    *,
    u5: float | None = 0.0,
    u7: float | None = 0.0,
    s7: str = "allowed",
    reset_5h: dt.datetime | None = None,
    reset_7d: dt.datetime | None = None,
) -> TokenHealthReading:
    return TokenHealthReading(
        organization_id="org-1",
        utilization_5h=u5,
        utilization_7d=u7,
        status_5h="allowed",
        status_7d=s7,
        reset_5h=reset_5h,
        reset_7d=reset_7d,
    )


def _rejected(claim: str) -> UnifiedVerdict:
    return UnifiedVerdict(status="rejected", representative_claim=claim)


class TestExhaustionRule:
    @pytest.mark.parametrize(
        ("u5", "u7", "s7", "exhausted"),
        [
            (0.30, 0.80, "allowed", False),
            (0.95, 0.0, "allowed", True),
            (0.9499, 0.0, "allowed", False),
            (0.0, 0.99, "allowed", True),
            (0.0, 0.9899, "allowed", False),
            (0.0, 0.0, "rejected", True),
        ],
    )
    def test_exhaustion_thresholds(self, u5: float, u7: float, s7: str, *, exhausted: bool) -> None:
        assert _reading(u5=u5, u7=u7, s7=s7).is_exhausted is exhausted

    def test_unknown_utilization_is_not_spent(self) -> None:
        assert _reading(u5=None, u7=None).is_exhausted is False

    def test_a_rejected_five_hour_window_is_exhaustion(self) -> None:
        # The 5h status was stored but never read, so a rejected 5h window whose
        # utilization header was absent read as HEALTHY.
        rejected_5h = replace(_reading(u5=None), status_5h="rejected")
        assert rejected_5h.is_exhausted is True
        assert rejected_5h.blocking == {Window.FIVE_HOUR}


class TestRepresentativeClaimDecidesTheWindow:
    """``representative-claim`` names the binding window; utilizations alone cannot.

    Both windows are below their thresholds and both carry a reset, so the ONLY thing
    that can attribute the account-wide ``rejected`` verdict is the claim.
    """

    _RESET_5H = _NOW + dt.timedelta(hours=2)
    _RESET_7D = _NOW + dt.timedelta(days=3)

    def _claimed(self, claim: str) -> TokenHealthReading:
        return replace(
            _reading(u5=0.10, u7=0.10, reset_5h=self._RESET_5H, reset_7d=self._RESET_7D),
            verdict=_rejected(claim),
        )

    def test_five_hour_claim_blocks_the_five_hour_window(self) -> None:
        reading = self._claimed("five_hour")
        assert reading.blocking == {Window.FIVE_HOUR}
        assert reading.valid_until(_NOW) == self._RESET_5H

    def test_seven_day_claim_blocks_the_seven_day_window(self) -> None:
        reading = self._claimed("seven_day")
        assert reading.blocking == {Window.SEVEN_DAY}
        assert reading.valid_until(_NOW) == self._RESET_7D

    def test_an_allowed_verdict_never_blocks_its_claimed_window(self) -> None:
        allowed = replace(
            _reading(u5=0.10, u7=0.10),
            verdict=UnifiedVerdict(status="allowed_warning", representative_claim="seven_day"),
        )
        assert allowed.blocking == set()

    def test_an_unrecognised_claim_falls_back_to_the_window_thresholds(self) -> None:
        assert replace(_reading(u5=0.10), verdict=_rejected("thirty_day")).blocking == set()


class TestValidUntilPolicy:
    def test_healthy_token_expires_after_the_short_ttl(self) -> None:
        # Healthy with distant resets → re-probe after the TTL, not at the far reset.
        reading = _reading(reset_5h=_NOW + dt.timedelta(hours=3), reset_7d=_NOW + dt.timedelta(days=5))
        assert reading.valid_until(_NOW) == _NOW + HEALTH_TTL

    def test_healthy_token_never_outlives_a_nearer_reset(self) -> None:
        near = _NOW + dt.timedelta(minutes=2)
        assert _reading(reset_5h=near).valid_until(_NOW) == near

    def test_exhausted_token_is_valid_until_its_blocking_window_resets(self) -> None:
        # An exhausted 5h window → not re-probed until the 5h reset (far past the TTL).
        reset = _NOW + dt.timedelta(hours=2)
        assert _reading(u5=0.97, reset_5h=reset).valid_until(_NOW) == reset

    def test_two_exhausted_windows_wait_for_the_later_reset(self) -> None:
        reset_5h = _NOW + dt.timedelta(hours=2)
        reset_7d = _NOW + dt.timedelta(days=3)
        assert _reading(u5=0.97, u7=0.995, reset_5h=reset_5h, reset_7d=reset_7d).valid_until(_NOW) == reset_7d

    def test_exhausted_without_a_known_reset_falls_back_to_the_ttl(self) -> None:
        assert _reading(s7="rejected").valid_until(_NOW) == _NOW + HEALTH_TTL


class TestUnverifiedVerdictExpiresAtTheTtl:
    """An exhausted verdict nobody measured is trusted for minutes, not until a reset.

    A verdict synthesized because the account could not be probed can name the wrong
    window or the wrong account; capping it at the TTL makes that self-correct.
    """

    _RESET = _NOW + dt.timedelta(days=6)

    def test_verified_exhausted_verdict_is_trusted_until_its_blocking_reset(self) -> None:
        verified = _reading(u7=1.0, s7="rejected", reset_7d=self._RESET)
        assert verified.verified is True
        assert verified.valid_until(_NOW) == self._RESET

    def test_unverified_exhausted_verdict_expires_after_the_health_ttl(self) -> None:
        unverified = replace(_reading(u7=1.0, s7="rejected", reset_7d=self._RESET), verified=False)
        assert unverified.valid_until(_NOW) == _NOW + HEALTH_TTL

    def test_unverified_healthy_verdict_follows_the_ordinary_healthy_policy(self) -> None:
        near = _NOW + dt.timedelta(minutes=2)
        assert replace(_reading(reset_5h=near), verified=False).valid_until(_NOW) == near


class TestTokenUsageManager(TestCase):
    def test_objects_is_the_token_usage_manager(self) -> None:
        assert isinstance(AnthropicTokenUsage.objects, AnthropicTokenUsageManager)

    def test_manager_record_upserts_via_the_manager_class(self) -> None:
        manager = AnthropicTokenUsage.objects
        assert isinstance(manager, AnthropicTokenUsageManager)
        row = manager.record("anthropic/mgr/oauth", _reading(u5=0.5), now=_NOW)
        assert row.pass_path == "anthropic/mgr/oauth"
        assert row.valid_until == _NOW + HEALTH_TTL


class TestRecordUpsert(TestCase):
    def test_record_creates_a_row_with_the_computed_valid_until(self) -> None:
        row = AnthropicTokenUsage.objects.record("anthropic/acct/oauth", _reading(u5=0.30, u7=0.80), now=_NOW)
        assert row.pass_path == "anthropic/acct/oauth"
        assert row.organization_id == "org-1"
        assert row.valid_until == _NOW + HEALTH_TTL
        assert not row.is_exhausted

    def test_unknown_utilization_stores_as_unknown(self) -> None:
        row = AnthropicTokenUsage.objects.record("anthropic/acct/oauth", _reading(u5=None, u7=None), now=_NOW)
        assert row.utilization_5h is None, "an unread window must not be persisted as measured headroom"
        assert row.utilization_7d is None
        assert not row.is_exhausted, "unknown is not spent — routing still offers the account"

    def test_record_is_idempotent_on_pass_path(self) -> None:
        AnthropicTokenUsage.objects.record("anthropic/acct/oauth", _reading(u5=0.10), now=_NOW)
        AnthropicTokenUsage.objects.record("anthropic/acct/oauth", _reading(u5=0.97), now=_NOW)
        rows = AnthropicTokenUsage.objects.filter(pass_path="anthropic/acct/oauth")
        assert rows.count() == 1
        assert rows.get().is_exhausted, "the re-probe overwrote the one row with the fresh verdict"


class TestRowHealthAccessors(TestCase):
    def test_is_fresh_tracks_valid_until(self) -> None:
        row = AnthropicTokenUsage.objects.record("anthropic/acct/oauth", _reading(), now=_NOW)
        assert row.is_fresh(_NOW + dt.timedelta(minutes=1))
        assert not row.is_fresh(_NOW + dt.timedelta(minutes=10))

    def test_earliest_reset_is_the_soonest_window(self) -> None:
        reset_5h = _NOW + dt.timedelta(hours=2)
        reset_7d = _NOW + dt.timedelta(days=3)
        row = AnthropicTokenUsage.objects.record(
            "anthropic/acct/oauth", _reading(reset_5h=reset_5h, reset_7d=reset_7d), now=_NOW
        )
        assert row.earliest_reset == reset_5h

    def test_earliest_reset_is_none_when_no_window_is_known(self) -> None:
        row = AnthropicTokenUsage.objects.record("anthropic/acct/oauth", _reading(), now=_NOW)
        assert row.earliest_reset is None

    def test_frees_up_at_ignores_an_idle_window_whose_reset_is_sooner(self) -> None:
        """The live flood signature: rejected on 7d, idle 5h rolling over every few minutes.

        ``earliest_reset`` points at the idle 5h window — an instant already in the PAST —
        so parking on it produced a window that self-cleared on the next recovery tick and
        DM'd the owner, once a minute. ``frees_up_at`` must name the 7-day window that is
        actually blocking.
        """
        stale_5h = _NOW - dt.timedelta(minutes=1)
        blocking_7d = _NOW + dt.timedelta(hours=14)
        row = AnthropicTokenUsage.objects.record(
            "anthropic/acct/oauth",
            _reading(u5=0.0, u7=1.0, s7="rejected", reset_5h=stale_5h, reset_7d=blocking_7d),
            now=_NOW,
        )
        assert row.is_exhausted, "a rejected 7-day window is exhaustion"
        assert row.earliest_reset == stale_5h, "the display accessor still reports the soonest reset on record"
        assert row.frees_up_at == blocking_7d, "the account re-arms when its BLOCKING window clears"
        assert row.frees_up_at > _NOW, "a blocking reset is never already in the past"

    def test_frees_up_at_is_the_latest_when_both_windows_block(self) -> None:
        reset_5h = _NOW + dt.timedelta(hours=2)
        reset_7d = _NOW + dt.timedelta(days=3)
        row = AnthropicTokenUsage.objects.record(
            "anthropic/acct/oauth",
            _reading(u5=0.99, u7=1.0, s7="rejected", reset_5h=reset_5h, reset_7d=reset_7d),
            now=_NOW,
        )
        assert row.frees_up_at == reset_7d, "both must clear before the account is usable"

    def test_frees_up_at_is_none_for_a_healthy_account(self) -> None:
        row = AnthropicTokenUsage.objects.record(
            "anthropic/acct/oauth",
            _reading(reset_5h=_NOW + dt.timedelta(hours=2)),
            now=_NOW,
        )
        assert not row.is_exhausted
        assert row.frees_up_at is None, "nothing is blocking, so there is nothing to re-arm to"

    def test_is_measured_is_false_when_neither_window_was_ever_read(self) -> None:
        # #118 medium: a metered API-key row (reading_from_metered) always carries this
        # shape — fresh, not exhausted, and utterly unmeasured. It must read the same as
        # having no row at all, never as maximal headroom.
        row = AnthropicTokenUsage.objects.record("anthropic/acct/api_key", _reading(u5=None, u7=None), now=_NOW)
        assert row.is_measured is False

    def test_is_measured_is_false_when_only_one_window_was_read(self) -> None:
        row = AnthropicTokenUsage.objects.record("anthropic/acct/oauth", _reading(u5=0.2, u7=None), now=_NOW)
        assert row.is_measured is False

    def test_is_measured_is_true_when_both_windows_were_read(self) -> None:
        row = AnthropicTokenUsage.objects.record("anthropic/acct/oauth", _reading(u5=0.2, u7=0.4), now=_NOW)
        assert row.is_measured is True

    def test_str_includes_pass_path_and_both_utilizations(self) -> None:
        rendered = str(
            AnthropicTokenUsage(pass_path="anthropic/x/oauth", utilization_5h=0.3, utilization_7d=0.8, valid_until=_NOW)
        )
        assert "anthropic/x/oauth" in rendered
        assert "5h=0.30" in rendered
        assert "7d=0.80" in rendered


class TestWarningBand:
    """The band BELOW exhaustion — the trigger that makes a strained sticky pick re-rank."""

    @staticmethod
    def _reading(*, u5: float | None, u7: float | None, s7: str = "allowed") -> TokenHealthReading:
        return TokenHealthReading(
            organization_id="org-1",
            utilization_5h=u5,
            utilization_7d=u7,
            status_5h="allowed",
            status_7d=s7,
            reset_5h=None,
            reset_7d=None,
        )

    @pytest.mark.parametrize(
        ("u5", "u7", "warning", "exhausted"),
        [
            (0.79, 0.89, False, False),
            (WARNING_5H, 0.0, True, False),
            (0.0, WARNING_7D, True, False),
            (0.0, 0.96, True, False),
            (UTILIZATION_5H_LIMIT, 0.0, True, True),
            (0.0, UTILIZATION_7D_LIMIT, True, True),
            (None, None, False, False),
        ],
    )
    def test_the_band_fires_below_the_exhaustion_limit(
        self, u5: float | None, u7: float | None, *, warning: bool, exhausted: bool
    ) -> None:
        reading = self._reading(u5=u5, u7=u7)

        assert reading.is_warning is warning
        assert reading.is_exhausted is exhausted

    def test_an_exhausted_reading_is_always_also_warning(self) -> None:
        # The band is a floor under exhaustion, never a disjoint bucket.
        assert self._reading(u5=1.0, u7=1.0).is_warning is True

    def test_a_rejected_status_alone_does_not_raise_the_numeric_band(self) -> None:
        # ``rejected`` is DEFINITIVE (it exhausts outright); the band stays numeric so a
        # row rendered HEALTHY today is never silently reclassified WARNING.
        reading = self._reading(u5=0.0, u7=0.0, s7=REJECTED_STATUS)

        assert reading.is_exhausted is True
        assert reading.is_warning is False


class TestRowWarningBand(TestCase):
    def test_the_stored_row_reports_the_same_band_as_the_reading(self) -> None:
        row = AnthropicTokenUsage.objects.record("anthropic/a/oauth", TestWarningBand._reading(u5=0.0, u7=0.96))

        assert row.is_warning is True
        assert row.is_exhausted is False

    def test_a_calm_row_is_not_in_the_band(self) -> None:
        row = AnthropicTokenUsage.objects.record("anthropic/b/oauth", TestWarningBand._reading(u5=0.1, u7=0.1))

        assert row.is_warning is False


class TestRecordUnpinsASpentAccount(TestCase):
    """``record`` is where BOTH writers converge, so the sweep can never be forgotten."""

    def _pin_everywhere(self, pass_path: str) -> None:
        for scope in ("", "alpha", "beta"):
            AnthropicActivePick.objects.set_pick("oauth", scope, pass_path)

    def test_recording_an_account_exhausted_unpins_it_in_every_scope(self) -> None:
        self._pin_everywhere("anthropic/a/oauth")
        AnthropicActivePick.objects.set_pick("api_key", "", "anthropic/e/api")

        AnthropicTokenUsage.objects.record(
            "anthropic/a/oauth", TestWarningBand._reading(u5=0.0, u7=1.0, s7=REJECTED_STATUS)
        )

        assert not AnthropicActivePick.objects.filter(pass_path="anthropic/a/oauth").exists()
        assert AnthropicActivePick.objects.pick_for("api_key", "") == "anthropic/e/api"

    def test_recording_a_warning_reading_leaves_every_pin_in_place(self) -> None:
        # WARNING is a per-scope re-rank trigger, taken at that scope's own next select()
        # from its own list — never a cross-scope eviction.
        self._pin_everywhere("anthropic/a/oauth")

        AnthropicTokenUsage.objects.record("anthropic/a/oauth", TestWarningBand._reading(u5=0.0, u7=0.96))

        assert AnthropicActivePick.objects.filter(pass_path="anthropic/a/oauth").count() == 3

    def test_recording_a_healthy_reading_leaves_every_pin_in_place(self) -> None:
        self._pin_everywhere("anthropic/a/oauth")

        AnthropicTokenUsage.objects.record("anthropic/a/oauth", TestWarningBand._reading(u5=0.1, u7=0.1))

        assert AnthropicActivePick.objects.filter(pass_path="anthropic/a/oauth").count() == 3


class TestFingerprintToken:
    def test_is_deterministic(self) -> None:
        assert fingerprint_token("TOK-alpha") == fingerprint_token("TOK-alpha")

    def test_differs_across_tokens(self) -> None:
        assert fingerprint_token("TOK-alpha") != fingerprint_token("TOK-beta")

    def test_never_contains_the_token(self) -> None:
        token = "TOK-super-secret"
        assert token not in fingerprint_token(token)

    def test_empty_token_is_the_unknown_marker(self) -> None:
        assert fingerprint_token("") == ""


class TestRecordCarriesTheProbedCredential(TestCase):
    def test_record_stores_the_probed_fingerprint(self) -> None:
        row = AnthropicTokenUsage.objects.record(
            "anthropic/acct", _reading(), now=_NOW, token_fingerprint=fingerprint_token("TOK-a")
        )
        assert row.token_fingerprint == fingerprint_token("TOK-a")

    def test_record_without_a_fingerprint_keeps_the_stored_one(self) -> None:
        AnthropicTokenUsage.objects.record(
            "anthropic/acct", _reading(), now=_NOW, token_fingerprint=fingerprint_token("TOK-a")
        )
        row = AnthropicTokenUsage.objects.record("anthropic/acct", _reading(u5=0.5), now=_NOW)
        assert row.token_fingerprint == fingerprint_token("TOK-a")

    def test_record_overwrites_the_fingerprint_on_a_rotation(self) -> None:
        AnthropicTokenUsage.objects.record(
            "anthropic/acct", _reading(), now=_NOW, token_fingerprint=fingerprint_token("TOK-a")
        )
        row = AnthropicTokenUsage.objects.record(
            "anthropic/acct", _reading(), now=_NOW, token_fingerprint=fingerprint_token("TOK-b")
        )
        assert row.token_fingerprint == fingerprint_token("TOK-b")

    def test_a_fresh_row_defaults_to_the_unknown_fingerprint(self) -> None:
        row = AnthropicTokenUsage.objects.record("anthropic/acct", _reading(), now=_NOW)
        assert row.token_fingerprint == ""


class TestExpireAll(TestCase):
    def test_expires_every_row_and_returns_the_count(self) -> None:
        AnthropicTokenUsage.objects.record("anthropic/a", _reading(), now=_NOW)
        AnthropicTokenUsage.objects.record("anthropic/b", _reading(), now=_NOW)

        assert AnthropicTokenUsage.objects.expire_all(now=_NOW) == 2
        assert not any(row.is_fresh(_NOW) for row in AnthropicTokenUsage.objects.all())

    def test_expiring_an_empty_cache_is_zero(self) -> None:
        assert AnthropicTokenUsage.objects.expire_all(now=_NOW) == 0

    def test_keeps_the_rows_for_display(self) -> None:
        AnthropicTokenUsage.objects.record("anthropic/a", _reading(u5=0.42), now=_NOW)
        AnthropicTokenUsage.objects.expire_all(now=_NOW)
        assert AnthropicTokenUsage.objects.get(pass_path="anthropic/a").utilization_5h == pytest.approx(0.42)

    def test_expiring_a_spent_fleet_unblocks_the_governor(self) -> None:
        for path in ("anthropic/a", "anthropic/b"):
            AnthropicTokenUsage.objects.record(path, _reading(u5=1.0, reset_5h=_NOW + dt.timedelta(hours=3)), now=_NOW)
        assert read_quota_signal(_NOW).all_accounts_exhausted is True

        AnthropicTokenUsage.objects.expire_all(now=_NOW)

        assert read_quota_signal(_NOW).all_accounts_exhausted is False
