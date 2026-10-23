"""Shipped mailbox entry points stay discoverable in the public docs."""

from pathlib import Path

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
        assert "older Codex" in text
    assert "agent_mailbox_send" in interactive
    assert "agent_mailbox_wait" in interactive
