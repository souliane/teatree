"""The interactive contract is shared across supported terminal agents."""

from pathlib import Path


def test_interactive_skill_describes_claude_and_codex_contract() -> None:
    skills_dir = Path(__file__).resolve().parents[2] / "skills"
    text = (skills_dir / "interactive" / "SKILL.md").read_text()

    assert "Claude Code and Codex" in text
    assert "The Claude Code side of teatree" not in text
    assert "Claude-only hook automation" in text


def test_blueprint_distinguishes_shared_terminal_agents_from_claude_hooks() -> None:
    blueprint = Path(__file__).resolve().parents[2] / "BLUEPRINT.md"
    text = blueprint.read_text()

    assert "Claude Code or Codex" in text
    assert "long-lived interactive Claude Code session" not in text
    assert "driven through Claude Code's interactive terminal app today" not in text
