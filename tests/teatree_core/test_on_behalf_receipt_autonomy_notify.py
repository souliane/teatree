"""The ``notify`` autonomy tier fires the after-receipt DM on every on-behalf action (#1668).

B.3 — coherence: the ``notify`` tier's contract is "act autonomously on
colleague-facing actions BUT DM me on every one". The derived
``notify_on_behalf`` field forces the after-receipt visibility DM on through
the ONE canonical egress (``teatree.core.notify.notify_user`` →
``notify_user_on_behalf_post``), never a parallel notifier. The ``full`` tier
is silent (``notify_on_behalf = False``).

These tests drive the real autonomy resolution (a ``[overlays.<n>]`` table +
``T3_OVERLAY_NAME``) so the wiring is exercised end-to-end, mocking only the
unstoppable Slack transport at the ``MessagingBackend`` boundary.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from teatree.core.models import BotPing, ConfigSetting
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    overlay: str,
    autonomy: str,
    notify_on_post_on_behalf: bool | None = None,
) -> None:
    # ``slack_user_id`` is a RAW key (TOML-home) — keep both the global and the
    # per-overlay value in TOML so notify_user resolves the user id. ``autonomy``
    # and ``notify_on_post_on_behalf`` are DB-home (#1775, no ``T3_*`` env var)
    # so a TOML value for them is ignored on read — stage ``autonomy`` in the
    # ``ConfigSetting`` store scoped to the overlay, and the global toggle in the
    # global scope.
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f'[teatree]\nslack_user_id = "U-OPERATOR"\n[overlays.{overlay}]\nslack_user_id = "U-OPERATOR"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])
    monkeypatch.setenv("T3_OVERLAY_NAME", overlay)
    ConfigSetting.objects.set_value("autonomy", autonomy, scope=overlay)
    if notify_on_post_on_behalf is not None:
        ConfigSetting.objects.set_value("notify_on_post_on_behalf", notify_on_post_on_behalf)


def _stub_backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D-OPERATOR"
    backend.post_message.return_value = {"ok": True, "ts": "1700000000.0001"}
    backend.get_permalink.return_value = "https://slack.example/archives/D-OPERATOR/p1"
    return backend


def _post(action: str) -> None:
    notify_user_on_behalf_post(
        target="client/product!42",
        action=action,
        destination="review channel C-eng",
        artifact_url="https://gitlab.example/client/product/-/merge_requests/42#note_7",
        summary=f"{action} on client/product!42",
    )


class TestNotifyTierFiresAfterReceiptDm:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_client_notify_approve_fires_exactly_one_dm(self) -> None:
        """The ``notify`` tier forces the DM even with the #949 toggle OFF."""
        _stage(
            self.tmp_path,
            self.monkeypatch,
            overlay="t3-client",
            autonomy="notify",
            notify_on_post_on_behalf=False,
        )
        backend = _stub_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)

        _post("approve")

        backend.post_message.assert_called_once()
        assert BotPing.objects.filter(idempotency_key="on_behalf_post:client/product!42:approve").count() == 1

    def test_client_notify_post_comment_fires_exactly_one_dm(self) -> None:
        _stage(self.tmp_path, self.monkeypatch, overlay="t3-client", autonomy="notify")
        backend = _stub_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)

        _post("post_comment")

        backend.post_message.assert_called_once()

    def test_full_teatree_action_does_not_fire_dm(self) -> None:
        """``full`` is silent — with the #949 toggle off too, no DM fires.

        Pairing ``autonomy = full`` with ``notify_on_post_on_behalf = false``
        isolates ``notify_on_behalf`` as the load-bearing trigger: ``full``
        derives it False, so neither gate is on and the post stays silent.
        """
        _stage(
            self.tmp_path,
            self.monkeypatch,
            overlay="t3-teatree",
            autonomy="full",
            notify_on_post_on_behalf=False,
        )
        backend = _stub_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)

        _post("approve")

        backend.post_message.assert_not_called()
        assert BotPing.objects.count() == 0
