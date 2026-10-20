# test-path: cross-cutting
# Pins the prose of skills/interactive, skills/rules and BLUEPRINT.md against the commands and markers the code ships.
"""The Slack-first contract is documented where an attended session reads it, and names real seams."""

from pathlib import Path

from teatree.hooks.slack_first_scanner import RELAY_RECEIPT_MARKER, RELAY_REFUSED_MARKER

_ROOT = Path(__file__).resolve().parents[2]
_HEADING = "## Slack-first while factory Slack is live (Non-Negotiable)"


def _interactive_section() -> str:
    text = (_ROOT / "skills" / "interactive" / "SKILL.md").read_text(encoding="utf-8")
    start = text.index(_HEADING)
    return text[start : text.index("\n## ", start + 1)]


def test_the_interactive_skill_carries_the_slack_first_contract() -> None:
    section = _interactive_section()

    for needle in (
        "t3 <overlay> pending_chat relay",
        "mcp__teatree__pending_chat_relay",
        "pending_chat_relay",
        "t3 <overlay> pending_chat relays",
        RELAY_RECEIPT_MARKER,
        RELAY_REFUSED_MARKER,
        "Exit 3 or 4",
        "t3 <overlay> live steer",
        "notify post --thread-ts",
        "[skip-slack-first: <reason>]",
        "Codex has no deterministic backstop",
    ):
        assert needle in section, needle


def test_factory_watch_names_the_relays_read() -> None:
    text = (_ROOT / "skills" / "interactive" / "references" / "factory-watch.md").read_text(encoding="utf-8")

    assert "t3 <overlay> pending_chat relays" in text


def test_the_rules_say_a_relay_receipt_is_the_answer() -> None:
    rules = (_ROOT / "skills" / "rules" / "SKILL.md").read_text(encoding="utf-8")
    reference = (_ROOT / "skills" / "rules" / "references" / "asking-questions.md").read_text(encoding="utf-8")

    assert RELAY_RECEIPT_MARKER in rules
    assert RELAY_RECEIPT_MARKER in reference
    assert "relay_receipt=True" in reference


def test_the_blueprint_names_the_relay_as_the_second_pending_chat_writer() -> None:
    blueprint = (_ROOT / "BLUEPRINT.md").read_text(encoding="utf-8")

    assert "second `PendingChatInjection` writer" in blueprint
    assert "`bind_reply` returns nothing for it" in blueprint
    assert "slack_first_question" in blueprint
