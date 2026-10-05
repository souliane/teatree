"""The resolver's terminal refusal names a reachable remedy.

``Cannot auto-detect worktree from <cwd>. Make sure you are running t3 from inside a
worktree directory.`` was accurate and dead-ended: the operator WAS inside a worktree
directory, and no ``t3`` verb existed to take them further. The message now names the
verb, and separates the one cause whose fix is entirely different — a checkout whose
gitdir this venue cannot see.
"""

from pathlib import Path

from teatree.core.intake.worktree_refusal import unresolvable_worktree_message


def _linked(checkout: Path, gitdir: Path | str) -> Path:
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return checkout


class TestUnresolvableWorktreeMessage:
    def test_names_the_cwd(self, tmp_path: Path) -> None:
        assert str(tmp_path) in unresolvable_worktree_message(str(tmp_path))

    def test_names_the_adopt_verb_as_the_remedy(self, tmp_path: Path) -> None:
        assert "worktree adopt" in unresolvable_worktree_message(str(tmp_path))

    def test_keeps_the_auto_detect_wording_consumers_match_on(self, tmp_path: Path) -> None:
        assert "Cannot auto-detect worktree" in unresolvable_worktree_message(str(tmp_path))

    def test_an_unreachable_gitdir_is_named_instead_of_the_generic_advice(self, tmp_path: Path) -> None:
        checkout = _linked(tmp_path / "wt", "/elsewhere/backend/.git/worktrees/wt")

        message = unresolvable_worktree_message(str(checkout))

        assert "/elsewhere/backend/.git/worktrees/wt" in message
        # The generic "stand inside a worktree" advice is WRONG here — they are.
        assert "running t3 from inside a worktree" not in message

    def test_a_main_clone_is_named_as_a_clone(self, tmp_path: Path) -> None:
        clone = tmp_path / "clone"
        (clone / ".git").mkdir(parents=True)

        assert "main clone" in unresolvable_worktree_message(str(clone))
