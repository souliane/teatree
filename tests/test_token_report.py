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

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage, TokenHealthReading
from teatree.core.models.config_setting import ConfigSetting
from teatree.credential_config import LIST_SETTING, TokenKind
from teatree.llm.rate_limits import RateLimitProbeError, RateLimitSnapshot
from teatree.token_report import TokenReport, TokenStatus, render_table


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
            unreachable={"TOK-unreachable"},
        )

        rows = {row.pass_path: row for row in TokenReport(reader=reader, secret_reader=secrets).rows()}

        assert rows["anthropic/oauth/healthy"].status is TokenStatus.HEALTHY
        assert rows["anthropic/oauth/warning"].status is TokenStatus.WARNING
        assert rows["anthropic/oauth/exhausted"].status is TokenStatus.EXHAUSTED
        assert rows["anthropic/apikey/missing"].status is TokenStatus.MISSING
        assert rows["anthropic/apikey/unreachable"].status is TokenStatus.UNREACHABLE
        assert rows["anthropic/oauth/healthy"].overlays_label == "global"
        assert rows["anthropic/oauth/exhausted"].overlays_label == "teatree"
        assert rows["anthropic/oauth/healthy"].organization_id == "org-healthy"
        # OAuth accounts are probed with the oauth beta header, API-key ones without.
        assert ("TOK-healthy", True) in reader.calls
        assert ("TOK-unreachable", False) in reader.calls

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
