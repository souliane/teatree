"""Behaviour tests for overlay-bound outbound-audit verifiers (#1275).

The outbound-audit scanner verifies each :class:`OutboundClaim` by
contacting the third-party system that originally received the post.
Multi-overlay setups configure different credentials per overlay
(`github_token_ref = "github/work-token"` on one overlay,
`github_token_ref = "github/personal"` on another); a verifier built
with a process-global resolver lands on the wrong identity for at least
one of them, producing a false 404 → false drift DM (failure mode #1)
or no token at all → silent claim-skipping with `kind=slack_dm —
skipping claim N` debug noise (failure mode #3).

These tests pin the new contract:

- Every record helper stamps ``overlay`` on ``extra`` at claim-time.
- The scanner uses ``claim.extra["overlay"]`` to build a verifier
    scoped to that overlay's credentials.
- A claim whose overlay can't resolve credentials surfaces as an
    ``outbound.audit_skipped`` ScanSignal — observable, never drift.
"""

import datetime as dt
import os
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import OutboundClaim
from teatree.loop.scanners.outbound_audit import OutboundAuditScanner, kind_settling_seconds


def _aged_claim(*, kind: OutboundClaim.Kind, key: str, **extra: object) -> OutboundClaim:
    age = max(kind_settling_seconds.values(), default=30) + 30
    return OutboundClaim.objects.create(
        kind=kind,
        idempotency_key=key,
        target_url="https://example.com/artifact",
        claim_ts=timezone.now() - dt.timedelta(seconds=age),
        extra=extra or {},
    )


class OverlayBoundVerifierDispatchTests(TestCase):
    """Scanner routes each claim through its recorded-overlay's verifier."""

    def test_slack_dm_verifier_uses_recorded_overlay_messaging_backend(self) -> None:
        """A claim recorded under overlay 'work' verifies through that overlay's backend.

        ``messaging_from_overlay(overlay_name='work')`` resolves the
        backend; the default overlay's backend is never consulted.
        """
        _aged_claim(
            kind=OutboundClaim.Kind.SLACK_DM,
            key="overlay-work:slack:1",
            overlay="work",
            channel="C_WORK",
            ts="1700000000.0001",
        )

        work_backend = MagicMock()
        work_backend.get_permalink.return_value = "https://slack.example/archives/C_WORK/p1"
        default_backend = MagicMock()

        def _factory(overlay_name: str | None = None) -> object:
            if overlay_name == "work":
                return work_backend
            return default_backend

        with patch(
            "teatree.core.backend_factory.messaging_from_overlay",
            side_effect=_factory,
        ):
            scanner = OutboundAuditScanner()
            signals = scanner.scan()

        work_backend.get_permalink.assert_called_once_with(channel="C_WORK", ts="1700000000.0001")
        default_backend.get_permalink.assert_not_called()
        assert signals == []

    def test_github_note_verifier_uses_overlay_specific_token(self) -> None:
        """Two GitHub overlays send their own token to ``_gh_api_get`` per claim.

        Each overlay has its own ``github_token_ref``; the verifier
        resolves the right token for each claim independently.
        """
        _aged_claim(
            kind=OutboundClaim.Kind.GITHUB_NOTE,
            key="github_note:org/work#5:42",
            overlay="work",
            repo="org/work",
            artifact_id="42",
            token_ref="github/work-token",
        )
        _aged_claim(
            kind=OutboundClaim.Kind.GITHUB_NOTE,
            key="github_note:org/personal#7:99",
            overlay="personal",
            repo="org/personal",
            artifact_id="99",
            token_ref="github/personal",
        )

        seen_tokens: list[str] = []

        def _resolve(overlay_name: str) -> str:
            return f"tok-for-{overlay_name}"

        def _gh_get(_endpoint: str, *, token: str = "") -> object:
            seen_tokens.append(token)
            return {"id": 42, "body": ""}

        with (
            patch(
                "teatree.loop.scanners.outbound_audit._resolve_github_token_for_overlay",
                side_effect=_resolve,
            ),
            patch("teatree.backends.github.client._gh_api_get", side_effect=_gh_get),
        ):
            scanner = OutboundAuditScanner()
            scanner.scan()

        assert "tok-for-work" in seen_tokens
        assert "tok-for-personal" in seen_tokens

    def test_gitlab_note_verifier_uses_recorded_overlay_credentials(self) -> None:
        """A GitLab note claim built under overlay 'client-A' verifies through that overlay.

        The verifier calls ``_gitlab_api_for_overlay('client-A')`` to
        build the GitLabAPI instance — client-A's token and URL apply.
        """
        _aged_claim(
            kind=OutboundClaim.Kind.GITLAB_NOTE,
            key="gitlab_note:org/proj!1:7",
            overlay="client-A",
            repo="org/proj",
            mr=1,
            artifact_id="7",
            endpoint="notes",
        )

        seen_overlays: list[str] = []
        fake_api = MagicMock()
        fake_api.get_json.return_value = {"id": 7, "body": "x"}

        def _build_api(overlay_name: str) -> object:
            seen_overlays.append(overlay_name)
            return fake_api

        with patch(
            "teatree.loop.scanners.outbound_audit._gitlab_api_for_overlay",
            side_effect=_build_api,
        ):
            scanner = OutboundAuditScanner()
            scanner.scan()

        assert seen_overlays == ["client-A"]
        fake_api.get_json.assert_called_once()


