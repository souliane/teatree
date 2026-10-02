"""The reviewer brief runs codex as the second reviewer and never hides its absence (#159)."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIEF = (REPO_ROOT / "agents" / "reviewer.md").read_text(encoding="utf-8")


def test_brief_names_the_codex_runner_by_path() -> None:
    # Not a frontmatter skill: the CI-portable ref validator resolves agents against
    # the repo's own ``skills/`` only, and codex-review is a harness-installed skill.
    assert "skills/codex-review/scripts/codex-elite-review" in BRIEF


def test_brief_runs_the_codex_runner_and_merges_its_findings() -> None:
    assert "codex-elite-review" in BRIEF
    assert "codex:" in BRIEF


def test_brief_records_an_unavailable_codex_as_a_finding() -> None:
    assert "codex second review unavailable" in BRIEF


def test_brief_never_approves() -> None:
    assert "never approve" in BRIEF.lower()
