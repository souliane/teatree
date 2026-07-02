"""Tests for the ``t3 tokens`` per-account Anthropic health reporter.

The reporter (``teatree.token_report``) reads the SAME per-overlay OAuth / API-key
``pass``-path lists the routing selector uses, resolves each account's token, and
probes / reuses cached health. These tests drive canned health + tokens through the
injected reader / secret reader (no network, no ``pass``) and assert the classified
rows, the cache reuse, and — the load-bearing invariant — that a token value is
NEVER emitted in the rendered table or the JSON.
"""

import datetime as dt
import json
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage, TokenHealthReading
from teatree.core.models.config_setting import ConfigSetting
from teatree.credential_config import LIST_SETTING, TokenKind
from teatree.llm.rate_limits import MeteredKeySnapshot, RateLimitProbeError, RateLimitSnapshot
from teatree.token_report import TokenAccountPayload, TokenAccountRow, TokenReport, TokenStatus, render_table


def _snapshot(*, org: str, u5h: float = 0.1, u7d: float = 0.1, status_7d: str = "allowed") -> RateLimitSnapshot:
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
            "pass_path": "anthropic/x/oauth",
            "kind": TokenKind.OAUTH,
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

    def test_oauth_row_payload_carries_utilization_not_per_minute_fields(self) -> None:
        payload = self._row().as_dict()
        assert payload["kind"] == "oauth"
        assert payload["status"] == "healthy"
        assert payload["utilization_5h"] == pytest.approx(0.1)
        assert payload["requests_remaining"] is None

    def test_weekly_reset_local_is_a_dash_when_absent(self) -> None:
        assert self._row(weekly_reset=None).weekly_reset_local == "—"


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
        rows = {row.pass_path: row for row in report.rows()}

        assert rows["anthropic/oauth/healthy"].status is TokenStatus.HEALTHY
        assert rows["anthropic/oauth/warning"].status is TokenStatus.WARNING
        assert rows["anthropic/oauth/exhausted"].status is TokenStatus.EXHAUSTED
        assert rows["anthropic/apikey/missing"].status is TokenStatus.MISSING
        assert rows["anthropic/apikey/unreachable"].status is TokenStatus.UNREACHABLE
        assert rows["anthropic/oauth/healthy"].overlays_label == "global"
        assert rows["anthropic/oauth/exhausted"].overlays_label == "teatree"
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

    def test_fresh_cache_row_is_reused_without_probe(self) -> None:
        _configure(TokenKind.OAUTH, ["anthropic/oauth/cached"])
        AnthropicTokenUsage.objects.record(
            "anthropic/oauth/cached",
            TokenHealthReading(
                organization_id="org-cached",
                utilization_5h=0.1,
                utilization_7d=0.1,
                status_5h="allowed",
                status_7d="allowed",
                reset_5h=None,
                reset_7d=None,
            ),
            now=timezone.now(),
        )
        secrets = RecordingSecretReader({"anthropic/oauth/cached": "TOK-cached"})
        reader = FakeReader({})

        rows = TokenReport(reader=reader, secret_reader=secrets).rows()

        assert rows[0].status is TokenStatus.HEALTHY
        assert rows[0].organization_id == "org-cached"
        assert reader.calls == []
        assert secrets.calls == []

    def test_no_configured_accounts_yields_no_rows(self) -> None:
        assert TokenReport(reader=FakeReader({}), secret_reader=RecordingSecretReader({})).rows() == []


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
            call_command("tokens", stdout=buf, **kwargs)
        return buf.getvalue()

    def test_table_renders_and_hides_tokens(self) -> None:
        out = self._run()
        assert "anthropic/oauth/exhausted" in out
        assert "EXHAUSTED" in out
        assert "SECRET-CLI-TOKEN" not in out

    def test_json_output_is_token_free(self) -> None:
        out = self._run(json_output=True)
        payload = json.loads(out)
        assert payload[0]["pass_path"] == "anthropic/oauth/exhausted"
        assert payload[0]["status"] == "exhausted"
        assert "SECRET-CLI-TOKEN" not in out

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
