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

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from teatree.core.models import BotPing
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True) -> None:
    cfg = tmp_path / ".teatree.toml"
    toggle = "true" if enabled else "false"
    cfg.write_text(
        f'[teatree]\nslack_user_id = "U-OPERATOR"\nnotify_on_post_on_behalf = {toggle}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


def _stub_backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D-OPERATOR"
    backend.post_message.return_value = {"ok": True, "ts": "1700000000.0001"}
    backend.get_permalink.return_value = "https://slack.example/archives/D-OPERATOR/p1"
    return backend


def _post_message_text(call_args: Any) -> str:
    _, kwargs = call_args
    return str(kwargs.get("text", ""))


class TestAfterReceiptDm:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_default_on_emits_one_dm_with_destination_link_summary(self) -> None:
        _cfg(self.tmp_path, self.monkeypatch, enabled=True)
        backend = _stub_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)

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

    def test_toggle_off_suppresses_dm_post_still_happened(self) -> None:
        _cfg(self.tmp_path, self.monkeypatch, enabled=False)
        backend = _stub_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)

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
        _cfg(self.tmp_path, self.monkeypatch, enabled=True)
        backend = _stub_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)

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
        _cfg(self.tmp_path, self.monkeypatch, enabled=True)
        backend = _stub_backend()
        backend.get_permalink.side_effect = RuntimeError("slack permalink boom")
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)

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
        _cfg(self.tmp_path, self.monkeypatch, enabled=True)
        backend = _stub_backend()
        backend.post_message.return_value = {"ok": False, "error": "missing_scope"}
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)

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
