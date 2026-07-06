"""Tests for the durable stale-clone notice helper (#2836).

When self-update / ``t3 update`` must skip a dirty or detached/off-default
clone, the skip becomes a DURABLE user-facing notice (a ``BotPing``-backed
bot→user DM) instead of a silent ``logger`` line. With no messaging backend the
notice is still recorded as a durable ``BotPing`` NOOP audit row — that
durability is the whole point. These tests force the no-backend path
(``messaging_from_overlay`` → ``None``) so they are hermetic and never attempt a
real Slack send.
"""

import pytest

from teatree.core.models import BotPing
from teatree.core.worktree.stale_clone_notice import (
    StaleCloneReason,
    StaleCloneSkip,
    notify_stale_clone_skip,
    stale_clone_message,
)

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the no-backend path so the notice records a durable NOOP audit row."""
    monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: None)
    monkeypatch.setattr("teatree.core.notify._feature_enabled", lambda: True)


def _skip(
    reason: StaleCloneReason, *, head_sha: str = "abc123def456", default_branch: str = "", detail: str = ""
) -> StaleCloneSkip:
    return StaleCloneSkip(
        label="teatree",
        repo_path="/clones/teatree",
        reason=reason,
        head_sha=head_sha,
        default_branch=default_branch,
        detail=detail,
    )


class TestDurableNoticeRecorded:
    def test_dirty_skip_records_durable_botping_naming_path_and_remediation(self) -> None:
        notify_stale_clone_skip(_skip(StaleCloneReason.DIRTY, detail="dirty_tracked:src/app.py"))
        row = BotPing.objects.get(idempotency_key__startswith="stale_clone_skip:teatree:dirty:")
        assert row.status == BotPing.Status.NOOP
        assert "/clones/teatree" in row.text
        assert "re-run `t3 update`" in row.text

    def test_off_default_skip_records_switch_remediation(self) -> None:
        notify_stale_clone_skip(_skip(StaleCloneReason.OFF_DEFAULT, default_branch="main", detail="branch=HEAD!=main"))
        row = BotPing.objects.get(idempotency_key__startswith="stale_clone_skip:teatree:off_default:")
        assert "git switch main" in row.text

    def test_idempotent_per_clone_reason_and_head(self) -> None:
        for _ in range(3):
            notify_stale_clone_skip(_skip(StaleCloneReason.DIRTY))
        assert BotPing.objects.filter(idempotency_key__startswith="stale_clone_skip:teatree:dirty:").count() == 1

    def test_distinct_head_re_notifies(self) -> None:
        for sha in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
            notify_stale_clone_skip(_skip(StaleCloneReason.DIRTY, head_sha=sha))
        assert BotPing.objects.filter(idempotency_key__startswith="stale_clone_skip:teatree:dirty:").count() == 2


class TestNeverRaises:
    def test_notify_failure_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A notify_user crash must never break the update flow the notice rides on.
        def _boom(*_args: object, **_kwargs: object) -> bool:
            raise RuntimeError

        monkeypatch.setattr("teatree.core.notify.notify_user", _boom)
        assert notify_stale_clone_skip(_skip(StaleCloneReason.DIRTY)) is False


class TestMessage:
    def test_dirty_message_mentions_stale_and_path(self) -> None:
        msg = stale_clone_message(_skip(StaleCloneReason.DIRTY, default_branch="main", detail="dirty_tracked:x"))
        assert "STALE" in msg
        assert "/clones/teatree" in msg
        assert "uncommitted tracked changes" in msg

    def test_off_default_message_mentions_branch(self) -> None:
        msg = stale_clone_message(
            _skip(StaleCloneReason.OFF_DEFAULT, default_branch="main", detail="branch=HEAD!=main")
        )
        assert "off its default branch" in msg
        assert "git switch main" in msg
