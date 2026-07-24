"""After-receipt visibility DM behaviour (#949).

:func:`teatree.core.on_behalf_post_receipt.notify_user_on_behalf_post`
fires *after* a colleague-visible on-behalf post has published. It DMs
the user the destination + a clickable artifact link + a one-line
summary, recorded in the ``BotPing`` ledger. The Slack backend is mocked
at the ``MessagingBackend`` boundary (``open_dm`` + ``post_message`` +
``get_permalink``) — only the unstoppable transport is replaced; the
``notify_user`` orchestration and the ``BotPing`` ledger run for real.

Asserts:

*   default-ON → exactly one DM, containing the destination, a clickable
    ``[label](url)`` link, and the summary;
*   ``notify_on_post_on_behalf = false`` → no DM (the post already
    happened; this only controls the after-receipt visibility);
*   a second call with the same ``(target, action)`` is idempotent;
*   a permalink-lookup failure falls back to the canonical URL (no raise);
*   a Slack non-delivery records one ``BotPing`` FAILED and never raises.
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.models import BotPing, ConfigSetting
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post


def _seed_cold_slack_user(tmp_path: Path, user_id: str) -> None:
    """Seed the global ``slack_user_id`` in a config-store sqlite the cold reader resolves."""
    db = tmp_path / "config.sqlite3"
    os.environ["T3_CONFIG_DB"] = str(db)
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
        conn.commit()
    finally:
        conn.close()


def _cfg(tmp_path: Path, *, enabled: bool = True) -> None:
    # ``slack_user_id`` (global) resolves via the Django-free cold reader — seed it
    # in a config-store sqlite the reader resolves via ``T3_CONFIG_DB``.
    # ``notify_on_post_on_behalf`` is DB-home (#1775, no ``T3_*`` env var) — stage
    # it in the ``ConfigSetting`` store (global scope).
    _seed_cold_slack_user(tmp_path, "U-OPERATOR")
    ConfigSetting.objects.set_value("notify_on_post_on_behalf", enabled)


def _stub_backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D-OPERATOR"
    backend.post_message.return_value = {"ok": True, "ts": "1700000000.0001"}
    backend.get_permalink.return_value = "https://slack.example/archives/D-OPERATOR/p1"
    return backend


def _post_message_text(call_args: Any) -> str:
    _, kwargs = call_args
    return str(kwargs.get("text", ""))


class TestAfterReceiptDm(TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)

    def _set_messaging(self, backend: MagicMock) -> None:
        patcher = patch("teatree.core.notify.messaging_from_overlay", lambda: backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_on_emits_one_dm_with_destination_link_summary(self) -> None:
        _cfg(self.tmp_path, enabled=True)
        backend = _stub_backend()
        self._set_messaging(backend)

        notify_user_on_behalf_post(
            target="org/repo!7",
            action="post_comment",
            destination="review channel C-eng",
            artifact_url="https://gitlab.example/org/repo/-/merge_requests/7#note_42",
            summary="LGTM on org/repo!7",
        )

        key = "on_behalf_post:org/repo!7:post_comment"
        ping = BotPing.objects.get(idempotency_key=key)
        assert ping.kind == BotPing.Kind.INFO
        assert ping.status == BotPing.Status.SENT
        backend.post_message.assert_called_once()
        sent = _post_message_text(backend.post_message.call_args)
        assert "review channel C-eng" in sent
        assert "LGTM on org/repo!7" in sent
        # maybe_linkify converts [label](url) → <url|label>; the artifact
        # URL must be present in the rendered Slack link.
        assert "https://gitlab.example/org/repo/-/merge_requests/7#note_42" in sent

    def test_non_url_artifact_id_is_not_leaked_as_a_dead_link(self) -> None:
        # C3: when the artifact is a non-URL internal id (a raw Slack channel id, a
        # note_id/discussion_id/line_code), the receipt must NOT emit it as a dead
        # ``[id](id)`` markdown link. The id appears ONLY in ``artifact_url`` here, so
        # its absence proves the guard dropped the link rather than a coincidence.
        _cfg(self.tmp_path, enabled=True)
        backend = _stub_backend()
        self._set_messaging(backend)

        notify_user_on_behalf_post(
            target="org/repo!7",
            action="post_comment",
            destination="the review channel",
            artifact_url="C07ABCDEF",
            summary="left a review note",
        )

        ping = BotPing.objects.get(idempotency_key="on_behalf_post:org/repo!7:post_comment")
        assert ping.status == BotPing.Status.SENT
        assert "the review channel" in ping.text
        assert "left a review note" in ping.text
        assert "C07ABCDEF" not in ping.text
        assert "](" not in ping.text

    def test_toggle_off_suppresses_dm_post_still_happened(self) -> None:
        _cfg(self.tmp_path, enabled=False)
        backend = _stub_backend()
        self._set_messaging(backend)

        notify_user_on_behalf_post(
            target="org/repo!7",
            action="post_comment",
            destination="review channel C-eng",
            artifact_url="https://gitlab.example/x",
            summary="LGTM",
        )

        assert BotPing.objects.count() == 0
        backend.post_message.assert_not_called()

    def test_double_call_idempotent(self) -> None:
        _cfg(self.tmp_path, enabled=True)
        backend = _stub_backend()
        self._set_messaging(backend)

        for _ in range(2):
            notify_user_on_behalf_post(
                target="org/repo!7",
                action="post_comment",
                destination="review channel C-eng",
                artifact_url="https://gitlab.example/x",
                summary="LGTM",
            )

        assert BotPing.objects.filter(idempotency_key="on_behalf_post:org/repo!7:post_comment").count() == 1
        assert backend.post_message.call_count == 1

    def test_permalink_lookup_failure_falls_back_to_canonical_url(self) -> None:
        _cfg(self.tmp_path, enabled=True)
        backend = _stub_backend()
        backend.get_permalink.side_effect = RuntimeError("slack permalink boom")
        self._set_messaging(backend)

        # Must not raise — the post already happened.
        notify_user_on_behalf_post(
            target="org/repo!7",
            action="post_comment",
            destination="review channel C-eng",
            artifact_url="https://gitlab.example/org/repo/-/merge_requests/7",
            summary="LGTM",
        )

        ping = BotPing.objects.get(idempotency_key="on_behalf_post:org/repo!7:post_comment")
        # Permalink lookup failed → BotPing.permalink is empty but the DM
        # still SENT carrying the canonical artifact URL in its body.
        assert ping.status == BotPing.Status.SENT
        assert "https://gitlab.example/org/repo/-/merge_requests/7" in ping.text

    def test_notify_failure_does_not_raise_and_records_durable_row(self) -> None:
        _cfg(self.tmp_path, enabled=True)
        backend = _stub_backend()
        backend.post_message.return_value = {"ok": False, "error": "missing_scope"}
        self._set_messaging(backend)

        # Record-and-proceed: never raise into the caller.
        notify_user_on_behalf_post(
            target="org/repo!7",
            action="post_comment",
            destination="review channel C-eng",
            artifact_url="https://gitlab.example/x",
            summary="LGTM",
        )

        ping = BotPing.objects.get(idempotency_key="on_behalf_post:org/repo!7:post_comment")
        assert ping.status == BotPing.Status.FAILED
