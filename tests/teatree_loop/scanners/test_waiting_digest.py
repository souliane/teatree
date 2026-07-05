"""Tests for the waiting-on-you Slack digest scanner (PR-21)."""

from unittest.mock import MagicMock

import pytest

from teatree.core.models import BotPing
from teatree.core.models.waiting_item import WaitingItem
from teatree.loop.scanners.waiting_digest import WaitingDigestScanner


def _backend() -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    b.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000000"
    return b


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestWaitingDigestScanner:
    def test_no_post_when_nothing_waiting(self) -> None:
        backend = _backend()
        signals = WaitingDigestScanner(backend=backend, user_id="U_ME").scan()
        assert signals == []
        backend.post_message.assert_not_called()

    def test_posts_a_table_digest_with_blocks(self) -> None:
        WaitingItem.objects.add("chase finance")
        backend = _backend()
        signals = WaitingDigestScanner(backend=backend, user_id="U_ME").scan()
        assert len(signals) == 1
        assert signals[0].kind == "waiting.digest"
        backend.post_message.assert_called_once()
        call_kwargs = backend.post_message.call_args.kwargs
        assert [b["type"] for b in call_kwargs["blocks"]] == ["section", "table"]
        assert "chase finance" in call_kwargs["text"]

    def test_deduped_to_once_per_content_hash(self) -> None:
        WaitingItem.objects.add("chase finance")
        backend = _backend()
        first = WaitingDigestScanner(backend=backend, user_id="U_ME").scan()
        second = WaitingDigestScanner(backend=backend, user_id="U_ME").scan()
        assert len(first) == 1
        assert second == []  # unchanged content → no re-post, no fresh signal
        backend.post_message.assert_called_once()
        assert BotPing.objects.filter(status=BotPing.Status.SENT).count() == 1

    def test_new_content_reposts(self) -> None:
        WaitingItem.objects.add("chase finance")
        backend = _backend()
        WaitingDigestScanner(backend=backend, user_id="U_ME").scan()
        WaitingItem.objects.add("call the bank")  # content changes → new hash
        signals = WaitingDigestScanner(backend=backend, user_id="U_ME").scan()
        assert len(signals) == 1
        assert backend.post_message.call_count == 2
