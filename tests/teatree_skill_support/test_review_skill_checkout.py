from pathlib import Path

_REVIEW_SKILL = Path(__file__).resolve().parents[2] / "skills" / "review" / "SKILL.md"


def test_cold_review_checkout_uses_verified_cli_route() -> None:
    text = _REVIEW_SKILL.read_text(encoding="utf-8")
    cold_checkout = [line for line in text.splitlines() if line.startswith("**Cold-review checkout")]

    assert any(
        "`t3 review checkout <pr-url> --sha <head>`" in line and "never `git worktree add <branch>`" in line
        for line in cold_checkout
    )
