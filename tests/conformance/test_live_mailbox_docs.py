"""Shipped mailbox entry points stay discoverable in the public docs."""

from pathlib import Path

from teatree.agents.live_mailbox_client import MailboxClient
from teatree.mcp.agent_mailbox import INSTRUCTIONS

_ROOT = Path(__file__).resolve().parents[2]


def test_attended_join_and_codex_outbound_are_described() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    blueprint = (_ROOT / "BLUEPRINT.md").read_text(encoding="utf-8")
    interactive = (_ROOT / "skills" / "interactive" / "SKILL.md").read_text(encoding="utf-8")
    for text in (readme, blueprint):
        assert "mcp serve --interactive" in text
        assert "dynamicTools" in text
        assert "accepted_inbox_read" in text
        assert "no live worker" in text
        assert "creation-time tool catalog" in text
    assert "agent_mailbox_send" in interactive
    assert "agent_mailbox_wait" in interactive


def test_codex_mailbox_descriptions_include_the_worker_inbox() -> None:
    codex = (_ROOT / "docs" / "codex-app-server.md").read_text(encoding="utf-8")
    descriptions = (codex, INSTRUCTIONS, MailboxClient.send.__doc__ or "")
    assert all("push-only peer has no inbox copy" not in text for text in descriptions)
    assert "worker retains their inbox" in codex
    assert "inbox" in INSTRUCTIONS
    assert "inbox" in (MailboxClient.send.__doc__ or "")
