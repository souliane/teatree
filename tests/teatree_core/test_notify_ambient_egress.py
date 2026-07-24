"""The headless bot→user egress must resolve a backend and name its failure reason.

The observed incident: five completed reviews were never announced. ``notify_user``
returned a bare ``False`` and the ledger recorded the conflated reason "no messaging
backend or user_id configured", while ``t3 doctor`` simultaneously reported the owner
id resolving fine. The cause was AMBIENT resolution — the headless worker exports no
``T3_OVERLAY_NAME``, so ``messaging_from_overlay()`` called ``get_overlay(None)``,
which raises ``Multiple overlays found`` once a second overlay is registered; the
factory swallowed that into ``None``. ``resolve_user_id`` already had an
env-independent fallback tier, the messaging factory did not.
"""

import os
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.backends.types import Service
from teatree.core.backend_factory import messaging_from_overlay, reset_backend_caches
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import BotPing
from teatree.core.notify import (
    NotifyKind,
    NotifyOptions,
    NotifyReason,
    notify_user,
    notify_user_outcome,
    resolve_owner_dm_backend,
)
from teatree.core.overlay import OverlayBase, OverlayConfig

_AMBIENT = {"T3_OVERLAY_NAME": ""}


class _SlackOverlay(OverlayBase):
    config = OverlayConfig(messaging_backend="slack", required_third_party_services=frozenset({Service.SLACK}))

    def get_repos(self):
        return []

    def get_provision_steps(self, worktree):
        return []


class _NoopOverlay(OverlayBase):
    config = OverlayConfig(messaging_backend="noop", required_third_party_services=frozenset({Service.SLACK}))

    def get_repos(self):
        return []

    def get_provision_steps(self, worktree):
        return []


@pytest.fixture(autouse=True)
def _reset_caches() -> Iterator[None]:
    reset_backend_caches()
    yield
    reset_backend_caches()


def _overlays(**registry: OverlayBase):
    return patch.object(overlay_loader_mod, "_discover_overlays", return_value=registry)


def _loader_by_declaration(overlay: OverlayBase) -> object:
    """What the real loader does: a real backend for a slack declarer, a no-op otherwise."""
    if overlay.config.messaging_backend == "slack":
        return "real-slack"
    return NoopMessagingBackend()


class TestAmbientMessagingResolution:
    """``messaging_from_overlay()`` with no ``T3_OVERLAY_NAME`` — the headless seam."""

    def test_resolves_the_sole_credentialed_overlay_when_several_are_registered(self) -> None:
        with (
            _overlays(noop_side=_NoopOverlay(), slack_side=_SlackOverlay()),
            patch.dict(os.environ, _AMBIENT),
            patch("teatree.backends.loader.get_messaging", side_effect=_loader_by_declaration),
        ):
            assert messaging_from_overlay() == "real-slack"

    def test_stays_none_when_two_overlays_both_carry_a_real_transport(self) -> None:
        with (
            _overlays(one=_SlackOverlay(), two=_SlackOverlay()),
            patch.dict(os.environ, _AMBIENT),
            patch("teatree.backends.loader.get_messaging", side_effect=_loader_by_declaration),
        ):
            assert messaging_from_overlay() is None

    def test_stays_none_when_every_registered_overlay_is_noop(self) -> None:
        with (
            _overlays(one=_NoopOverlay(), two=_NoopOverlay()),
            patch.dict(os.environ, _AMBIENT),
        ):
            assert messaging_from_overlay() is None

    def test_a_named_overlay_never_borrows_the_credentialed_overlays_backend(self) -> None:
        """The new fallback is AMBIENT-only: asking for an overlay by name still answers about IT."""
        with (
            _overlays(noop_side=_NoopOverlay(), slack_side=_SlackOverlay()),
            patch("teatree.backends.loader.get_messaging", side_effect=_loader_by_declaration),
        ):
            assert isinstance(messaging_from_overlay("noop_side"), NoopMessagingBackend)
            reset_backend_caches()
            with patch.dict(os.environ, _AMBIENT):
                assert messaging_from_overlay() == "real-slack"


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D-USER"
    backend.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    backend.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000000"
    return backend


