"""#1490 — ship/SKILL.md validator-reference doc-invariant guard.

The ship skill describes how MR/PR title+description linking is enforced.
It must name validators that actually exist: the overlay extension hook
``OverlayMetadata.validate_pr`` and its CLI surface ``t3 tool validate-mr``.

It previously named ``validate_mr_title_and_description`` — a symbol that
exists nowhere in ``src/`` or ``hooks/``, so an agent that greps for it
finds only the skill file and is left confused. This guard fails RED if
that stale name reappears, and asserts the real symbols are present both
in the skill prose and in the code it points at (anti-vacuous).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIP_SKILL = REPO_ROOT / "skills" / "ship" / "SKILL.md"
# ``validate_pr`` lives on ``OverlayMetadata``, which the #1983 "split by
# concern" extraction moved out of ``overlay.py`` into its own module. The
# guard points at the file that actually defines the hook, not the composer.
OVERLAY_METADATA_MODULE = REPO_ROOT / "src" / "teatree" / "core" / "overlay_metadata.py"
TOOLS_CLI = REPO_ROOT / "src" / "teatree" / "cli" / "tools.py"

_STALE_SYMBOL = "validate_mr_title_and_description"


class TestShipSkillNamesRealValidators:
    def test_does_not_name_nonexistent_core_validator(self) -> None:
        prose = SHIP_SKILL.read_text(encoding="utf-8")
        assert _STALE_SYMBOL not in prose, (
            f"ship/SKILL.md names {_STALE_SYMBOL!r}, which exists nowhere in "
            "src/ or hooks/. Reference validate_pr / `t3 tool validate-mr` instead."
        )

    def test_names_the_validate_pr_hook(self) -> None:
        prose = SHIP_SKILL.read_text(encoding="utf-8")
        assert "validate_pr" in prose

    def test_names_the_validate_mr_cli_surface(self) -> None:
        prose = SHIP_SKILL.read_text(encoding="utf-8")
        assert "validate-mr" in prose


class TestReferencedSymbolsExistInCode:
    def test_validate_pr_hook_exists_on_overlay_metadata(self) -> None:
        assert "def validate_pr(" in OVERLAY_METADATA_MODULE.read_text(encoding="utf-8")

    def test_validate_mr_cli_command_exists(self) -> None:
        assert 'command("validate-mr")' in TOOLS_CLI.read_text(encoding="utf-8")
