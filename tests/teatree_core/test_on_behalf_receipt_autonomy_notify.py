"""Every autonomy tier sends the after-receipt DM on on-behalf actions."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from teatree.core.models import BotPing, ConfigSetting
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post


def _seed_cold_slack_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overlay: str, user_id: str) -> None:
    """Seed global + per-overlay ``slack_user_id`` in a config-store sqlite the cold reader resolves."""
    db = tmp_path / "config.sqlite3"
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'slack_user_id', ?)",
            (json.dumps(user_id),),
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'overlays', ?)",
            (json.dumps({overlay: {"slack_user_id": user_id}}),),
        )
        conn.commit()
    finally:
        conn.close()


# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    overlay: str,
    autonomy: str,
) -> None:
    # ``slack_user_id`` (both global and per-overlay) resolves via the Django-free
    # cold reader — seed both in a config-store sqlite the reader resolves via
    # ``T3_CONFIG_DB`` so notify_user resolves the user id. ``autonomy`` is DB-home (#1775); stage it in the
    # ``ConfigSetting`` store scoped to the overlay.
    _seed_cold_slack_user(tmp_path, monkeypatch, overlay, "U-OPERATOR")
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])
    monkeypatch.setenv("T3_OVERLAY_NAME", overlay)
    ConfigSetting.objects.set_value("autonomy", autonomy, scope=overlay)


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
        """The ``notify`` tier sends a receipt."""
        _stage(
            self.tmp_path,
            self.monkeypatch,
            overlay="t3-client",
            autonomy="notify",
        )
        backend = _stub_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)

        _post("approve")

        backend.post_message.assert_called_once()
        assert (
            BotPing.objects.filter(idempotency_key__startswith="on_behalf_post:client/product!42:approve").count() == 1
        )

    def test_client_notify_post_comment_fires_exactly_one_dm(self) -> None:
        _stage(self.tmp_path, self.monkeypatch, overlay="t3-client", autonomy="notify")
        backend = _stub_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)

        _post("post_comment")

        backend.post_message.assert_called_once()

    def test_full_teatree_action_also_fires_dm(self) -> None:
        _stage(self.tmp_path, self.monkeypatch, overlay="t3-teatree", autonomy="full")
        backend = _stub_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)

        _post("approve")

        backend.post_message.assert_called_once()
        assert BotPing.objects.count() == 1