class TestNotifyOutcomeNamesTheReason(TestCase):
    """A non-send must tell its caller WHY — a bare ``False`` is what hid the incident."""

    def test_missing_backend_is_reported_distinctly_from_a_missing_owner_id(self) -> None:
        with patch("teatree.core.notify.messaging_from_overlay", return_value=None):
            outcome = notify_user_outcome(
                "five reviews are done",
                kind=NotifyKind.INFO,
                idempotency_key="no-backend",
                audience=NotifyAudience.OWNER_DELIVERY,
                options=NotifyOptions(user_id="U_ME"),
            )
        assert outcome.sent is False
        assert outcome.reason is NotifyReason.NO_MESSAGING_BACKEND

    def test_missing_owner_id_is_reported_distinctly_from_a_missing_backend(self) -> None:
        with patch("teatree.core.notify.resolve_user_id", return_value=""):
            outcome = notify_user_outcome(
                "five reviews are done",
                kind=NotifyKind.INFO,
                idempotency_key="no-user-id",
                audience=NotifyAudience.OWNER_DELIVERY,
                options=NotifyOptions(backend=_backend()),
            )
        assert outcome.sent is False
        assert outcome.reason is NotifyReason.NO_USER_ID

    def test_the_ledger_records_the_precise_reason_not_the_conflated_one(self) -> None:
        with patch("teatree.core.notify.messaging_from_overlay", return_value=None):
            notify_user_outcome(
                "five reviews are done",
                kind=NotifyKind.INFO,
                idempotency_key="ledger-reason",
                audience=NotifyAudience.OWNER_DELIVERY,
                options=NotifyOptions(user_id="U_ME"),
            )
        row = BotPing.objects.get(idempotency_key="ledger-reason")
        assert row.status == BotPing.Status.NOOP
        assert row.error_message == NotifyReason.NO_MESSAGING_BACKEND.detail

    def test_a_disabled_feature_is_named_rather_than_indistinguishable_from_a_dead_transport(self) -> None:
        with patch("teatree.core.notify._feature_enabled", return_value=False):
            outcome = notify_user_outcome(
                "five reviews are done",
                kind=NotifyKind.INFO,
                idempotency_key="disabled",
                audience=NotifyAudience.OWNER_DELIVERY,
                options=NotifyOptions(backend=_backend(), user_id="U_ME"),
            )
        assert outcome.reason is NotifyReason.FEATURE_DISABLED

    def test_a_delivered_dm_carries_no_reason(self) -> None:
        outcome = notify_user_outcome(
            "five reviews are done",
            kind=NotifyKind.INFO,
            idempotency_key="delivered",
            audience=NotifyAudience.OWNER_DELIVERY,
            options=NotifyOptions(backend=_backend(), user_id="U_ME"),
        )
        assert outcome.sent is True
        assert outcome.reason is NotifyReason.NONE

    def test_the_bool_egress_keeps_its_contract_for_existing_call_sites(self) -> None:
        assert (
            notify_user(
                "five reviews are done",
                kind=NotifyKind.INFO,
                idempotency_key="bool-contract",
                audience=NotifyAudience.OWNER_DELIVERY,
                backend=_backend(),
                user_id="U_ME",
            )
            is True
        )


def _by_declaration(backend: MagicMock) -> object:
    """The real loader shape: *backend* for a slack declarer, a no-op otherwise."""

    def build(overlay: OverlayBase) -> object:
        return backend if overlay.config.messaging_backend == "slack" else NoopMessagingBackend()

    return build


class TestOwnerDmTransportResolution(TestCase):
    """A working credential anywhere on the box must deliver; a refusal must name its fix.

    The second half of the incident: the ACTIVE overlay resolving a truthy
    ``NoopMessagingBackend`` sailed past the ``is None`` guard and dropped the
    owner DM behind a misleading Slack-shaped delivery error, while a sibling
    overlay's working credential sat unused. The owner is a box-global target,
    so the egress mirrors ``resolve_user_id``'s tier order: active overlay →
    sole credentialed overlay → a typed, actionable refusal.
    """

    def test_an_active_noop_overlay_falls_back_to_the_sole_credentialed_transport(self) -> None:
        backend = _backend()
        with (
            _overlays(noop_side=_NoopOverlay(), slack_side=_SlackOverlay()),
            patch.dict(os.environ, {"T3_OVERLAY_NAME": "noop_side"}),
            patch("teatree.backends.loader.get_messaging", side_effect=_by_declaration(backend)),
        ):
            outcome = notify_user_outcome(
                "review of !42 is done",
                kind=NotifyKind.INFO,
                idempotency_key="fallback-delivers",
                audience=NotifyAudience.OWNER_DELIVERY,
                options=NotifyOptions(user_id="U_ME"),
            )

        assert outcome.sent is True
        backend.post_message.assert_called_once()
        assert BotPing.objects.get(idempotency_key="fallback-delivers").status == BotPing.Status.SENT

    def test_a_box_with_no_transport_names_the_fix_and_shouts(self) -> None:
        with (
            _overlays(one=_NoopOverlay(), two=_NoopOverlay()),
            patch.dict(os.environ, _AMBIENT),
            self.assertLogs("teatree.core.notify", level="ERROR") as logs,
        ):
            outcome = notify_user_outcome(
                "review of !42 is done",
                kind=NotifyKind.INFO,
                idempotency_key="no-transport-anywhere",
                audience=NotifyAudience.OWNER_DELIVERY,
                options=NotifyOptions(user_id="U_ME"),
            )

        assert outcome.sent is False
        assert outcome.reason is NotifyReason.NO_MESSAGING_BACKEND
        assert "t3 setup slack-bot" in outcome.detail
        assert any("DID NOT DELIVER" in line for line in logs.output)
        row = BotPing.objects.get(idempotency_key="no-transport-anywhere")
        assert row.status == BotPing.Status.NOOP
        assert row.error_message == outcome.detail

    def test_two_credentialed_overlays_with_no_active_one_name_the_ambiguity(self) -> None:
        with (
            _overlays(one=_SlackOverlay(), two=_SlackOverlay()),
            patch.dict(os.environ, _AMBIENT),
            patch("teatree.backends.loader.get_messaging", side_effect=_by_declaration(_backend())),
        ):
            outcome = notify_user_outcome(
                "review of !42 is done",
                kind=NotifyKind.INFO,
                idempotency_key="two-transports",
                audience=NotifyAudience.OWNER_DELIVERY,
                options=NotifyOptions(user_id="U_ME"),
            )

        assert outcome.sent is False
        assert outcome.reason is NotifyReason.AMBIGUOUS_OVERLAY
        assert "T3_OVERLAY_NAME" in outcome.detail

    def test_the_resolver_never_hands_out_a_noop_backend(self) -> None:
        with (
            _overlays(sole=_NoopOverlay()),
            patch.dict(os.environ, _AMBIENT),
        ):
            backend, refusal = resolve_owner_dm_backend()

        assert backend is None
        assert refusal is NotifyReason.NO_MESSAGING_BACKEND
