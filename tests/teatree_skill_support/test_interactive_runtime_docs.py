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


def test_interactive_skill_pins_the_factory_watch_duty() -> None:
    skills_dir = Path(__file__).resolve().parents[2] / "skills"
    text = (skills_dir / "interactive" / "SKILL.md").read_text()

    report = text.index("## A status field is a report")
    watch = text.index("## Factory watch — BLOCKING first (Non-Negotiable)")
    loading = text.index("## Skill Loading")
    assert report < watch < loading

    section = text[watch:loading]
    assert (skills_dir / "interactive" / "references" / "factory-watch.md").is_file()
    assert "references/factory-watch.md" in section
    reads = [
        "t3 worker status --json",
        "t3 <overlay> health show --json",
        "t3 loop self-improve status --limit 30",
        "t3 loop preset show",
        "t3 tokens --cached --json",
    ]
    positions = [section.index(read) for read in reads]
    assert positions == sorted(positions)
    assert "BLOCKING preempts the PR board and the todo drain" in section
    for flag in ("--no-reconcile", "--fail-on"):
        assert section.count(flag) == 1
        assert "#5069" in section[section.index(flag) :].split("\n", 1)[0]
