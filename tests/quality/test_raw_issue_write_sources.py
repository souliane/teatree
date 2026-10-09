# test-path: cross-cutting — the sibling of test_chokepoints.py, scanning tracked sources
"""Green-on-tree ledger: no tracked workflow, script or skill invokes a raw issue write (#162).

The sibling of ``test_chokepoints.py``. That gate asks the question of ``src/``
call sites; this one asks it of the sources a hook cannot reach — a CI step, a
checked-in script, a skill teaching the wrong command. The green assertion is what
makes the scan a gate rather than a report, and the anti-vacuous pair proves it
still bites: a real invocation turns it red, and a documented counter-example does
not.
"""

from pathlib import Path

from teatree.hooks.raw_issue_write_sources import scan_paths, scan_text
from tests.quality._anchor_prose import tracked_files

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _tracked() -> list[str]:
    return [str(path.relative_to(_REPO_ROOT)) for path in tracked_files(repo_root=_REPO_ROOT)]


class TestTreeIsClean:
    def test_no_tracked_source_invokes_a_raw_forge_issue_write(self) -> None:
        found = scan_paths(_tracked(), root=_REPO_ROOT)
        assert not found, "route these through teatree.core.issue_hygiene:\n" + "\n".join(str(f) for f in found)


class TestTheGateBites:
    def test_a_workflow_step_filing_an_issue_is_reported(self) -> None:
        text = "      - run: |\n          gh issue create --title t --body b --label needs-triage\n"
        assert [f.verb for f in scan_text(text, path=".github/workflows/ci.yml")] == ["create"]

    def test_a_skill_teaching_a_raw_note_is_reported(self) -> None:
        assert [f.verb for f in scan_text("glab issue note 7 -m hi\n", path="skills/x/SKILL.md")] == ["note"]

    def test_a_documented_counter_example_is_not_reported(self) -> None:
        text = "gh issue create --title t   # FORBIDDEN — call the facade\n"
        assert scan_text(text, path="skills/x/SKILL.md") == []
