"""The shared forge-write seam every issue/comment writer routes through (U14).

``route_forge_write`` is the core seam hoisted out of the MCP layer's
``_scrub_forge_body`` so the public-repo leak gate + the #117 send-proxy fire
IDENTICALLY on every forge write — the MCP tools AND the internal loop/CLI
writers (dream umbrella / memory-gap filers, review-findings enforcement filer,
``t3`` ticket / test-plan CLIs) — closing the "MCP is stricter than core"
inversion where the dream loop filed distilled-memory issues on a public repo
through an unscrubbed path.
"""

from unittest.mock import patch

import pytest

from teatree.config.enums import SendProxyMode
from teatree.core.models import ConfigSetting, SendAudit
from teatree.core.send_proxy import (
    REDACTION_PLACEHOLDER,
    OutboundBlockedError,
    OutboundLeakError,
    SendBlockedError,
    SendChannel,
    channel_for_forge,
    forge_from_url,
    route_forge_write,
)

# These tests need pytest's monkeypatch + patch of privacy_gate internals, which
# django.test.TestCase cannot provide — so pytest.mark.django_db is the right tool.
# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestChannelForForge:
    def test_maps_known_forges(self) -> None:
        assert channel_for_forge("github") is SendChannel.GITHUB
        assert channel_for_forge("gitlab") is SendChannel.GITLAB

    def test_unknown_forge_falls_back_to_other(self) -> None:
        assert channel_for_forge("") is SendChannel.OTHER
        assert channel_for_forge("bitbucket") is SendChannel.OTHER


class TestForgeFromUrl:
    def test_detects_github_and_gitlab(self) -> None:
        assert forge_from_url("https://github.com/souliane/teatree/issues/1") == "github"
        assert forge_from_url("https://gitlab.com/acme/widgets/-/issues/1") == "gitlab"

    def test_unknown_host_is_blank(self) -> None:
        assert forge_from_url("souliane/teatree") == ""


class TestRouteForgeWriteCleanPath:
    def test_empty_body_is_a_noop_passthrough_with_no_audit(self) -> None:
        assert route_forge_write(forge="github", repo="souliane/teatree", text="", action="a", target="t") == ""
        assert SendAudit.objects.count() == 0

    def test_clean_body_to_a_private_target_passes_through_and_audits(self) -> None:
        with patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False):
            out = route_forge_write(
                forge="github", repo="souliane/teatree", text="a clean note", action="issue_create", target="t"
            )
        assert out == "a clean note"
        row = SendAudit.objects.get()
        assert row.channel == SendChannel.GITHUB.value
        assert row.destination == "souliane/teatree"
        assert row.action == "issue_create"


class TestRouteForgeWriteLeakGate:
    def test_customer_codename_to_a_public_repo_raises_before_any_audit(self) -> None:
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate._overlay_privacy_rules", return_value=(["Contoso"], [])),
            pytest.raises(OutboundLeakError, match="privacy gate refused"),
        ):
            route_forge_write(
                forge="github", repo="souliane/teatree", text="roll out for Contoso", action="a", target="t"
            )
        # The leak gate refuses BEFORE the send-proxy, so nothing is audited.
        assert SendAudit.objects.count() == 0

    def test_leak_error_is_an_outbound_blocked_error(self) -> None:
        assert issubclass(OutboundLeakError, OutboundBlockedError)
        assert issubclass(SendBlockedError, OutboundBlockedError)


class TestRouteForgeWriteSendProxy:
    def test_enforce_mode_non_allowlisted_destination_raises_and_audits_denied(self) -> None:
        ConfigSetting.objects.set_value("send_proxy_mode", SendProxyMode.ENFORCE.value)
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False),
            pytest.raises(SendBlockedError, match="allowlist"),
        ):
            route_forge_write(forge="github", repo="acme/blocked", text="body", action="a", target="t")
        assert SendAudit.objects.get().allowlist_verdict == SendAudit.Verdict.DENIED.value

    def test_enforce_mode_redacts_an_allowlisted_send(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ConfigSetting.objects.set_value("send_proxy_mode", SendProxyMode.ENFORCE.value)
        ConfigSetting.objects.set_value("send_proxy_allowlist", ["acme/widgets"])
        monkeypatch.setattr("teatree.core.send_proxy._redact_terms", lambda _overlay: ["SECRETCORP"])
        with patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False):
            out = route_forge_write(
                forge="github", repo="acme/widgets", text="leak SECRETCORP here", action="a", target="t"
            )
        assert REDACTION_PLACEHOLDER in out
        assert "SECRETCORP" not in out
        assert SendAudit.objects.get().redaction_applied is True
