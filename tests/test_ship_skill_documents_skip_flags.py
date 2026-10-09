"""ship/SKILL.md must document the bypass flags the `pr create` contract exposes.

#1486 split the cheap MR title/description format check off from
``--skip-validation``: the latter now skips only the heavy gates and STILL
runs the format check, while the new explicit ``--skip-mr-format-check``
is required to disable the format check too (``pr.py`` create signature +
docstring). The ship skill §4b must mention both, so an agent reading the
skill knows the format check survives ``--skip-validation`` and which flag
to reach for in the rare non-canonical-title case.

Doc-invariant guard, in the spirit of ``test_skill_t3_invocations``: catch
a skill that drifts out of sync with the live CLI flag contract on every CI
run instead of misleading an agent at runtime. Per ``/t3:code`` § 5d, the
relationship assertion scans every occurrence of the anchor token rather
than keying on the first match.
"""

from pathlib import Path

_SHIP_SKILL = Path(__file__).resolve().parents[1] / "skills" / "ship" / "SKILL.md"
_PR_COMMAND = Path(__file__).resolve().parents[1] / "src" / "teatree" / "core" / "management" / "commands" / "pr.py"


def _any_window_contains(text: str, anchor: str, *, must_include: str, radius: int) -> bool:
    """True iff some ``anchor`` occurrence has ``must_include`` within ``radius`` chars.

    Scans all occurrences (``/t3:code`` § 5d): an anchor token recurs across a
    doc section, so the first match is not authoritative.
    """
    start = 0
    while (idx := text.find(anchor, start)) != -1:
        window = text[max(0, idx - radius) : idx + len(anchor) + radius]
        if must_include in window:
            return True
        start = idx + 1
    return False


class TestShipSkillDocumentsSkipFlags:
    def test_skill_documents_skip_mr_format_check_flag(self) -> None:
        text = _SHIP_SKILL.read_text(encoding="utf-8")
        assert "--skip-mr-format-check" in text, (
            "ship/SKILL.md must document the --skip-mr-format-check flag introduced "
            "by #1486 (the explicit opt-in that disables the cheap MR format check)."
        )

    def test_skill_states_skip_validation_keeps_the_format_check(self) -> None:
        text = _SHIP_SKILL.read_text(encoding="utf-8")
        assert _any_window_contains(
            text,
            "--skip-mr-format-check",
            must_include="--skip-validation",
            radius=400,
        ), (
            "ship/SKILL.md §4b must explain the relationship: --skip-validation keeps "
            "the cheap MR title/description format check, and --skip-mr-format-check is "
            "the separate explicit opt-in that disables it too."
        )

    def test_documented_flag_matches_pr_command_contract(self) -> None:
        """The skill must not document a flag the CLI does not expose."""
        skill_text = _SHIP_SKILL.read_text(encoding="utf-8")
        command_text = _PR_COMMAND.read_text(encoding="utf-8")
        if "--skip-mr-format-check" in skill_text:
            assert "skip_mr_format_check" in command_text, (
                "ship/SKILL.md documents --skip-mr-format-check but the pr create "
                "command no longer exposes the skip_mr_format_check parameter — stale "
                "reference (CLAUDE.md § 'No stale references')."
            )
