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
from teatree.token_report import (
    TokenAccountPayload,
    TokenAccountRow,
    TokenReport,
    TokenSource,
    TokenStatus,
    render_table,
)


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
        assert payload[0]["account"] == "anthropic/oauth/exhausted"
        assert payload[0]["source"] == "pass"
        assert payload[0]["status"] == "exhausted"
        assert "pass_path" not in payload[0]
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
            call_command("tokens", stdout=buf, tokens=tokens, **kwargs)
        return buf.getvalue()

    def test_table_renders_ad_hoc_row_and_hides_the_token(self) -> None:
        out = self._run([_OAUTH_TOKEN])
        assert "token[1]" in out
        assert "HEALTHY" in out
        assert _OAUTH_TOKEN not in out

    def test_json_reports_ad_hoc_row_with_source_and_hides_the_token(self) -> None:
        payload = json.loads(self._run([_API_KEY_TOKEN], json_output=True))
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
            call_command("tokens", stdout=buf, tokens=None)
        out = buf.getvalue()
        assert "anthropic/oauth/only-pass" in out
        assert "token[1]" not in out
