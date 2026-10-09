"""The skills must cover the SHARED-checkout half of the prek stash.

ship/SKILL.md already explains that the gate commits the INDEX and that an edit made
after ``git add`` does not ship — a correctness problem for the committer alone. On a
checkout several agents write to, the same stash is a DATA-LOSS problem: prek stashes
every unstaged change including another writer's, so a run killed between the stash and
the restore leaves the tree without them, and the ``git commit -a`` the section
recommends commits their half-finished work under someone else's message.

There is nothing to gate here — the section's own reasoning holds: a hook runs AFTER the
stash, so the information is gone before any hook could look. What was missing is the
written rule, and these are its doc-invariant guards, in the shape of
``test_ship_skill_documents_skip_flags``. Per ``/t3:code`` § 5d each relationship
assertion scans every occurrence of its anchor rather than keying on the first.
"""

from pathlib import Path

_SKILLS = Path(__file__).resolve().parents[1] / "skills"
_SHIP_SKILL = _SKILLS / "ship" / "SKILL.md"
_RULES_SKILL = _SKILLS / "rules" / "SKILL.md"

_PATCH_DIR = "~/.cache/prek/patches"
_SCOPED_COMMIT = "git commit -o"


def _any_window_contains(text: str, anchor: str, *, must_include: str, radius: int) -> bool:
    """True iff some ``anchor`` occurrence has ``must_include`` within ``radius`` chars."""
    start = 0
    while (idx := text.find(anchor, start)) != -1:
        window = text[max(0, idx - radius) : idx + len(anchor) + radius]
        if must_include in window:
            return True
        start = idx + 1
    return False


class TestShipSkillDocumentsTheSharedCheckoutDiscipline:
    def test_the_commit_a_recommendation_is_scoped_to_a_sole_writer(self) -> None:
        assert _any_window_contains(
            _SHIP_SKILL.read_text(encoding="utf-8"),
            "git commit -a",
            must_include="SHARED checkout",
            radius=600,
        ), (
            "ship/SKILL.md recommends `git commit -a` to avoid the index/worktree split, which on a "
            "shared checkout commits another writer's half-finished edits. The recommendation must "
            "name the sole-writer scope it holds for."
        )

    def test_a_shared_checkout_is_told_to_scope_the_commit_to_its_own_paths(self) -> None:
        assert _any_window_contains(
            _SHIP_SKILL.read_text(encoding="utf-8"),
            "SHARED checkout",
            must_include=_SCOPED_COMMIT,
            radius=900,
        ), f"ship/SKILL.md must give the shared-checkout recipe: `{_SCOPED_COMMIT} -- <your paths only>`."

    def test_the_patch_directory_is_named_as_the_recovery_path(self) -> None:
        assert _any_window_contains(
            _SHIP_SKILL.read_text(encoding="utf-8"),
            _PATCH_DIR,
            must_include="git apply",
            radius=400,
        ), (
            f"ship/SKILL.md names {_PATCH_DIR} as where prek saves a stash, but never as how to get "
            "one back — a stash a killed run never restored is recoverable only from there."
        )


class TestTheConcurrencyRuleSaysWhenToStage:
    def test_staging_timing_and_a_scoped_commit_sit_with_the_concurrency_rule(self) -> None:
        assert _any_window_contains(
            _RULES_SKILL.read_text(encoding="utf-8"),
            "## Concurrent Agent Safety",
            must_include=_SCOPED_COMMIT,
            radius=1200,
        ), (
            "rules/SKILL.md § Concurrent Agent Safety says WHICH files to commit but not WHEN to "
            "stage them. Staging each edit immediately is what keeps it out of a stash a killed hook "
            f"run may never restore, and `{_SCOPED_COMMIT}` is what keeps the other writer's out of "
            "the commit."
        )