class NoVerifierForKindObservabilityTests(TestCase):
    """Unresolvable overlay credentials emit ``outbound.audit_skipped``.

    Never silent skipping (the legacy log-debug behaviour) and never
    classified as drift — the credential gap is its own observable
    backlog signal.
    """

    def test_unresolvable_overlay_credentials_emit_audit_skipped(self) -> None:
        _aged_claim(
            kind=OutboundClaim.Kind.SLACK_DM,
            key="overlay-missing:slack:1",
            overlay="archived-overlay",
            channel="C123",
            ts="1.1",
        )

        notify = MagicMock()
        with patch(
            "teatree.core.backend_factory.messaging_from_overlay",
            return_value=None,
        ):
            scanner = OutboundAuditScanner(notifier=notify)
            signals = scanner.scan()

        # Drift was NOT emitted — that would have spammed a false alert.
        assert all(s.kind != "outbound.drift" for s in signals)
        # Observability: at least one signal of the new kind is emitted.
        kinds = {s.kind for s in signals}
        assert "outbound.audit_skipped" in kinds
        notify.assert_not_called()


class RecordClaimStampsOverlayTests(TestCase):
    """Every record helper writes the active overlay name into ``extra``."""

    def test_record_claim_stamps_active_overlay(self) -> None:
        from teatree.outbound_claim import record_claim  # noqa: PLC0415

        with patch.dict(os.environ, {"T3_OVERLAY_NAME": "work"}, clear=False):
            row = record_claim(
                kind=OutboundClaim.Kind.SLACK_DM,
                idempotency_key="rcs:1",
                extra={"channel": "C1", "ts": "1.1"},
            )

        assert row is not None
        assert row.extra["overlay"] == "work"
        # Pre-existing extra survives.
        assert row.extra["channel"] == "C1"
        assert row.extra["ts"] == "1.1"

    def test_record_claim_with_explicit_overlay_wins_over_env(self) -> None:
        """An explicit overlay in ``extra`` wins over ``T3_OVERLAY_NAME``."""
        from teatree.outbound_claim import record_claim  # noqa: PLC0415

        with patch.dict(os.environ, {"T3_OVERLAY_NAME": "env-overlay"}, clear=False):
            row = record_claim(
                kind=OutboundClaim.Kind.GITLAB_NOTE,
                idempotency_key="rcs:2",
                extra={
                    "repo": "org/proj",
                    "mr": 1,
                    "artifact_id": "1",
                    "overlay": "explicit-overlay",
                },
            )

        assert row is not None
        assert row.extra["overlay"] == "explicit-overlay"

    def test_record_note_claim_stamps_overlay(self) -> None:
        from teatree.cli.review.audit import record_note_claim  # noqa: PLC0415

        with patch.dict(os.environ, {"T3_OVERLAY_NAME": "gitlab-overlay"}, clear=False):
            record_note_claim(
                lambda: "https://gitlab.example/api/v4",
                "org/proj",
                1,
                42,
                endpoint="notes",
            )

        row = OutboundClaim.objects.get(idempotency_key="gitlab_note:org/proj!1:42")
        assert row.extra["overlay"] == "gitlab-overlay"

    def test_record_github_note_claim_stamps_overlay(self) -> None:
        from teatree.backends.github.claims import (  # noqa: PLC0415
            record_github_note_claim as _record_github_note_claim,
        )

        with patch.dict(os.environ, {"T3_OVERLAY_NAME": "gh-overlay"}, clear=False):
            _record_github_note_claim(
                repo="org/repo",
                target_number=5,
                comment_id=42,
                body="lgtm",
                target_url="https://github.com/org/repo/issues/5#issuecomment-42",
            )

        row = OutboundClaim.objects.get(idempotency_key="github_note:org/repo#5:42")
        assert row.extra["overlay"] == "gh-overlay"

    def test_notify_user_stamps_overlay_on_recorded_slack_dm_claim(self) -> None:
        """``notify_user`` records a SLACK_DM claim with the active overlay.

        Lets the audit verifier re-read with the same credentials that
        posted the DM, closing the multi-overlay drift gap.
        """
        from teatree.core.notify import _record_outbound_claim  # noqa: PLC0415

        with patch.dict(os.environ, {"T3_OVERLAY_NAME": "slack-overlay"}, clear=False):
            _record_outbound_claim(
                idempotency_key="slack_dm:notify-1",
                target_url="https://slack.example/p/1",
                channel="C123",
                posted_ts="1.1",
            )

        row = OutboundClaim.objects.get(idempotency_key="slack_dm:notify-1")
        assert row.extra["overlay"] == "slack-overlay"
        assert row.extra["channel"] == "C123"
        assert row.extra["ts"] == "1.1"


