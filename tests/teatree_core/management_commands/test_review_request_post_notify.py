"""``review_request_post`` fires the #949 after-receipt visibility DM.

A successful post to the review channel must be followed by exactly one
bot→user DM (``on_behalf_post:`` BotPing); a refused post (no #960
approval) must NOT emit one — the post never happened. Mirrors
``test_review_request_post_command``'s ``_FakeBackend`` harness for the
messaging boundary; the ``notify_user`` orchestration + BotPing ledger
run for real.
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.gates.review_request_guard import GuardDecision, GuardTarget
from teatree.core.models import BotPing, OnBehalfApproval


def _seed_cold_slack_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """Seed the global ``slack_user_id`` in a config-store sqlite the cold reader resolves."""
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
        conn.commit()
    finally:
        conn.close()


_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/385"
_TARGET = GuardTarget(channel_id="C_REVIEW", channel_name="the-review-team", token="xoxp")
_CMD = "teatree.core.management.commands.review_request_post"


class _FakeBackend:
    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> dict[str, object]:
        return {"ok": True, "ts": "1.23"}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://team.slack.com/archives/{channel}/p{ts.replace('.', '')}"


def _notify_backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D-OPERATOR"
    backend.post_message.return_value = {"ok": True, "ts": "1700000000.0001"}
    backend.get_permalink.return_value = "https://slack.example/archives/D-OPERATOR/p1"
    return backend


class _Base(TestCase):
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``slack_user_id`` (global) resolves via the Django-free cold reader —
        # seed it in a config-store sqlite the reader resolves via ``T3_CONFIG_DB``.
        _seed_cold_slack_user(tmp_path, monkeypatch, "U-OPERATOR")
        self.monkeypatch = monkeypatch

    def setUp(self) -> None:
        super().setUp()
        self._tmp = Path(tempfile.mkdtemp())
        self._prev_data_dir = os.environ.get("T3_DATA_DIR")
        os.environ["T3_DATA_DIR"] = str(self._tmp)

    def tearDown(self) -> None:
        if self._prev_data_dir is None:
            os.environ.pop("T3_DATA_DIR", None)
        else:
            os.environ["T3_DATA_DIR"] = self._prev_data_dir
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()

    def _run(self, *extra: str) -> int:
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf):
            try:
                call_command("review_request_post", "--mr-url", _MR_URL, "--approver", "souliane", *extra)
            except SystemExit as exc:
                code = int(exc.code) if isinstance(exc.code, int) else 1
        return code


class TestReviewRequestPostAfterReceipt(_Base):
    def test_successful_post_emits_after_receipt_dm(self) -> None:
        OnBehalfApproval.record(target=_MR_URL, action="review_request_post", approver_id="souliane")
        notify_backend = _notify_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: notify_backend)

        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.should_post_review_request", return_value=GuardDecision(action="post")),
            patch(f"{_CMD}.messaging_from_overlay", return_value=_FakeBackend()),
        ):
            code = self._run("--title", "fix(scope): thing")

        assert code == 0
        ping = BotPing.objects.get(idempotency_key=f"on_behalf_post:{_MR_URL}:review_request_post")
        assert ping.status == BotPing.Status.SENT
        assert ping.kind == BotPing.Kind.INFO

    def test_refused_post_emits_no_after_receipt_dm(self) -> None:
        notify_backend = _notify_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: notify_backend)

        # No OnBehalfApproval recorded → the #960 pre-gate refuses; the
        # post never happens so the after-receipt DM must NOT fire.
        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.should_post_review_request", return_value=GuardDecision(action="post")),
            patch(f"{_CMD}.messaging_from_overlay", return_value=_FakeBackend()),
        ):
            code = self._run("--title", "fix(scope): thing")

        assert code == 2
        assert not BotPing.objects.filter(idempotency_key__startswith="on_behalf_post:").exists()
