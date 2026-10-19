"""The resolver's terminal refusal names a reachable remedy.

``Cannot auto-detect worktree from <cwd>. Make sure you are running t3 from inside a
worktree directory.`` was accurate and dead-ended: the operator WAS inside a worktree
directory, and no ``t3`` verb existed to take them further. The message now names the
verb, and separates the one cause whose fix is entirely different — a checkout whose
gitdir this venue cannot see.
"""

from pathlib import Path

import pytest

from teatree.core.intake.worktree_refusal import unresolvable_worktree_message
from teatree.core.invocation_cwd import INVOCATION_CWD_ENV


def _linked(checkout: Path, gitdir: Path | str) -> Path:
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return checkout


class TestUnresolvableWorktreeMessage:
    def test_unset_declaration_is_named(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(INVOCATION_CWD_ENV, raising=False)

        message = unresolvable_worktree_message(str(tmp_path))

        assert f"{INVOCATION_CWD_ENV} is unset (not declared)" in message

    def test_unresolvable_declaration_is_quoted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        missing = tmp_path / "missing"
        monkeypatch.setenv(INVOCATION_CWD_ENV, str(missing))

        message = unresolvable_worktree_message(str(tmp_path))

        assert f"{INVOCATION_CWD_ENV}={str(missing)!r} is declared but does not resolve here" in message
        assert "is unset" not in message

    def test_valid_declaration_keeps_the_existing_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(INVOCATION_CWD_ENV, str(tmp_path))

        assert unresolvable_worktree_message(str(tmp_path)) == (
            f"Cannot auto-detect worktree from {tmp_path}.\n"
            "Make sure you are running t3 from inside a worktree directory.\n"
            "Register an existing checkout with `t3 <overlay> worktree adopt <path>` (it provisions nothing)."
        )

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
