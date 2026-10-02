"""Tests for the ``t3 tokens`` per-account Anthropic health reporter.

The reporter (``teatree.token_report``) reads the SAME per-overlay OAuth / API-key
``pass``-path lists the routing selector uses, resolves each account's token, and probes
its health. These tests drive canned health + tokens through the injected reader / secret
reader (no network, no ``pass``) and assert the classified rows, that the DEFAULT path
never renders a stored verdict, the ``--cached`` inverse, the best-first ordering, and —
the load-bearing invariant — that a token value is NEVER emitted in the rendered table or
the JSON.
"""

import datetime as dt
import json
from dataclasses import replace
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage, TokenHealthReading, fingerprint_token
from teatree.core.models.config_setting import ConfigSetting
from teatree.credential_config import LIST_SETTING, TokenKind
from teatree.llm.rate_limits import MeteredKeySnapshot, OverageUsage, RateLimitProbeError, RateLimitSnapshot
from teatree.token_report import (
    TokenAccountPayload,
    TokenAccountRow,
    TokenReport,
    TokenSource,
    TokenStatus,
    render_table,
)


def _snapshot(
    *,
    org: str,
    u5h: float | None = 0.1,
    u7d: float | None = 0.1,
    status_7d: str = "allowed",
    overage: OverageUsage | None = None,
) -> RateLimitSnapshot:
    reset = dt.datetime(2026, 7, 8, 12, 0, tzinfo=dt.UTC)
    return RateLimitSnapshot(
        organization_id=org,
        unified_5h_status="allowed",
        unified_5h_utilization=u5h,
        unified_5h_reset=reset,
        unified_7d_status=status_7d,
        unified_7d_utilization=u7d,
        unified_7d_reset=reset,
        retry_after=None,
        overage=overage or OverageUsage(),
    )


def _live_snapshot(*, org: str, u5h: float = 0.09, u7d: float = 0.28) -> RateLimitSnapshot:
    """A healthy snapshot whose windows reset in the FUTURE, so its cached row stays fresh."""
    reset = timezone.now() + dt.timedelta(hours=3)
    return RateLimitSnapshot(
        organization_id=org,
        unified_5h_status="allowed",
        unified_5h_utilization=u5h,
        unified_5h_reset=reset,
        unified_7d_status="allowed",
        unified_7d_utilization=u7d,
        unified_7d_reset=reset,
        retry_after=None,
    )


def _metered(
    *,
    org: str,
    out_of_credits: bool = False,
    requests_remaining: int = 4999,
    requests_limit: int = 5000,
    tokens_remaining: int = 990000,
) -> MeteredKeySnapshot:
    return MeteredKeySnapshot(
        organization_id=org,
        out_of_credits=out_of_credits,
        requests_remaining=None if out_of_credits else requests_remaining,
        requests_limit=None if out_of_credits else requests_limit,
        tokens_remaining=None if out_of_credits else tokens_remaining,
        input_tokens_remaining=None,
        output_tokens_remaining=None,
    )


class FakeReader:
    """Maps token -> snapshot; a token in *unreachable* raises like a transport failure."""

    def __init__(self, snapshots: dict[str, RateLimitSnapshot], *, unreachable: set[str] | None = None) -> None:
        self._snapshots = snapshots
        self._unreachable = unreachable or set()
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, token: str, *, is_oauth: bool) -> RateLimitSnapshot:
        self.calls.append((token, is_oauth))
        if token in self._unreachable:
            msg = "probe failed"
            raise RateLimitProbeError(msg)
        return self._snapshots[token]


class FakeApiKeyReader:
    """Maps an API key -> metered snapshot; a key in *unreachable* raises like a transport failure."""

    def __init__(self, snapshots: dict[str, MeteredKeySnapshot], *, unreachable: set[str] | None = None) -> None:
        self._snapshots = snapshots
        self._unreachable = unreachable or set()
        self.calls: list[str] = []

    def __call__(self, token: str) -> MeteredKeySnapshot:
        self.calls.append(token)
        if token in self._unreachable:
            msg = "probe failed"
            raise RateLimitProbeError(msg)
        return self._snapshots[token]


class RecordingSecretReader:
    """Maps pass_path -> token (``""`` = no stored credential) and records lookups."""

    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = tokens
        self.calls: list[str] = []

    def __call__(self, pass_path: str) -> str:
        self.calls.append(pass_path)
        return self._tokens.get(pass_path, "")


def _configure(kind: TokenKind, paths: list[str], scope: str = "") -> None:
    ConfigSetting.objects.set_value(LIST_SETTING[kind], paths, scope=scope)


class TestTokenAccountPayloadShape:
    """The token-free JSON row shape (``TokenAccountPayload``) that ``as_dict`` emits."""

    def _row(self, **overrides: object) -> TokenAccountRow:
        base: dict[str, object] = {
            "account": "anthropic/x/oauth",
            "kind": TokenKind.OAUTH,
            "source": TokenSource.STORE,
            "scopes": ("",),
            "organization_id": "org-x",
            "utilization_5h": 0.1,
            "utilization_7d": 0.2,
            "weekly_reset": None,
            "status": TokenStatus.HEALTHY,
        }
        return TokenAccountRow(**(base | overrides))

    def test_as_dict_keys_match_the_payload_typeddict(self) -> None:
        payload: TokenAccountPayload = self._row().as_dict()
        assert set(payload) == set(TokenAccountPayload.__annotations__)

    def test_pass_row_carries_the_pass_source_discriminator(self) -> None:
        payload = self._row().as_dict()
        assert payload["account"] == "anthropic/x/oauth"
        assert payload["source"] == "pass"
        assert "pass_path" not in payload

    def test_oauth_row_payload_carries_utilization_not_per_minute_fields(self) -> None:
        payload = self._row().as_dict()
        assert payload["kind"] == "oauth"
        assert payload["status"] == "healthy"
        assert payload["utilization_5h"] == pytest.approx(0.1)
        assert payload["requests_remaining"] is None

    def test_reset_cells_are_a_dash_when_absent(self) -> None:
        row = self._row(weekly_reset=None, next_window_reset=None)
        assert row.col_reset == "—"
        assert row.col_next_window == "—"


