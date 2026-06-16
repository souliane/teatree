"""Every colleague-visible ``ReviewService`` publish fires the #949 after-receipt DM.

``post_comment``, ``reply_to_discussion``, ``resolve_discussion``,
``update_note``, ``delete_discussion`` and ``publish_draft_notes`` each
publish a colleague-visible mutation on a GitLab MR under the user's
identity — a successful call must be followed by exactly one
``on_behalf_post:`` bot→user DM.

``post_draft_note`` is the draft-form exception: drafts are
colleague-invisible until published, so it must yield ONLY the
``on_behalf_autodraft:`` BotPing (the pre-gate's own DM) and never an
``on_behalf_post:`` one.

The GitLab API boundary is stubbed; ``notify_user`` + the BotPing ledger
run for real. The on-behalf pre-gate is set to ``immediate`` via the
test config so these tests isolate the after-receipt behaviour.
"""

from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from teatree.cli.review import ReviewService
from teatree.core.models import BotPing

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _write_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: str) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f'[teatree]\nslack_user_id = "U-OPERATOR"\non_behalf_post_mode = "{mode}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


def _http_404() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://gitlab.example/api/v4/x")
    response = httpx.Response(HTTPStatus.NOT_FOUND, request=request)
    return httpx.HTTPStatusError("not found", request=request, response=response)


def _wire_notify_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MagicMock()
    backend.open_dm.return_value = "D-OPERATOR"
    backend.post_message.return_value = {"ok": True, "ts": "1700000000.0001"}
    backend.get_permalink.return_value = "https://slack.example/archives/D-OPERATOR/p1"
    monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)


class _StubAPI:
    """In-memory ``GitLabAPI`` stand-in returning success shapes."""

    def __init__(self) -> None:
        self._deleted_ids: set[str] = set()

    def post_json(self, endpoint: str, payload: object) -> dict[str, object]:
        return {
            "id": 11,
            "web_url": "https://gitlab.example/org/repo/-/mr/7#note_11",
            "notes": [{"type": "DiffNote", "id": 11}],
        }

    def post_status(self, endpoint: str) -> int:
        return 200

    def put_status(self, endpoint: str, payload: object | None = None) -> int:
        return 200

    def current_username(self) -> str:
        return "souliane"

    def get_json(self, endpoint: str) -> object:
        # Verify-after-post (#2081) reads the artifact back: confirm it landed.
        last = endpoint.rstrip("/").rsplit("/", 1)[-1]
        if last.isdigit():
            if last in self._deleted_ids:
                raise _http_404()
            return {"id": int(last), "resolvable": True, "resolved": True}
        if endpoint.endswith("/approvals"):
            return {"approved_by": [{"user": {"username": "souliane"}}]}
        if last == "draft_notes":
            return []
        if last == "notes":
            return [{"id": 99, "author": {"username": "souliane"}}]
        if "discussions/" in endpoint:
            return {"notes": [{"resolvable": True, "resolved": True}]}
        return []

    def delete(self, endpoint: str) -> int:
        self._deleted_ids.add(endpoint.rstrip("/").rsplit("/", 1)[-1])
        return 204


def _service(monkeypatch: pytest.MonkeyPatch) -> ReviewService:
    service = ReviewService(token="t")
    stub = _StubAPI()
    monkeypatch.setattr(service, "_get_api", lambda: stub)
    return service


class TestReviewServiceAfterReceiptDm:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, mode="immediate")
        _wire_notify_backend(monkeypatch)
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.svc = _service(monkeypatch)

    def _ping(self, action: str) -> BotPing:
        return BotPing.objects.get(idempotency_key=f"on_behalf_post:org/repo!7:{action}")

    def test_post_comment_emits_after_receipt_dm(self) -> None:
        # Default ``post_comment`` is a DRAFT under #1207 — the after-receipt
        # DM is for colleague-visible publishes only, so use the ``--live``
        # path (gated on a recorded ``LivePostApproval``) to exercise it.
        from teatree.core.models import LivePostApproval  # noqa: PLC0415

        LivePostApproval.record(mr_url="org/repo!7", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")
        _, code = self.svc.post_comment("org/repo", 7, "lgtm", live=True)
        assert code == 0
        assert self._ping("post_comment").status == BotPing.Status.SENT

    def test_reply_to_discussion_emits_after_receipt_dm(self) -> None:
        _, code = self.svc.reply_to_discussion("org/repo", 7, "d1", "thanks")
        assert code == 0
        assert self._ping("reply_to_discussion").status == BotPing.Status.SENT

    def test_resolve_discussion_emits_after_receipt_dm(self) -> None:
        _, code = self.svc.resolve_discussion("org/repo", 7, "d1")
        assert code == 0
        assert self._ping("resolve_discussion").status == BotPing.Status.SENT

    def test_update_note_emits_after_receipt_dm(self) -> None:
        _, code = self.svc.update_note("org/repo", 7, 11, "edited")
        assert code == 0
        assert self._ping("update_note").status == BotPing.Status.SENT

    def test_delete_discussion_emits_after_receipt_dm(self) -> None:
        _, code = self.svc.delete_discussion("org/repo", 7, 11)
        assert code == 0
        assert self._ping("delete_discussion").status == BotPing.Status.SENT

    def test_publish_draft_notes_emits_after_receipt_dm(self) -> None:
        _, code = self.svc.publish_draft_notes("org/repo", 7)
        assert code == 0
        assert self._ping("publish_draft_notes").status == BotPing.Status.SENT

    def test_post_draft_note_yields_only_autodraft_never_after_receipt(self) -> None:
        """Scope guard: drafts are colleague-invisible — no on_behalf_post: DM.

        Under DRAFT_OR_ASK the pre-gate DMs an ``on_behalf_autodraft:``
        ping; the after-receipt helper must NOT also fire an
        ``on_behalf_post:`` one (the draft is not colleague-visible).
        """
        _write_cfg(self.tmp_path, self.monkeypatch, mode="draft_or_ask")

        _, code = self.svc.post_draft_note("org/repo", 7, "nit")

        assert code == 0
        assert BotPing.objects.filter(idempotency_key="on_behalf_autodraft:org/repo!7:post_draft_note").exists()
        assert not BotPing.objects.filter(idempotency_key__startswith="on_behalf_post:").exists()