class OverlayCredentialResolutionTests(TestCase):
    """Per-overlay credential resolution drives the verifier factories.

    Covers the resolver helpers in
    ``outbound_audit_overlay_verifiers``: registered-overlay path
    (``get_overlay`` succeeds, ``config.get_gitlab_token`` /
    ``config.get_github_token`` returns the token), the TOML-only
    fallback path (overlay lives in ``[overlays.<name>]`` without a
    Python class, so ``get_overlay`` raises and the resolver re-reads
    via ``load_config`` + ``read_pass``), and the empty-overlay legacy
    branch (delegates to the process-global resolver).
    """

    def test_gitlab_credentials_from_registered_overlay(self) -> None:
        """Registered overlay: ``get_overlay(name).config`` provides token + URL."""
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import _overlay_gitlab_credentials  # noqa: PLC0415

        fake_overlay = MagicMock()
        fake_overlay.config.get_gitlab_token.return_value = "glpat-overlay-A"
        fake_overlay.config.gitlab_url = "https://gitlab.example.com/api/v4"

        with patch(
            "teatree.core.overlay_loader.get_overlay",
            return_value=fake_overlay,
        ):
            token, base_url = _overlay_gitlab_credentials("overlay-A")

        assert token == "glpat-overlay-A"
        assert base_url == "https://gitlab.example.com/api/v4"

    def test_gitlab_credentials_falls_back_to_toml_when_get_overlay_raises(self) -> None:
        """Path-only TOML overlay: ``get_overlay`` raises; resolver re-reads TOML."""
        from teatree.config import TeaTreeConfig  # noqa: PLC0415
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import _overlay_gitlab_credentials  # noqa: PLC0415

        toml_config = TeaTreeConfig(
            raw={
                "overlays": {
                    "toml-only": {
                        "gitlab_token_ref": "gitlab/toml-only-token",
                        "gitlab_url": "https://gitlab.example.org",
                    },
                },
            },
        )
        with (
            patch(
                "teatree.core.overlay_loader.get_overlay",
                side_effect=LookupError("no python class"),
            ),
            patch("teatree.config.load_config", return_value=toml_config),
            patch("teatree.utils.secrets.read_pass", return_value="glpat-from-pass"),
        ):
            token, base_url = _overlay_gitlab_credentials("toml-only")

        assert token == "glpat-from-pass"
        assert base_url == "https://gitlab.example.org/api/v4"

    def test_gitlab_credentials_empty_name_returns_blank_pair(self) -> None:
        """Empty overlay name short-circuits — legacy global-resolver path."""
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import _overlay_gitlab_credentials  # noqa: PLC0415

        assert _overlay_gitlab_credentials("") == ("", "")

    def test_gitlab_credentials_overlay_config_get_token_raises(self) -> None:
        """``config.get_gitlab_token`` raising returns empty token + default URL."""
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import _overlay_gitlab_credentials  # noqa: PLC0415

        broken = MagicMock()
        broken.config.get_gitlab_token.side_effect = RuntimeError("vault unavailable")
        broken.config.gitlab_url = "https://gitlab.com/api/v4"

        with patch(
            "teatree.core.overlay_loader.get_overlay",
            return_value=broken,
        ):
            token, base_url = _overlay_gitlab_credentials("broken-overlay")

        assert token == ""
        assert base_url == "https://gitlab.com/api/v4"

    def test_gitlab_credentials_toml_missing_overlay_returns_blank(self) -> None:
        """TOML fallback: overlay name absent from config → empty pair."""
        from teatree.config import TeaTreeConfig  # noqa: PLC0415
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import (  # noqa: PLC0415
            _overlay_gitlab_credentials_from_toml,
        )

        with patch(
            "teatree.config.load_config",
            return_value=TeaTreeConfig(raw={"overlays": {}}),
        ):
            assert _overlay_gitlab_credentials_from_toml("absent") == ("", "")

    def test_gitlab_credentials_toml_missing_token_ref_returns_blank(self) -> None:
        """TOML fallback: overlay present but no ``gitlab_token_ref`` → empty."""
        from teatree.config import TeaTreeConfig  # noqa: PLC0415
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import (  # noqa: PLC0415
            _overlay_gitlab_credentials_from_toml,
        )

        cfg = TeaTreeConfig(raw={"overlays": {"x": {"gitlab_url": "https://gl.example"}}})
        with patch("teatree.config.load_config", return_value=cfg):
            assert _overlay_gitlab_credentials_from_toml("x") == ("", "")

    def test_gitlab_credentials_toml_read_pass_raises_returns_blank_token(self) -> None:
        """TOML fallback: ``read_pass`` raising → empty token, URL preserved."""
        from teatree.config import TeaTreeConfig  # noqa: PLC0415
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import (  # noqa: PLC0415
            _overlay_gitlab_credentials_from_toml,
        )

        cfg = TeaTreeConfig(
            raw={
                "overlays": {
                    "x": {
                        "gitlab_token_ref": "gitlab/x",
                        "gitlab_url": "https://gl.example/api/v4",
                    },
                },
            },
        )
        with (
            patch("teatree.config.load_config", return_value=cfg),
            patch("teatree.utils.secrets.read_pass", side_effect=RuntimeError("pass down")),
        ):
            token, base_url = _overlay_gitlab_credentials_from_toml("x")

        assert token == ""
        assert base_url == "https://gl.example/api/v4"

    def test_resolve_github_token_for_overlay_empty_falls_through(self) -> None:
        """Empty overlay name delegates to the legacy process-global resolver."""
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import (  # noqa: PLC0415
            resolve_github_token_for_overlay,
        )

        with patch(
            "teatree.loop.scanners.outbound_audit._resolve_github_token",
            return_value="legacy-token",
        ):
            assert resolve_github_token_for_overlay("") == "legacy-token"

    def test_resolve_github_token_for_overlay_uses_registered_overlay(self) -> None:
        """Registered overlay's ``config.get_github_token`` wins over fallback."""
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import (  # noqa: PLC0415
            resolve_github_token_for_overlay,
        )

        fake_overlay = MagicMock()
        fake_overlay.config.get_github_token.return_value = "ghp-from-overlay"
        with (
            patch(
                "teatree.core.overlay_loader.get_overlay",
                return_value=fake_overlay,
            ),
            patch(
                "teatree.loop.scanners.outbound_audit._resolve_github_token",
                return_value="legacy-token",
            ),
        ):
            assert resolve_github_token_for_overlay("work") == "ghp-from-overlay"

    def test_resolve_github_token_for_overlay_falls_back_to_toml(self) -> None:
        """Registered path returns empty → TOML path resolves the token."""
        from teatree.config import TeaTreeConfig  # noqa: PLC0415
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import (  # noqa: PLC0415
            resolve_github_token_for_overlay,
        )

        cfg = TeaTreeConfig(
            raw={
                "overlays": {
                    "toml-overlay": {"github_token_ref": "github/toml"},
                },
            },
        )
        with (
            patch(
                "teatree.core.overlay_loader.get_overlay",
                side_effect=LookupError("no class"),
            ),
            patch("teatree.config.load_config", return_value=cfg),
            patch("teatree.utils.secrets.read_pass", return_value="ghp-from-toml"),
            patch(
                "teatree.loop.scanners.outbound_audit._resolve_github_token",
                return_value="legacy-token",
            ),
        ):
            assert resolve_github_token_for_overlay("toml-overlay") == "ghp-from-toml"

    def test_resolve_github_token_for_overlay_falls_through_to_legacy(self) -> None:
        """Neither registered nor TOML path resolves → legacy global resolver."""
        from teatree.config import TeaTreeConfig  # noqa: PLC0415
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import (  # noqa: PLC0415
            resolve_github_token_for_overlay,
        )

        with (
            patch(
                "teatree.core.overlay_loader.get_overlay",
                side_effect=LookupError("no class"),
            ),
            patch(
                "teatree.config.load_config",
                return_value=TeaTreeConfig(raw={"overlays": {}}),
            ),
            patch(
                "teatree.loop.scanners.outbound_audit._resolve_github_token",
                return_value="legacy-token",
            ),
        ):
            assert resolve_github_token_for_overlay("unknown") == "legacy-token"

    def test_github_token_from_registered_overlay_get_token_raises(self) -> None:
        """``config.get_github_token`` raising → empty string (no crash)."""
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import (  # noqa: PLC0415
            _github_token_from_registered_overlay,
        )

        broken = MagicMock()
        broken.config.get_github_token.side_effect = RuntimeError("vault down")
        with patch(
            "teatree.core.overlay_loader.get_overlay",
            return_value=broken,
        ):
            assert _github_token_from_registered_overlay("broken") == ""

    def test_github_token_from_toml_overlay_missing_returns_blank(self) -> None:
        """TOML fallback: overlay absent → empty string."""
        from teatree.config import TeaTreeConfig  # noqa: PLC0415
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import (  # noqa: PLC0415
            _github_token_from_toml_overlay,
        )

        with patch(
            "teatree.config.load_config",
            return_value=TeaTreeConfig(raw={"overlays": {}}),
        ):
            assert _github_token_from_toml_overlay("absent") == ""

    def test_github_token_from_toml_overlay_no_token_ref_returns_blank(self) -> None:
        """TOML fallback: overlay present but no ``github_token_ref`` → empty."""
        from teatree.config import TeaTreeConfig  # noqa: PLC0415
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import (  # noqa: PLC0415
            _github_token_from_toml_overlay,
        )

        cfg = TeaTreeConfig(raw={"overlays": {"x": {"slack_token_ref": "slack/x"}}})
        with patch("teatree.config.load_config", return_value=cfg):
            assert _github_token_from_toml_overlay("x") == ""

    def test_github_token_from_toml_overlay_read_pass_raises_returns_blank(self) -> None:
        """TOML fallback: ``read_pass`` raising → empty string."""
        from teatree.config import TeaTreeConfig  # noqa: PLC0415
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import (  # noqa: PLC0415
            _github_token_from_toml_overlay,
        )

        cfg = TeaTreeConfig(
            raw={"overlays": {"x": {"github_token_ref": "github/x"}}},
        )
        with (
            patch("teatree.config.load_config", return_value=cfg),
            patch("teatree.utils.secrets.read_pass", side_effect=RuntimeError("pass down")),
        ):
            assert _github_token_from_toml_overlay("x") == ""

    def test_gitlab_api_for_overlay_empty_name_uses_default_constructor(self) -> None:
        """Empty overlay name → ``GitLabAPI()`` legacy single-overlay default."""
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import gitlab_api_for_overlay  # noqa: PLC0415

        sentinel = object()
        with patch(
            "teatree.backends.gitlab.api.GitLabAPI",
            return_value=sentinel,
        ) as ctor:
            api = gitlab_api_for_overlay("")

        assert api is sentinel
        ctor.assert_called_once_with()

    def test_gitlab_api_for_overlay_uses_overlay_credentials(self) -> None:
        """Resolved overlay credentials → ``GitLabAPI(token=..., base_url=...)``."""
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import gitlab_api_for_overlay  # noqa: PLC0415

        fake_overlay = MagicMock()
        fake_overlay.config.get_gitlab_token.return_value = "glpat-z"
        fake_overlay.config.gitlab_url = "https://gl.example/api/v4"
        sentinel = object()
        with (
            patch(
                "teatree.core.overlay_loader.get_overlay",
                return_value=fake_overlay,
            ),
            patch(
                "teatree.backends.gitlab.api.GitLabAPI",
                return_value=sentinel,
            ) as ctor,
        ):
            api = gitlab_api_for_overlay("named")

        assert api is sentinel
        ctor.assert_called_once_with(token="glpat-z", base_url="https://gl.example/api/v4")

    def test_gitlab_api_for_overlay_returns_none_when_ctor_raises(self) -> None:
        """``GitLabAPI`` constructor raising → ``None`` (graceful degrade)."""
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import gitlab_api_for_overlay  # noqa: PLC0415

        with patch(
            "teatree.backends.gitlab.api.GitLabAPI",
            side_effect=RuntimeError("network failed at construction"),
        ):
            assert gitlab_api_for_overlay("") is None

    def test_overlay_gitlab_credentials_returns_blank_when_loader_import_fails(self) -> None:
        """``overlay_loader`` import failure → ``("", "")`` graceful fallback."""
        import builtins  # noqa: PLC0415

        from teatree.loop.scanners.outbound_audit_overlay_verifiers import _overlay_gitlab_credentials  # noqa: PLC0415

        real_import = builtins.__import__

        def _blocked(name: str, *args: object, **kwargs: object) -> object:
            if name == "teatree.core.overlay_loader":
                msg = "synthetic import block"
                raise ImportError(msg)
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_blocked):
            assert _overlay_gitlab_credentials("x") == ("", "")

    def test_overlay_gitlab_credentials_from_toml_blank_when_config_import_fails(self) -> None:
        """``teatree.config`` import failure inside the TOML helper → blank pair."""
        import builtins  # noqa: PLC0415

        from teatree.loop.scanners.outbound_audit_overlay_verifiers import (  # noqa: PLC0415
            _overlay_gitlab_credentials_from_toml,
        )

        real_import = builtins.__import__

        def _blocked(name: str, *args: object, **kwargs: object) -> object:
            if name == "teatree.config":
                msg = "config blocked"
                raise ImportError(msg)
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_blocked):
            assert _overlay_gitlab_credentials_from_toml("x") == ("", "")

    def test_github_token_from_toml_overlay_blank_when_imports_fail(self) -> None:
        """Import failure inside the TOML GitHub token helper → empty string."""
        import builtins  # noqa: PLC0415

        from teatree.loop.scanners.outbound_audit_overlay_verifiers import (  # noqa: PLC0415
            _github_token_from_toml_overlay,
        )

        real_import = builtins.__import__

        def _blocked(name: str, *args: object, **kwargs: object) -> object:
            if name == "teatree.config":
                msg = "config blocked"
                raise ImportError(msg)
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_blocked):
            assert _github_token_from_toml_overlay("x") == ""

    def test_gitlab_approve_verifier_returns_none_when_current_username_raises(self) -> None:
        """``api.current_username()`` raising → ``None`` from the factory."""
        from teatree.loop.scanners.outbound_audit_overlay_verifiers import (  # noqa: PLC0415
            gitlab_approve_verifier_for_overlay,
        )

        fake_api = MagicMock()
        fake_api.current_username.side_effect = RuntimeError("token expired")
        with patch(
            "teatree.loop.scanners.outbound_audit._gitlab_api_for_overlay",
            return_value=fake_api,
        ):
            assert gitlab_approve_verifier_for_overlay("any") is None