class TokenReportRowsTest(TestCase):
    def test_classifies_health_across_scopes_and_kinds(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/healthy", "anthropic/oauth/warning"])
        _configure(TokenKind.OAUTH, ["anthropic/oauth/exhausted"], scope="teatree")
        _configure(TokenKind.API_KEY, ["anthropic/apikey/missing", "anthropic/apikey/unreachable"])
        secrets = RecordingSecretReader(
            {
                "anthropic/oauth/healthy": "TOK-healthy",
                "anthropic/oauth/warning": "TOK-warning",
                "anthropic/oauth/exhausted": "TOK-exhausted",
                "anthropic/apikey/missing": "",
                "anthropic/apikey/unreachable": "TOK-unreachable",
            }
        )
        reader = FakeReader(
            {
                "TOK-healthy": _snapshot(org="org-healthy", u5h=0.1, u7d=0.1),
                "TOK-warning": _snapshot(org="org-warning", u5h=0.85, u7d=0.2),
                "TOK-exhausted": _snapshot(org="org-exhausted", u5h=0.2, u7d=0.995),
            },
        )
        api_key_reader = FakeApiKeyReader({}, unreachable={"TOK-unreachable"})

        report = TokenReport(reader=reader, secret_reader=secrets, api_key_reader=api_key_reader)
        rows = {row.account: row for row in report.rows()}

        assert rows["anthropic/oauth/healthy"].status is TokenStatus.HEALTHY
        assert rows["anthropic/oauth/warning"].status is TokenStatus.WARNING
        assert rows["anthropic/oauth/exhausted"].status is TokenStatus.EXHAUSTED
        assert rows["anthropic/apikey/missing"].status is TokenStatus.MISSING
        assert rows["anthropic/apikey/unreachable"].status is TokenStatus.UNREACHABLE
        assert rows["anthropic/oauth/healthy"].overlays_label == "global"
        assert rows["anthropic/oauth/exhausted"].overlays_label == "teatree"
        assert rows["anthropic/oauth/healthy"].source is TokenSource.STORE
        assert rows["anthropic/oauth/healthy"].organization_id == "org-healthy"
        # OAuth accounts probe with the oauth beta header; metered keys go through their own reader.
        assert ("TOK-healthy", True) in reader.calls
        assert "TOK-unreachable" in api_key_reader.calls

    def test_probe_upserts_the_shared_health_cache(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/healthy"])
        secrets = RecordingSecretReader({"anthropic/oauth/healthy": "TOK-healthy"})
        reader = FakeReader({"TOK-healthy": _snapshot(org="org-healthy")})

        TokenReport(reader=reader, secret_reader=secrets).rows()

        cached = AnthropicTokenUsage.objects.get(pass_path="anthropic/oauth/healthy")
        assert cached.organization_id == "org-healthy"

    def test_no_configured_accounts_yields_no_rows(self) -> None:
        assert TokenReport(reader=FakeReader({}), secret_reader=RecordingSecretReader({})).rows() == []


def _stored(
    pass_path: str, *, org: str, u7d: float = 0.1, status_7d: str = "allowed", now: dt.datetime | None = None
) -> AnthropicTokenUsage:
    return AnthropicTokenUsage.objects.record(
        pass_path,
        TokenHealthReading(
            organization_id=org,
            utilization_5h=0.1,
            utilization_7d=u7d,
            status_5h="allowed",
            status_7d=status_7d,
            reset_5h=None,
            reset_7d=None,
        ),
        now=now or timezone.now(),
    )


class DefaultPathNeverRendersTheCacheTest(TestCase):
    """The regression: a stored verdict must never reach the default report.

    A cached row is written by the reactive exhaustion path and can be wrong in every
    field — including carrying another account's reset. The report is an explicit
    operator command off every hot path, so it always probes.
    """

    def test_a_fresh_cache_row_is_ignored_and_the_account_is_probed(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/acct"])
        _stored("anthropic/oauth/acct", org="org-STALE", u7d=0.995, status_7d="rejected")
        secrets = RecordingSecretReader({"anthropic/oauth/acct": "TOK-live"})
        reader = FakeReader({"TOK-live": _snapshot(org="org-LIVE", u5h=0.02, u7d=0.03)})

        row = TokenReport(reader=reader, secret_reader=secrets).rows()[0]

        assert reader.calls == [("TOK-live", True)], "the fresh cache row must not short-circuit the probe"
        assert row.organization_id == "org-LIVE"
        assert row.status is TokenStatus.HEALTHY

    def test_the_probe_overwrites_the_stale_cache_row(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/acct"])
        _stored("anthropic/oauth/acct", org="org-STALE", u7d=0.995, status_7d="rejected")
        secrets = RecordingSecretReader({"anthropic/oauth/acct": "TOK-live"})
        reader = FakeReader({"TOK-live": _snapshot(org="org-LIVE", u5h=0.02, u7d=0.03)})

        TokenReport(reader=reader, secret_reader=secrets).rows()

        refreshed = AnthropicTokenUsage.objects.get(pass_path="anthropic/oauth/acct")
        assert refreshed.organization_id == "org-LIVE"
        assert not refreshed.is_exhausted

    def test_a_failed_probe_reports_unreachable_and_does_not_fall_back_to_the_cache(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/acct"])
        _stored("anthropic/oauth/acct", org="org-STALE")
        secrets = RecordingSecretReader({"anthropic/oauth/acct": "TOK-down"})
        reader = FakeReader({}, unreachable={"TOK-down"})

        row = TokenReport(reader=reader, secret_reader=secrets).rows()[0]

        assert row.status is TokenStatus.UNREACHABLE
        assert row.organization_id == "", "a failed probe must not borrow the cached org id"
        assert row.utilization_5h is None
        assert row.as_dict()["utilization_5h"] is None


class CachedRoutingViewTest(TestCase):
    """``--cached`` is the opt-in inverse: render the stored verdict, probe nothing."""

    def test_cached_mode_performs_zero_probes_and_renders_the_stored_verdict(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/acct"])
        _stored("anthropic/oauth/acct", org="org-stored", u7d=0.995, status_7d="rejected")
        secrets = RecordingSecretReader({"anthropic/oauth/acct": "TOK-live"})
        reader = FakeReader({"TOK-live": _snapshot(org="org-live")})

        row = TokenReport(reader=reader, secret_reader=secrets, from_cache=True).rows()[0]

        assert reader.calls == []
        assert secrets.calls == []
        assert row.organization_id == "org-stored"
        assert row.status is TokenStatus.EXHAUSTED

    def test_an_account_the_router_has_no_verdict_for_is_uncached(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/never-probed"])
        secrets = RecordingSecretReader({"anthropic/oauth/never-probed": "TOK"})
        row = TokenReport(reader=FakeReader({}), secret_reader=secrets, from_cache=True).rows()[0]
        assert row.status is TokenStatus.UNCACHED
        assert row.status.is_measured is False
        assert row.col_5h == "—"

    def test_cached_mode_says_so_in_the_title_and_shows_the_reading_age(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/acct"])
        _stored("anthropic/oauth/acct", org="org-stored", now=timezone.now() - dt.timedelta(hours=3))
        report = TokenReport(reader=FakeReader({}), secret_reader=RecordingSecretReader({}), from_cache=True)
        out = report.render()
        assert "CACHED" in out
        assert "as of" in out
        assert "3h ago" in out

    def test_the_live_report_shows_neither_the_cached_title_nor_the_age_column(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/acct"])
        secrets = RecordingSecretReader({"anthropic/oauth/acct": "TOK"})
        out = TokenReport(reader=FakeReader({"TOK": _snapshot(org="org-live")}), secret_reader=secrets).render()
        assert "CACHED" not in out
        assert "as of" not in out

    def test_cached_mode_never_probes_an_ad_hoc_token(self) -> None:
        reader = FakeReader({})
        report = TokenReport(
            reader=reader,
            secret_reader=RecordingSecretReader({}),
            ad_hoc_tokens=["sk-ant-oat01-ADHOC"],
            from_cache=True,
        )
        row = report.rows()[0]
        assert row.status is TokenStatus.UNCACHED
        assert reader.calls == []


class OAuthUnhappyRowsTest(TestCase):
    """The OAuth ``_row_for`` unhappy branches: no stored token, and a probe failure."""

    def test_oauth_account_with_no_stored_token_is_missing(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/missing"])
        secrets = RecordingSecretReader({"anthropic/oauth/missing": ""})
        rows = TokenReport(reader=FakeReader({}), secret_reader=secrets).rows()
        assert rows[0].status is TokenStatus.MISSING

    def test_oauth_probe_failure_is_unreachable(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/down"])
        secrets = RecordingSecretReader({"anthropic/oauth/down": "TOK-down"})
        reader = FakeReader({}, unreachable={"TOK-down"})
        rows = TokenReport(reader=reader, secret_reader=secrets).rows()
        assert rows[0].status is TokenStatus.UNREACHABLE

    def test_render_delegates_to_the_table_renderer(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/healthy"])
        secrets = RecordingSecretReader({"anthropic/oauth/healthy": "TOK"})
        reader = FakeReader({"TOK": _snapshot(org="org-x")})
        out = TokenReport(reader=reader, secret_reader=secrets).render()
        assert "anthropic/oauth/healthy" in out
        assert "HEALTHY" in out

    def test_a_non_list_config_value_yields_no_accounts(self) -> None:
        ConfigSetting.objects.set_value(LIST_SETTING[TokenKind.OAUTH], "not-a-list")
        assert TokenReport(reader=FakeReader({}), secret_reader=RecordingSecretReader({})).rows() == []


class ApiKeyReportRowsTest(TestCase):
    """Metered API-key rows: credit state + per-minute remaining, not weekly utilization."""

    def _api_key_report(
        self,
        path: str,
        token: str,
        snapshot: MeteredKeySnapshot | None,
        *,
        unreachable: set[str] | None = None,
    ) -> TokenReport:
        _configure(TokenKind.API_KEY, [path])
        secrets = RecordingSecretReader({path: token})
        snapshots = {token: snapshot} if snapshot is not None else {}
        api_key_reader = FakeApiKeyReader(snapshots, unreachable=unreachable)
        return TokenReport(reader=FakeReader({}), secret_reader=secrets, api_key_reader=api_key_reader)

    def test_funded_key_is_healthy_and_shows_per_minute_remaining(self) -> None:
        report = self._api_key_report("anthropic/apikey/funded", "TOK-funded", _metered(org="org-metered"))
        row = report.rows()[0]
        assert row.status is TokenStatus.HEALTHY
        assert row.organization_id == "org-metered"
        assert row.requests_remaining == 4999
        assert row.tokens_remaining == 990000
        payload = row.as_dict()
        assert payload["requests_remaining"] == 4999
        assert payload["tokens_remaining"] == 990000
        assert payload["utilization_5h"] is None, "api-key rows carry no weekly utilization"

    def test_funded_key_renders_remaining_and_never_emits_the_key(self) -> None:
        report = self._api_key_report("anthropic/apikey/funded", "SUPER-SECRET-API-KEY", _metered(org="org-metered"))
        out = render_table(report.rows())
        assert "HEALTHY" in out
        assert "req 4999/5000" in out
        assert "tok 990000" in out
        assert "credit state" in out, "the caption explains the metered columns"
        assert "SUPER-SECRET-API-KEY" not in out

    def test_out_of_credits_key_is_an_alarming_row(self) -> None:
        report = self._api_key_report(
            "anthropic/apikey/broke", "TOK-broke", _metered(org="org-broke", out_of_credits=True)
        )
        row = report.rows()[0]
        assert row.status is TokenStatus.OUT_OF_CREDITS
        assert row.as_dict()["status"] == "out_of_credits"
        assert "! OUT_OF_CREDITS" in render_table([row])

    def test_probe_failure_is_unreachable(self) -> None:
        report = self._api_key_report("anthropic/apikey/down", "TOK-down", None, unreachable={"TOK-down"})
        assert report.rows()[0].status is TokenStatus.UNREACHABLE

    def test_missing_key_is_missing(self) -> None:
        report = self._api_key_report("anthropic/apikey/missing", "", None)
        assert report.rows()[0].status is TokenStatus.MISSING


class TokenReportRenderTest(TestCase):
    def _rows(self) -> list:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/healthy", "anthropic/oauth/exhausted"])
        secrets = RecordingSecretReader(
            {
                "anthropic/oauth/healthy": "SUPER-SECRET-TOKEN-healthy",
                "anthropic/oauth/exhausted": "SUPER-SECRET-TOKEN-exhausted",
            }
        )
        reader = FakeReader(
            {
                "SUPER-SECRET-TOKEN-healthy": _snapshot(org="org-healthy", u5h=0.1, u7d=0.1),
                "SUPER-SECRET-TOKEN-exhausted": _snapshot(org="org-exhausted", u5h=0.2, u7d=0.995),
            }
        )
        return TokenReport(reader=reader, secret_reader=secrets).rows()

    def test_render_lists_accounts_and_statuses(self) -> None:
        out = render_table(self._rows())
        assert "anthropic/oauth/healthy" in out
        assert "HEALTHY" in out
        assert "EXHAUSTED" in out

    def test_render_marks_exhausted_rows(self) -> None:
        assert "! EXHAUSTED" in render_table(self._rows())

    def test_render_never_emits_a_token(self) -> None:
        out = render_table(self._rows())
        assert "SUPER-SECRET-TOKEN-healthy" not in out
        assert "SUPER-SECRET-TOKEN-exhausted" not in out

    def test_render_placeholder_when_nothing_configured(self) -> None:
        assert "No Anthropic accounts configured" in render_table([])

    def test_render_marks_missing_accounts_with_dashes(self) -> None:
        _configure(TokenKind.API_KEY, ["anthropic/apikey/missing"])
        secrets = RecordingSecretReader({"anthropic/apikey/missing": ""})
        rows = TokenReport(reader=FakeReader({}), secret_reader=secrets).rows()
        out = render_table(rows)
        assert "! MISSING" in out
        assert "—" in out
        assert rows[0].as_dict()["utilization_5h"] is None


class ResetWindowColumnsTest(TestCase):
    """Both window resets flow probe → cache → row, and both cells render local time.

    Regression for the ``t3 tokens`` weekly-reset ``—`` bug (#3258): the 7d reset was
    already persisted but the row also carries the 5h next-window reset now, and both
    render as a local datetime for an OAuth account while an API-key row shows ``—``.
    """

    _RESET_5H = dt.datetime(2026, 7, 15, 9, 0, tzinfo=dt.UTC)
    _RESET_7D = dt.datetime(2026, 7, 20, 18, 0, tzinfo=dt.UTC)

    def _oauth_rows(self) -> list[TokenAccountRow]:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/live"])
        secrets = RecordingSecretReader({"anthropic/oauth/live": "TOK-live"})
        snapshot = RateLimitSnapshot(
            organization_id="org-live",
            unified_5h_status="allowed",
            unified_5h_utilization=0.28,
            unified_5h_reset=self._RESET_5H,
            unified_7d_status="allowed",
            unified_7d_utilization=0.96,
            unified_7d_reset=self._RESET_7D,
            retry_after=None,
        )
        return TokenReport(reader=FakeReader({"TOK-live": snapshot}), secret_reader=secrets).rows()

    def test_probe_persists_both_window_resets_non_null(self) -> None:
        self._oauth_rows()
        cached = AnthropicTokenUsage.objects.get(pass_path="anthropic/oauth/live")
        assert cached.reset_5h == self._RESET_5H
        assert cached.reset_7d == self._RESET_7D

    def test_oauth_row_renders_both_reset_cells_as_local_time(self) -> None:
        row = self._oauth_rows()[0]
        assert row.col_reset == self._RESET_7D.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        assert row.col_next_window == self._RESET_5H.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        assert row.col_reset != "—"
        assert row.col_next_window != "—"

    def test_table_shows_the_5h_reset_column_and_both_local_times(self) -> None:
        out = render_table(self._oauth_rows())
        assert "5h reset" in out
        assert self._RESET_5H.astimezone().strftime("%Y-%m-%d %H:%M") in out
        assert self._RESET_7D.astimezone().strftime("%Y-%m-%d %H:%M") in out

    def test_json_payload_carries_both_window_resets(self) -> None:
        payload = self._oauth_rows()[0].as_dict()
        assert payload["weekly_reset"] == self._RESET_7D.astimezone().isoformat()
        assert payload["next_window_reset"] == self._RESET_5H.astimezone().isoformat()

    def test_api_key_row_shows_a_dash_for_both_reset_cells(self) -> None:
        _configure(TokenKind.API_KEY, ["anthropic/apikey/funded"])
        secrets = RecordingSecretReader({"anthropic/apikey/funded": "TOK-funded"})
        api_key_reader = FakeApiKeyReader({"TOK-funded": _metered(org="org-metered")})
        row = TokenReport(reader=FakeReader({}), secret_reader=secrets, api_key_reader=api_key_reader).rows()[0]
        assert row.col_reset == "—"
        assert row.col_next_window == "—"
        assert row.as_dict()["next_window_reset"] is None


class ExtraUsageColumnTest(TestCase):
    """The extra-usage (overage) balance: parsed from the same probe, rendered per state."""

    def _row(self, overage: OverageUsage) -> TokenAccountRow:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/acct"])
        secrets = RecordingSecretReader({"anthropic/oauth/acct": "TOK"})
        reader = FakeReader({"TOK": _snapshot(org="org-x", overage=overage)})
        return TokenReport(reader=reader, secret_reader=secrets).rows()[0]

    def test_available_overage_renders_its_utilization_percent(self) -> None:
        row = self._row(OverageUsage(status="allowed", utilization=0.95, in_use=True))
        assert row.col_extra_usage == "95%"
        assert "extra usage" in render_table([row])
        assert "95%" in render_table([row])

    def test_disabled_overage_renders_the_reason(self) -> None:
        for reason, cell in (
            ("org_level_disabled", "off"),
            ("out_of_credits", "out of credits"),
            ("org_spend_cap_reached", "spend cap"),
        ):
            with self.subTest(reason=reason):
                row = self._row(OverageUsage(status="disabled", disabled_reason=reason))
                assert row.col_extra_usage == cell

    def test_unreported_overage_utilization_renders_a_dash_not_zero(self) -> None:
        assert self._row(OverageUsage(status="allowed")).col_extra_usage == "—"

    def test_json_payload_carries_the_overage_fields(self) -> None:
        reset = dt.datetime(2026, 7, 20, 18, 0, tzinfo=dt.UTC)
        payload = self._row(OverageUsage(status="allowed", utilization=0.42, reset=reset, in_use=True)).as_dict()
        assert payload["overage_status"] == "allowed"
        assert payload["overage_utilization"] == pytest.approx(0.42)
        assert payload["overage_reset"] == reset.astimezone().isoformat()
        assert payload["overage_in_use"] is True
        assert payload["overage_disabled_reason"] == ""

    def test_a_cached_row_reports_no_overage_because_the_cache_does_not_carry_it(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/acct"])
        _stored("anthropic/oauth/acct", org="org-stored")
        row = TokenReport(reader=FakeReader({}), secret_reader=RecordingSecretReader({}), from_cache=True).rows()[0]
        assert row.col_extra_usage == "—"
        assert row.as_dict()["overage_status"] is None


class BestAccountFirstOrderingTest(TestCase):
    """Rows are ordered so the top one is the account a new task should run on."""

    _SOON = dt.datetime(2026, 7, 2, 12, 0, tzinfo=dt.UTC)
    _LATER = dt.datetime(2026, 7, 9, 12, 0, tzinfo=dt.UTC)

    def _rows(self) -> list[TokenAccountRow]:
        _configure(
            TokenKind.OAUTH,
            [
                "oauth/warning",
                "oauth/unreachable",
                "oauth/spent-later",
                "oauth/healthy",
                "oauth/missing",
                "oauth/spent-soon",
            ],
        )
        _configure(TokenKind.API_KEY, ["key/broke", "key/funded"])
        secrets = RecordingSecretReader(
            {
                "oauth/healthy": "T-healthy",
                "oauth/warning": "T-warning",
                "oauth/spent-soon": "T-spent-soon",
                "oauth/spent-later": "T-spent-later",
                "oauth/missing": "",
                "oauth/unreachable": "T-down",
                "key/funded": "T-funded",
                "key/broke": "T-broke",
            }
        )
        reader = FakeReader(
            {
                "T-healthy": _snapshot(org="org-healthy", u5h=0.05, u7d=0.05),
                "T-warning": _snapshot(org="org-warning", u5h=0.85, u7d=0.20),
                "T-spent-soon": replace(_snapshot(org="org-soon", u5h=0.99, u7d=0.20), unified_5h_reset=self._SOON),
                "T-spent-later": replace(_snapshot(org="org-later", u5h=0.99, u7d=0.20), unified_5h_reset=self._LATER),
            },
            unreachable={"T-down"},
        )
        api_key_reader = FakeApiKeyReader(
            {"T-funded": _metered(org="org-funded"), "T-broke": _metered(org="org-broke", out_of_credits=True)}
        )
        return TokenReport(reader=reader, secret_reader=secrets, api_key_reader=api_key_reader).rows()

    def test_full_ordering_is_best_first(self) -> None:
        assert [row.account for row in self._rows()] == [
            "oauth/healthy",
            "oauth/warning",
            "oauth/spent-soon",
            "oauth/spent-later",
            "key/funded",
            "key/broke",
            "oauth/missing",
            "oauth/unreachable",
        ]

    def test_ad_hoc_rows_stay_last_in_first_seen_order(self) -> None:
        _configure(TokenKind.OAUTH, ["oauth/spent"])
        secrets = RecordingSecretReader({"oauth/spent": "T-spent"})
        reader = FakeReader(
            {
                "T-spent": _snapshot(org="org-spent", u7d=0.995),
                "sk-ant-oat01-B": _snapshot(org="org-b", u5h=0.01, u7d=0.01),
                "sk-ant-oat01-A": _snapshot(org="org-a", u5h=0.02, u7d=0.02),
            }
        )
        rows = TokenReport(
            reader=reader, secret_reader=secrets, ad_hoc_tokens=["sk-ant-oat01-B", "sk-ant-oat01-A"]
        ).rows()
        assert [row.account for row in rows] == ["oauth/spent", "token[1]", "token[2]"]
        assert [row.organization_id for row in rows] == ["org-spent", "org-b", "org-a"]


class ConcurrentProbeTest(TestCase):
    """Probes fan out across accounts; the emitted rows stay deterministic."""

    def test_every_account_is_probed_once_and_rows_are_complete(self) -> None:
        paths = [f"oauth/acct-{index}" for index in range(5)]
        _configure(TokenKind.OAUTH, paths)
        secrets = RecordingSecretReader({path: f"T-{path}" for path in paths})
        reader = FakeReader(
            {f"T-{path}": _snapshot(org=f"org-{index}", u5h=index / 100) for index, path in enumerate(paths)}
        )

        rows = TokenReport(reader=reader, secret_reader=secrets).rows()

        assert sorted(token for token, _ in reader.calls) == sorted(f"T-{path}" for path in paths)
        assert len(reader.calls) == len(paths), "each account is probed exactly once"
        assert {row.account for row in rows} == set(paths)
        assert AnthropicTokenUsage.objects.count() == len(paths)


class TokensCommandTest(TestCase):
    def _run(self, **kwargs: object) -> str:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/exhausted"])
        secrets = RecordingSecretReader({"anthropic/oauth/exhausted": "SECRET-CLI-TOKEN"})
        reader = FakeReader({"SECRET-CLI-TOKEN": _snapshot(org="org-cli", u5h=0.2, u7d=0.995)})
        buf = StringIO()
        with (
            patch("teatree.token_report.read_pass", secrets),
            patch("teatree.token_report.read_rate_limits", reader),
        ):
            call_command("tokens", stderr=buf, **kwargs)
        return buf.getvalue()

    def _run_json(self, **kwargs: object) -> str:
        """Stdout under ``--json`` — the machine channel the seam guarantees."""
        _configure(TokenKind.OAUTH, ["anthropic/oauth/exhausted"])
        secrets = RecordingSecretReader({"anthropic/oauth/exhausted": "SECRET-CLI-TOKEN"})
        reader = FakeReader({"SECRET-CLI-TOKEN": _snapshot(org="org-cli", u5h=0.2, u7d=0.995)})
        buf = StringIO()
        with (
            patch("teatree.token_report.read_pass", secrets),
            patch("teatree.token_report.read_rate_limits", reader),
        ):
            call_command("tokens", json_output=True, stdout=buf, **kwargs)
        return buf.getvalue()

    def test_table_renders_and_hides_tokens(self) -> None:
        out = self._run()
        assert "anthropic/oauth/exhausted" in out
        assert "EXHAUSTED" in out
        assert "SECRET-CLI-TOKEN" not in out

    def test_json_output_is_token_free(self) -> None:
        out = self._run_json()
        payload = json.loads(out)
        assert payload[0]["account"] == "anthropic/oauth/exhausted"
        assert payload[0]["source"] == "pass"
        assert payload[0]["status"] == "exhausted"
        assert "pass_path" not in payload[0]
        assert "SECRET-CLI-TOKEN" not in out

    def test_the_default_command_probes_and_reports_live_values_over_a_stale_row(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/acct"])
        _stored("anthropic/oauth/acct", org="org-STALE", u7d=0.995, status_7d="rejected")
        secrets = RecordingSecretReader({"anthropic/oauth/acct": "SECRET-CLI-TOKEN"})
        reader = FakeReader({"SECRET-CLI-TOKEN": _snapshot(org="org-LIVE", u5h=0.02, u7d=0.03)})
        buf = StringIO()
        with (
            patch("teatree.token_report.read_pass", secrets),
            patch("teatree.token_report.read_rate_limits", reader),
        ):
            call_command("tokens", json_output=True, stdout=buf)
        payload = json.loads(buf.getvalue())
        assert payload[0]["organization_id"] == "org-LIVE"
        assert payload[0]["status"] == "healthy"
        assert reader.calls == [("SECRET-CLI-TOKEN", True)]

    def test_the_cached_flag_reports_the_stored_verdict_without_probing(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/acct"])
        _stored("anthropic/oauth/acct", org="org-STORED", u7d=0.995, status_7d="rejected")
        secrets = RecordingSecretReader({"anthropic/oauth/acct": "SECRET-CLI-TOKEN"})
        reader = FakeReader({"SECRET-CLI-TOKEN": _snapshot(org="org-LIVE")})
        buf = StringIO()
        with (
            patch("teatree.token_report.read_pass", secrets),
            patch("teatree.token_report.read_rate_limits", reader),
        ):
            call_command("tokens", json_output=True, cached=True, stdout=buf)
        payload = json.loads(buf.getvalue())
        assert payload[0]["organization_id"] == "org-STORED"
        assert payload[0]["status"] == "exhausted"
        assert reader.calls == []

    def test_api_key_json_reports_credit_state_and_hides_key(self) -> None:
        _configure(TokenKind.API_KEY, ["anthropic/apikey/funded"])
        secrets = RecordingSecretReader({"anthropic/apikey/funded": "SECRET-CLI-API-KEY"})
        api_key_reader = FakeApiKeyReader({"SECRET-CLI-API-KEY": _metered(org="org-cli-metered")})
        buf = StringIO()
        with (
            patch("teatree.token_report.read_pass", secrets),
            patch("teatree.token_report.read_api_key_status", api_key_reader),
        ):
            call_command("tokens", json_output=True, stdout=buf)
        payload = json.loads(buf.getvalue())
        assert payload[0]["kind"] == "api_key"
        assert payload[0]["status"] == "healthy"
        assert payload[0]["requests_remaining"] == 4999
        assert payload[0]["tokens_remaining"] == 990000
        assert payload[0]["utilization_5h"] is None
        assert "SECRET-CLI-API-KEY" not in buf.getvalue()


_OAUTH_TOKEN = "sk-ant-oat01-ADHOC-SUPER-SECRET"
_API_KEY_TOKEN = "sk-ant-api03-ADHOC-SUPER-SECRET"


class AdHocTokenRowsTest(TestCase):
    """``--token`` ad-hoc rows: probed fresh, labelled ``token[N]``, never cache-backed.

    An ad-hoc token is health-probed BEFORE it is written into ``pass`` (the re-mint
    recovery flow). The token is passed directly (never resolved from the secret store),
    so the secret reader is asserted untouched throughout.
    """

    def _report(
        self,
        tokens: list[str] | None,
        *,
        snapshots: dict[str, RateLimitSnapshot] | None = None,
        oauth_unreachable: set[str] | None = None,
        metered: dict[str, MeteredKeySnapshot] | None = None,
        metered_unreachable: set[str] | None = None,
    ) -> tuple[TokenReport, RecordingSecretReader, FakeReader, FakeApiKeyReader]:
        secrets = RecordingSecretReader({})
        reader = FakeReader(snapshots or {}, unreachable=oauth_unreachable)
        api_key_reader = FakeApiKeyReader(metered or {}, unreachable=metered_unreachable)
        report = TokenReport(reader=reader, secret_reader=secrets, api_key_reader=api_key_reader, ad_hoc_tokens=tokens)
        return report, secrets, reader, api_key_reader

    def test_single_oauth_token_probes_fresh_and_is_healthy(self) -> None:
        report, secrets, reader, _ = self._report(
            [_OAUTH_TOKEN], snapshots={_OAUTH_TOKEN: _snapshot(org="org-adhoc", u5h=0.1, u7d=0.1)}
        )
        rows = report.rows()
        assert len(rows) == 1
        assert rows[0].account == "token[1]"
        assert rows[0].source is TokenSource.AD_HOC
        assert rows[0].status is TokenStatus.HEALTHY
        assert rows[0].organization_id == "org-adhoc"
        assert rows[0].overlays_label == "—"
        assert reader.calls == [(_OAUTH_TOKEN, True)]
        assert secrets.calls == []

    def test_oauth_token_probe_failure_is_unreachable(self) -> None:
        report, _, _, _ = self._report([_OAUTH_TOKEN], oauth_unreachable={_OAUTH_TOKEN})
        assert report.rows()[0].status is TokenStatus.UNREACHABLE

    def test_three_tokens_render_three_rows_in_option_order(self) -> None:
        t1, t2, t3 = "sk-ant-oat01-A", "sk-ant-oat01-B", "sk-ant-api03-C"
        report, _, _, _ = self._report(
            [t1, t2, t3],
            snapshots={t1: _snapshot(org="org-a"), t2: _snapshot(org="org-b")},
            metered={t3: _metered(org="org-c")},
        )
        rows = report.rows()
        assert [row.account for row in rows] == ["token[1]", "token[2]", "token[3]"]
        assert [row.organization_id for row in rows] == ["org-a", "org-b", "org-c"]

    def test_duplicate_tokens_are_deduped_preserving_first_seen_order(self) -> None:
        t1, t2 = "sk-ant-oat01-A", "sk-ant-oat01-B"
        report, _, _, _ = self._report([t1, t2, t1], snapshots={t1: _snapshot(org="org-a"), t2: _snapshot(org="org-b")})
        rows = report.rows()
        assert [row.account for row in rows] == ["token[1]", "token[2]"]
        assert [row.organization_id for row in rows] == ["org-a", "org-b"]

    def test_api_key_token_is_metered_and_shows_the_caption(self) -> None:
        report, _, _, _ = self._report([_API_KEY_TOKEN], metered={_API_KEY_TOKEN: _metered(org="org-metered")})
        rows = report.rows()
        assert rows[0].status is TokenStatus.HEALTHY
        assert rows[0].requests_remaining == 4999
        assert rows[0].tokens_remaining == 990000
        out = render_table(rows)
        assert "req 4999/5000" in out
        assert "tok 990000" in out
        assert "prepaid" in out  # the api_key caption (its only occurrence of the word)

    def test_api_key_token_probe_failure_is_unreachable(self) -> None:
        report, _, reader, api_key_reader = self._report([_API_KEY_TOKEN], metered_unreachable={_API_KEY_TOKEN})
        row = report.rows()[0]
        assert row.account == "token[1]"
        assert row.source is TokenSource.AD_HOC
        assert row.kind is TokenKind.API_KEY
        assert row.status is TokenStatus.UNREACHABLE
        assert reader.calls == []
        assert api_key_reader.calls == [_API_KEY_TOKEN]

    def test_unrecognised_prefix_is_unreachable_and_never_transmitted(self) -> None:
        token = "not-an-anthropic-token-SUPER-SECRET"
        report, _, reader, api_key_reader = self._report([token])
        rows = report.rows()
        assert rows[0].status is TokenStatus.UNREACHABLE
        assert rows[0].account == "token[1]"
        assert reader.calls == []
        assert api_key_reader.calls == []
        assert token not in render_table(rows)
        assert token not in json.dumps([row.as_dict() for row in rows])

    def test_empty_string_token_is_missing(self) -> None:
        report, _, reader, api_key_reader = self._report([""])
        row = report.rows()[0]
        assert row.account == "token[1]"
        assert row.status is TokenStatus.MISSING
        assert row.source is TokenSource.AD_HOC
        assert reader.calls == []
        assert api_key_reader.calls == []

    def test_token_value_never_appears_in_table_or_json(self) -> None:
        report, _, _, _ = self._report(
            [_OAUTH_TOKEN, _API_KEY_TOKEN],
            snapshots={_OAUTH_TOKEN: _snapshot(org="org-o")},
            metered={_API_KEY_TOKEN: _metered(org="org-k")},
        )
        rows = report.rows()
        out = render_table(rows)
        blob = json.dumps([row.as_dict() for row in rows])
        for secret in (_OAUTH_TOKEN, _API_KEY_TOKEN):
            assert secret not in out
            assert secret not in blob

    def test_ad_hoc_rows_never_read_or_write_the_usage_cache(self) -> None:
        assert AnthropicTokenUsage.objects.count() == 0
        report, _, reader, _ = self._report([_OAUTH_TOKEN], snapshots={_OAUTH_TOKEN: _snapshot(org="org-o")})
        report.rows()
        assert AnthropicTokenUsage.objects.count() == 0
        assert reader.calls == [(_OAUTH_TOKEN, True)]

    def test_json_shape_uses_account_and_source_not_pass_path(self) -> None:
        report, _, _, _ = self._report([_OAUTH_TOKEN], snapshots={_OAUTH_TOKEN: _snapshot(org="org-o")})
        payload = report.rows()[0].as_dict()
        assert payload["account"] == "token[1]"
        assert payload["source"] == "token"
        assert "pass_path" not in payload

    def test_ad_hoc_rows_render_alongside_the_pass_rows(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/configured"])
        secrets = RecordingSecretReader({"anthropic/oauth/configured": "PASS-TOKEN"})
        reader = FakeReader({"PASS-TOKEN": _snapshot(org="org-pass"), _OAUTH_TOKEN: _snapshot(org="org-adhoc")})
        report = TokenReport(reader=reader, secret_reader=secrets, ad_hoc_tokens=[_OAUTH_TOKEN])
        rows = report.rows()
        assert [row.account for row in rows] == ["anthropic/oauth/configured", "token[1]"]
        assert [row.source for row in rows] == [TokenSource.STORE, TokenSource.AD_HOC]

    def test_no_ad_hoc_option_leaves_only_the_pass_rows(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/configured"])
        for ad_hoc in (None, []):
            reader = FakeReader({"PASS-TOKEN": _snapshot(org="org-pass")})
            rows = TokenReport(
                reader=reader,
                secret_reader=RecordingSecretReader({"anthropic/oauth/configured": "PASS-TOKEN"}),
                ad_hoc_tokens=ad_hoc,
            ).rows()
            assert [row.account for row in rows] == ["anthropic/oauth/configured"]
            assert rows[0].source is TokenSource.STORE


class TokensCommandAdHocTest(TestCase):
    """``call_command('tokens', tokens=[...])`` threads the repeatable option end to end."""

    def _run(self, tokens: list[str], **kwargs: object) -> str:
        secrets = RecordingSecretReader({})
        reader = FakeReader({_OAUTH_TOKEN: _snapshot(org="org-cli-adhoc", u5h=0.1, u7d=0.1)})
        api_key_reader = FakeApiKeyReader({_API_KEY_TOKEN: _metered(org="org-cli-metered")})
        buf = StringIO()
        with (
            patch("teatree.token_report.read_pass", secrets),
            patch("teatree.token_report.read_rate_limits", reader),
            patch("teatree.token_report.read_api_key_status", api_key_reader),
        ):
            call_command("tokens", stderr=buf, tokens=tokens, **kwargs)
        return buf.getvalue()

    def _run_json(self, tokens: list[str], **kwargs: object) -> str:
        """Stdout under ``--json`` — the machine channel the seam guarantees."""
        secrets = RecordingSecretReader({})
        reader = FakeReader({_OAUTH_TOKEN: _snapshot(org="org-cli-adhoc", u5h=0.1, u7d=0.1)})
        api_key_reader = FakeApiKeyReader({_API_KEY_TOKEN: _metered(org="org-cli-metered")})
        buf = StringIO()
        with (
            patch("teatree.token_report.read_pass", secrets),
            patch("teatree.token_report.read_rate_limits", reader),
            patch("teatree.token_report.read_api_key_status", api_key_reader),
        ):
            call_command("tokens", json_output=True, stdout=buf, tokens=tokens, **kwargs)
        return buf.getvalue()

    def test_table_renders_ad_hoc_row_and_hides_the_token(self) -> None:
        out = self._run([_OAUTH_TOKEN])
        assert "token[1]" in out
        assert "HEALTHY" in out
        assert _OAUTH_TOKEN not in out

    def test_json_reports_ad_hoc_row_with_source_and_hides_the_token(self) -> None:
        payload = json.loads(self._run_json([_API_KEY_TOKEN]))
        assert payload[0]["account"] == "token[1]"
        assert payload[0]["source"] == "token"
        assert payload[0]["kind"] == "api_key"
        assert payload[0]["requests_remaining"] == 4999
        assert _API_KEY_TOKEN not in json.dumps(payload)

    def test_explicit_none_tokens_behaves_like_the_option_absent(self) -> None:
        # The CLI always threads ``tokens=<value>``; the zero-``--token`` case passes
        # ``None`` — django-typer's ``call_command`` must accept it and add no ad-hoc row.
        _configure(TokenKind.OAUTH, ["anthropic/oauth/only-pass"])
        secrets = RecordingSecretReader({"anthropic/oauth/only-pass": "PASS-TOK"})
        reader = FakeReader({"PASS-TOK": _snapshot(org="org-pass")})
        buf = StringIO()
        with (
            patch("teatree.token_report.read_pass", secrets),
            patch("teatree.token_report.read_rate_limits", reader),
        ):
            call_command("tokens", stderr=buf, tokens=None)
        out = buf.getvalue()
        assert "anthropic/oauth/only-pass" in out
        assert "token[1]" not in out


class UnmeasuredCachedWindowRendersUnknownTest(TestCase):
    """A window the router never measured must render ``—`` under ``--cached``, never ``0%``.

    The reactive writer records an exhaustion verdict for the window that refused, and knows
    nothing about the other one. Storing that unknown as ``0.0`` made the cache assert full
    headroom on a window nobody read — the reading a human then acts on.
    """

    _ACCOUNT = "anthropic/unprobeable/oauth"

    def setUp(self) -> None:
        _configure(TokenKind.OAUTH, [self._ACCOUNT])
        AnthropicTokenUsage.objects.record(
            self._ACCOUNT,
            TokenHealthReading(
                organization_id="",
                utilization_5h=None,
                utilization_7d=1.0,
                status_5h="",
                status_7d="rejected",
                reset_5h=None,
                reset_7d=timezone.now() + dt.timedelta(days=5),
                verified=False,
            ),
        )
        self.report = TokenReport(reader=FakeReader({}), secret_reader=RecordingSecretReader({}), from_cache=True)

    def test_the_unmeasured_window_is_stored_as_unknown(self) -> None:
        row = AnthropicTokenUsage.objects.get(pass_path=self._ACCOUNT)
        assert row.utilization_5h is None, "an unread window must not be persisted as measured headroom"
        assert row.utilization_7d == pytest.approx(1.0), "the window that refused is still recorded"

    def test_the_cached_row_renders_the_unmeasured_window_as_unknown(self) -> None:
        row = self.report.rows()[0]
        assert row.utilization_5h is None
        assert row.col_5h == "—"
        assert row.col_7d == "100%"

    def test_the_cached_json_reports_the_unmeasured_window_as_null(self) -> None:
        payload = self.report.rows()[0].as_dict()
        assert payload["utilization_5h"] is None
        assert payload["utilization_7d"] == pytest.approx(1.0)


class ApiOwnWarningThresholdsTest(TestCase):
    """The API reports where its own warning band starts; teatree's constants are a guess.

    A shipped 0.80 band flags an account the API still considers healthy, and misses one it
    already warns about — on an account whose real band sits either side of the guess.
    """

    _ACCOUNT = "anthropic/oauth/banded"

    def _row(self, *, u5h: float, warn_5h: float | None) -> TokenAccountRow:
        _configure(TokenKind.OAUTH, [self._ACCOUNT])
        snapshot = replace(_snapshot(org="org-1", u5h=u5h, u7d=0.1), warn_above_5h=warn_5h)
        secrets = RecordingSecretReader({self._ACCOUNT: "TOK"})
        return TokenReport(reader=FakeReader({"TOK": snapshot}), secret_reader=secrets).rows()[0]

    def test_a_looser_api_band_leaves_a_row_healthy(self) -> None:
        row = self._row(u5h=0.85, warn_5h=0.95)
        assert row.status is TokenStatus.HEALTHY, "teatree's 0.80 guess must not overrule the API's own band"

    def test_a_tighter_api_band_warns_earlier(self) -> None:
        row = self._row(u5h=0.55, warn_5h=0.50)
        assert row.status is TokenStatus.WARNING

    def test_an_absent_api_band_falls_back_to_the_shipped_one(self) -> None:
        assert self._row(u5h=0.85, warn_5h=None).status is TokenStatus.WARNING


class FallbackModelIsReportedTest(TestCase):
    """Whether a fallback model is still available rides the same probe response."""

    _ACCOUNT = "anthropic/oauth/fallback"

    def _payload(self, fallback: str) -> TokenAccountPayload:
        _configure(TokenKind.OAUTH, [self._ACCOUNT])
        snapshot = replace(_snapshot(org="org-1"), fallback=fallback)
        secrets = RecordingSecretReader({self._ACCOUNT: "TOK"})
        return TokenReport(reader=FakeReader({"TOK": snapshot}), secret_reader=secrets).rows()[0].as_dict()

    def test_the_reported_fallback_reaches_the_json(self) -> None:
        assert self._payload("available")["fallback"] == "available"

    def test_an_unreported_fallback_is_null_not_empty(self) -> None:
        assert self._payload("")["fallback"] is None


_ROTATION_ACCOUNT = "anthropic/acct/oauth-token"


class CredentialRotationTest(TestCase):
    """A cached verdict belongs to the credential it was probed with (#4736).

    The report always probes live, so a rotated credential is re-read on every run; the
    stored row must then name the credential that produced it, never the previous one.
    """

    def _exhausted_row(self, *, probed_with: str | None) -> None:
        now = timezone.now()
        AnthropicTokenUsage.objects.record(
            _ROTATION_ACCOUNT,
            TokenHealthReading(
                organization_id="org-old",
                utilization_5h=1.0,
                utilization_7d=0.0,
                status_5h="rejected",
                status_7d="allowed",
                reset_5h=now + dt.timedelta(hours=3),
                reset_7d=now + dt.timedelta(days=3),
            ),
            now=now,
            token_fingerprint=fingerprint_token(probed_with) if probed_with is not None else None,
        )

    def test_rotated_credential_re_probes_instead_of_serving_the_stale_verdict(self) -> None:
        _configure(TokenKind.OAUTH, [_ROTATION_ACCOUNT])
        self._exhausted_row(probed_with="TOK-old")
        secrets = RecordingSecretReader({_ROTATION_ACCOUNT: "TOK-new"})
        reader = FakeReader({"TOK-new": _snapshot(org="org-new", u5h=0.09, u7d=0.28)})

        rows = TokenReport(reader=reader, secret_reader=secrets).rows()

        assert rows[0].status is TokenStatus.HEALTHY
        assert rows[0].organization_id == "org-new"
        assert reader.calls == [("TOK-new", True)]

    def test_the_re_probe_rebinds_the_row_to_the_new_credential(self) -> None:
        _configure(TokenKind.OAUTH, [_ROTATION_ACCOUNT])
        self._exhausted_row(probed_with="TOK-old")
        secrets = RecordingSecretReader({_ROTATION_ACCOUNT: "TOK-new"})
        reader = FakeReader({"TOK-new": _snapshot(org="org-new", u5h=0.09)})

        TokenReport(reader=reader, secret_reader=secrets).rows()

        cached = AnthropicTokenUsage.objects.get(pass_path=_ROTATION_ACCOUNT)
        assert cached.token_fingerprint == fingerprint_token("TOK-new")
        assert cached.organization_id == "org-new"

    def test_a_row_with_no_recorded_credential_is_re_probed(self) -> None:
        _configure(TokenKind.OAUTH, [_ROTATION_ACCOUNT])
        self._exhausted_row(probed_with=None)
        secrets = RecordingSecretReader({_ROTATION_ACCOUNT: "TOK-new"})
        reader = FakeReader({"TOK-new": _snapshot(org="org-new", u5h=0.09)})

        rows = TokenReport(reader=reader, secret_reader=secrets).rows()

        assert rows[0].status is TokenStatus.HEALTHY
        assert reader.calls == [("TOK-new", True)]

    def test_a_deleted_pass_entry_reports_missing_not_the_cached_verdict(self) -> None:
        _configure(TokenKind.OAUTH, [_ROTATION_ACCOUNT])
        self._exhausted_row(probed_with="TOK-old")
        secrets = RecordingSecretReader({})
        reader = FakeReader({})

        rows = TokenReport(reader=reader, secret_reader=secrets).rows()

        assert rows[0].status is TokenStatus.MISSING
        assert reader.calls == []

    def test_an_unreachable_re_probe_keeps_the_stored_reading(self) -> None:
        _configure(TokenKind.OAUTH, [_ROTATION_ACCOUNT])
        self._exhausted_row(probed_with="TOK-old")
        secrets = RecordingSecretReader({_ROTATION_ACCOUNT: "TOK-new"})
        reader = FakeReader({}, unreachable={"TOK-new"})

        rows = TokenReport(reader=reader, secret_reader=secrets).rows()

        assert rows[0].status is TokenStatus.UNREACHABLE
        cached = AnthropicTokenUsage.objects.get(pass_path=_ROTATION_ACCOUNT)
        assert cached.organization_id == "org-old"
        assert cached.token_fingerprint == fingerprint_token("TOK-old")

    def test_the_fingerprint_is_never_emitted_in_the_payload(self) -> None:
        _configure(TokenKind.OAUTH, [_ROTATION_ACCOUNT])
        secrets = RecordingSecretReader({_ROTATION_ACCOUNT: "TOK-new"})
        reader = FakeReader({"TOK-new": _snapshot(org="org-new")})

        payload = json.dumps([row.as_dict() for row in TokenReport(reader=reader, secret_reader=secrets).rows()])

        assert fingerprint_token("TOK-new") not in payload
        assert "TOK-new" not in payload
