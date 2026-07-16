"""Tests for the waiting-on-you digest scanner (PR-21).

The digest is INTERNAL: it records a terminal ``BotPing.LOGGED`` audit row and
surfaces a local statusline signal, but NEVER DMs the owner (the owner allowlist
classifies the waiting digest as internal noise).
"""

import pytest

from teatree.core.models import BotPing
from teatree.core.models.waiting_item import WaitingItem
from teatree.loop.scanners.waiting_digest import WaitingDigestScanner


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestWaitingDigestScanner:
    def test_no_signal_when_nothing_waiting(self) -> None:
        assert WaitingDigestScanner().scan() == []
        assert BotPing.objects.count() == 0

    def test_records_an_internal_logged_row_never_a_dm(self) -> None:
        WaitingItem.objects.add("chase finance")
        signals = WaitingDigestScanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == "waiting.digest"
        row = BotPing.objects.get()
        assert row.status == BotPing.Status.LOGGED
        assert row.audience == "internal"
        assert "chase finance" in row.text

    def test_deduped_to_once_per_content_hash(self) -> None:
        WaitingItem.objects.add("chase finance")
        first = WaitingDigestScanner().scan()
        second = WaitingDigestScanner().scan()
        assert len(first) == 1
        assert second == []  # unchanged content → no fresh signal
        assert BotPing.objects.filter(status=BotPing.Status.LOGGED).count() == 1

    def test_new_content_re_records(self) -> None:
        WaitingItem.objects.add("chase finance")
        WaitingDigestScanner().scan()
        WaitingItem.objects.add("call the bank")  # content changes → new hash
        signals = WaitingDigestScanner().scan()
        assert len(signals) == 1
        assert BotPing.objects.filter(status=BotPing.Status.LOGGED).count() == 2