class DefaultVerifierForClaimGitlabApproveBranchTests(TestCase):
    """The ``gitlab_approve`` branch of ``_default_verifier_for_claim``."""

    def test_gitlab_approve_kind_dispatches_to_overlay_factory(self) -> None:
        """A claim with ``kind="gitlab_approve"`` routes through the approve factory."""
        from teatree.loop.scanners.outbound_audit import _default_verifier_for_claim  # noqa: PLC0415

        claim = MagicMock()
        claim.kind = "gitlab_approve"
        claim.extra = {"overlay": "client-A"}

        sentinel: object = object()
        with patch(
            "teatree.loop.scanners.outbound_audit._gitlab_approve_verifier_for_overlay",
            return_value=sentinel,
        ) as factory:
            result = _default_verifier_for_claim(claim)

        assert result is sentinel
        factory.assert_called_once_with("client-A")


class ResolveGithubTokenSecretsImportFailureTests(TestCase):
    """``_resolve_github_token`` graceful handling when secrets import fails."""

    def test_returns_blank_when_secrets_module_unimportable(self) -> None:
        """No env token + ``teatree.utils.secrets`` unimportable → empty string."""
        import builtins  # noqa: PLC0415
        import os  # noqa: PLC0415

        from teatree.loop.scanners.outbound_audit import _resolve_github_token  # noqa: PLC0415

        real_import = builtins.__import__

        def _blocked(name: str, *args: object, **kwargs: object) -> object:
            if name == "teatree.utils.secrets":
                msg = "secrets blocked"
                raise ImportError(msg)
            return real_import(name, *args, **kwargs)

        with (
            patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": ""}, clear=False),
            patch("builtins.__import__", side_effect=_blocked),
        ):
            assert _resolve_github_token() == ""
