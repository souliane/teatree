"""Doc-invariant guard against a closed bughunt scanner inventory.

The ``teatree-bughunt`` skill's whole job is to find missing-signal /
broken-scanner bugs. Its Reference section used to enumerate a *closed*
six-item scanner list (``pending_tasks, my_prs, reviewer_prs,
assigned_issues, slack_mentions, notion_view`` + "The first five run
per-overlay …") while ``src/teatree/loop/scanners/`` actually holds ~30
modules. A hunting agent reading the closed list as authoritative
mis-judges what is wired — filing false "scanner missing" bugs or
skipping real scanners (it would not know ``resource.*`` / ``task.*``
signals or the ``free_resources`` / ``task_completion`` handlers exist).
#1478 widened the gap by adding ``resource_pressure`` and ``task_sweep``.

This guard makes "the skill points at the live directory, never an
exhaustive inline list" *mechanically enforced* rather than
prose-vigilance: it fails RED if the skill reintroduces the closed-list
phrasing, drops the source-of-truth pointer, or omits a scanner family
sibling that is actually present in the scanners package.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = REPO_ROOT / "skills" / "teatree-bughunt" / "SKILL.md"
SCANNERS_DIR = REPO_ROOT / "src" / "teatree" / "loop" / "scanners"

# Module stems that are infrastructure, not a scanner the loop fans out.
_NON_SCANNER_STEMS = {"__init__", "base"}


def _live_scanner_stems() -> set[str]:
    """The set of scanner module names actually present in the package."""
    return {p.stem for p in SCANNERS_DIR.glob("*.py") if p.stem not in _NON_SCANNER_STEMS}


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


class TestScannersPackageShape:
    def test_package_holds_many_more_than_six_scanners(self) -> None:
        # The premise of the guard: the live package is far bigger than the
        # retired six-item list, so any closed inline enumeration is stale.
        assert len(_live_scanner_stems()) > 6

    def test_1478_pair_present_in_package(self) -> None:
        stems = _live_scanner_stems()
        assert "resource_pressure" in stems
        assert "task_sweep" in stems


class TestSkillPointsAtLiveDirectory:
    def test_names_the_scanners_package_as_source_of_truth(self, skill_text: str) -> None:
        # The reference must direct the reader to the live package, not a
        # frozen inline roster.
        assert "src/teatree/loop/scanners/" in skill_text
        lowered = skill_text.lower()
        assert "source of truth" in lowered
        assert "read it for the current set" in lowered or "read the directory" in lowered

    def test_no_closed_six_item_inventory_phrasing(self, skill_text: str) -> None:
        # The exact closure that made the old list read as exhaustive.
        assert "The first five run per-overlay" not in skill_text

    def test_examples_are_flagged_non_exhaustive(self, skill_text: str) -> None:
        # If the skill keeps example scanner names, they must be flagged as a
        # non-exhaustive sample so no reader treats them as the full set.
        assert "non-exhaustive" in skill_text.lower()


class TestNewerFamilySiblingsCited:
    """A reader must know the newer scanner families exist.

    This is the drift this guard exists for: the skill must name the
    #1478 pair and at least the earlier global/cadence-gated siblings as
    examples.
    """

    @pytest.mark.parametrize(
        "scanner",
        ["resource_pressure", "task_sweep", "pr_sweep", "self_update", "codex_review"],
    )
    def test_family_sibling_named(self, skill_text: str, scanner: str) -> None:
        assert scanner in skill_text, f"{scanner} scanner missing from the bughunt reference"

    def test_mechanical_handlers_for_new_signals_named(self, skill_text: str) -> None:
        # The handlers a hunting agent would otherwise not know exist.
        assert "free_resources" in skill_text
        assert "task_completion" in skill_text
