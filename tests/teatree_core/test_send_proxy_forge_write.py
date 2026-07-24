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
from django.test import TestCase

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
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError

_BANNED_SOURCE_READ_FAILURE = "banned-terms DB read wedged"


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


class TestRouteForgeWriteCleanPath(TestCase):
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


class TestRouteForgeWriteLeakGate(TestCase):
    def test_customer_codename_to_a_public_repo_raises_before_any_audit(self) -> None:
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["Contoso"], [])),
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


class TestRouteForgeWriteBannedTermsLeakGate(TestCase):
    """The DB ``banned_terms`` list is scanned on the PUBLIC forge-write seam (security fix).

    Reproduces the production config that leaked: the overlay's
    ``privacy_redact_terms`` is EMPTY, but the customer codename lives in the
    DB-home ``banned_terms`` list. The forge-write seam must union that list into
    the public-target scan — the synthetic term ``acme`` stands in for the real
    codename that leaked.
    """

    def test_db_banned_term_blocks_public_forge_write_with_empty_redact_terms(self) -> None:
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=([], [])),
            patch("teatree.hooks.banned_terms_cli.resolve_banned_terms", return_value=("acme",)),
            pytest.raises(OutboundLeakError, match="privacy gate refused"),
        ):
            route_forge_write(
                forge="github",
                repo="souliane/teatree",
                text="rolling out the acme-fork migration",
                action="github_issue_create",
                target="souliane/teatree",
            )
        assert SendAudit.objects.count() == 0

    def test_clean_body_passes_when_banned_terms_set(self) -> None:
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=([], [])),
            patch("teatree.hooks.banned_terms_cli.resolve_banned_terms", return_value=("acme",)),
        ):
            out = route_forge_write(
                forge="github",
                repo="souliane/teatree",
                text="rolling out a routine migration",
                action="github_issue_create",
                target="souliane/teatree",
            )
        assert out == "rolling out a routine migration"

    def test_private_target_not_blocked_by_banned_term(self) -> None:
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False),
            patch("teatree.hooks.banned_terms_cli.resolve_banned_terms", return_value=("acme",)),
        ):
            out = route_forge_write(
                forge="github",
                repo="acme/private",
                text="rolling out the acme-fork migration",
                action="github_issue_create",
                target="acme/private",
            )
        assert out == "rolling out the acme-fork migration"

    def test_unreadable_banned_source_fails_closed_on_public_target(self) -> None:
        def _boom() -> tuple[str, ...]:
            raise RuntimeError(_BANNED_SOURCE_READ_FAILURE)

        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=([], [])),
            patch("teatree.hooks.banned_terms_cli.resolve_banned_terms", side_effect=_boom),
            pytest.raises(OutboundLeakError, match="banned-terms-unresolvable"),
        ):
            route_forge_write(
                forge="github",
                repo="souliane/teatree",
                text="a perfectly ordinary note",
                action="github_issue_create",
                target="souliane/teatree",
            )

    def test_unset_not_required_is_a_dev_noop(self) -> None:
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=([], [])),
            patch(
                "teatree.hooks.banned_terms_cli.resolve_banned_terms",
                side_effect=BannedTermsUnsetError.for_key("banned_terms"),
            ),
            patch("teatree.hooks.banned_terms_cli.banned_terms_required", return_value=False),
        ):
            out = route_forge_write(
                forge="github",
                repo="souliane/teatree",
                text="a perfectly ordinary note",
                action="github_issue_create",
                target="souliane/teatree",
            )
        assert out == "a perfectly ordinary note"

    def test_unset_required_fails_closed_on_public_target(self) -> None:
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=([], [])),
            patch(
                "teatree.hooks.banned_terms_cli.resolve_banned_terms",
                side_effect=BannedTermsUnsetError.for_key("banned_terms"),
            ),
            patch("teatree.hooks.banned_terms_cli.banned_terms_required", return_value=True),
            pytest.raises(OutboundLeakError, match="banned-terms-unresolvable"),
        ):
            route_forge_write(
                forge="github",
                repo="souliane/teatree",
                text="a perfectly ordinary note",
                action="github_issue_create",
                target="souliane/teatree",
            )


class TestRouteForgeWriteSendProxy(TestCase):
    def test_enforce_mode_non_allowlisted_destination_raises_and_audits_denied(self) -> None:
        ConfigSetting.objects.set_value("send_proxy_mode", SendProxyMode.ENFORCE.value)
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False),
            pytest.raises(SendBlockedError, match="allowlist"),
        ):
            route_forge_write(forge="github", repo="acme/blocked", text="body", action="a", target="t")
        assert SendAudit.objects.get().allowlist_verdict == SendAudit.Verdict.DENIED.value

    def test_enforce_mode_redacts_an_allowlisted_send(self) -> None:
        ConfigSetting.objects.set_value("send_proxy_mode", SendProxyMode.ENFORCE.value)
        ConfigSetting.objects.set_value("send_proxy_allowlist", ["acme/widgets"])
        with (
            patch("teatree.core.send_proxy._redact_terms", lambda _overlay: ["SECRETCORP"]),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False),
        ):
            out = route_forge_write(
                forge="github", repo="acme/widgets", text="leak SECRETCORP here", action="a", target="t"
            )
        assert REDACTION_PLACEHOLDER in out
        assert "SECRETCORP" not in out
        assert SendAudit.objects.get().redaction_applied is True
