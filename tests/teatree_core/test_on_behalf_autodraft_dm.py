"""Auto-draft DM behaviour under ``DRAFT_OR_ASK`` + ``post_draft_note`` (#960).

When the gate verdict is :attr:`~teatree.on_behalf_gate.OnBehalfVerdict.AUTO_DRAFT`,
``require_on_behalf_approval`` calls
:func:`teatree.notify.notify_user` with an idempotency key uniquely
keyed off the ``(target, action)`` pair. The Slack backend is mocked at
the ``MessagingBackend`` boundary (``open_dm`` + ``post_message`` +
``get_permalink``) — only the unstoppable external transport is
replaced; the ``notify_user`` orchestration, the ``BotPing`` ledger,
and the auto-draft helper all run for real.

Asserts:

*   one ``BotPing`` row per ``(target, action)`` pair, kind=``info``,
    status=``sent``;
*   a second call to the gate with the same ``(target, action)`` is
    idempotent on the DM (no second Slack ``post_message``, no second
    ``BotPing`` row).
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from teatree.config import OnBehalfPostMode
from teatree.core.models import BotPing
from teatree.core.on_behalf_gate_recorded import require_on_behalf_approval

pytestmark = pytest.mark.django_db


def _noop() -> None:
    return None


def _gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: OnBehalfPostMode) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f'[teatree]\nslack_user_id = "U-OPERATOR"\non_behalf_post_mode = "{mode.value}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


def _stub_backend() -> MagicMock:
    """A ``MessagingBackend``-shaped MagicMock that records ``post_message`` calls."""
    backend = MagicMock()
    backend.open_dm.return_value = "D-OPERATOR"
    # Real Slack ``chat.postMessage`` success carries ``ok:true`` AND a
    # ``ts``. ``notify_user`` hard-fails on a missing ``ok`` (#1048-class
    # phantom success), so the stub must mirror the real contract.
    backend.post_message.return_value = {"ok": True, "ts": "1700000000.0001"}
    backend.get_permalink.return_value = "https://slack.example/archives/D-OPERATOR/p1"
    return backend


class TestAutoDraftDmOnePerTargetAction:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_one_bot_ping_per_target_action(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.DRAFT_OR_ASK)
        backend = _stub_backend()
        # Point notify_user's backend resolver at our stub. Mocking at the
        # MessagingBackend boundary (open_dm + post_message) only — the
        # notify orchestration runs for real, including the BotPing ledger.
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)

        require_on_behalf_approval(target="org/repo!7", action="post_draft_note", publish=_noop)

        key = "on_behalf_autodraft:org/repo!7:post_draft_note"
        ping = BotPing.objects.get(idempotency_key=key)
        assert ping.kind == BotPing.Kind.INFO
        assert ping.status == BotPing.Status.SENT
        backend.post_message.assert_called_once()
        # The DM text names the publish/delete commands.
        sent_text: str = _post_message_text(backend.post_message.call_args)
        assert "publish-draft-notes" in sent_text
        assert "delete-draft-note" in sent_text

    def test_double_call_is_idempotent(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.DRAFT_OR_ASK)
        backend = _stub_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)

        require_on_behalf_approval(target="org/repo!7", action="post_draft_note", publish=_noop)
        require_on_behalf_approval(target="org/repo!7", action="post_draft_note", publish=_noop)

        key = "on_behalf_autodraft:org/repo!7:post_draft_note"
        assert BotPing.objects.filter(idempotency_key=key).count() == 1
        # The second call must NOT have hit Slack again.
        assert backend.post_message.call_count == 1

    def test_distinct_targets_get_distinct_pings(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.DRAFT_OR_ASK)
        backend = _stub_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)

        require_on_behalf_approval(target="org/repo!7", action="post_draft_note", publish=_noop)
        require_on_behalf_approval(target="org/repo!42", action="post_draft_note", publish=_noop)

        assert BotPing.objects.filter(idempotency_key__startswith="on_behalf_autodraft:").count() == 2
        assert backend.post_message.call_count == 2


def _post_message_text(call_args: Any) -> str:
    """Pull the ``text=`` kwarg out of a mock ``post_message`` call."""
    _, kwargs = call_args
    return str(kwargs.get("text", ""))
