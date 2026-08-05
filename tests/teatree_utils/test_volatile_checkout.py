"""A checkout may not be created where it dies with its session (#4194).

The measured incident: five worktrees holding unpushed work registered under
``~/.claude/jobs/<session>/tmp/**`` — the dead session's job dir. The predicate
here is the one every consumer shares, so the barrier that rescues such a
checkout, the seam that refuses to create one, and the doctor check that names a
registered one can never disagree about which paths are volatile.
"""

from pathlib import Path

import pytest

from teatree.utils.volatile_checkout import (
    VolatileCheckoutPathError,
    durable_checkout_root,
    resolve_checkout_base_dir,
    volatile_reason,
)


class TestVolatileReason:
    """Cases join onto ``tmp_path``: the predicate reads path COMPONENTS, so a literal root adds nothing."""

    @pytest.mark.parametrize(
        "relative",
        [
            ".claude/jobs/0e077e62/tmp/conflict-4122-4128/wt4122",
            ".claude/jobs/0e077e62/tmp/wt4101",
            ".claude/jobs/0e077e62/wt",
        ],
    )
    def test_names_a_job_dir_descendant(self, tmp_path: Path, relative: str) -> None:
        assert "job dir" in volatile_reason(tmp_path / relative)

    def test_names_a_harness_subagent_worktree(self, tmp_path: Path) -> None:
        assert "harness" in volatile_reason(tmp_path / ".claude/worktrees/agent-abc123")

    @pytest.mark.parametrize(
        "relative",
        [
            # The job dir itself is not a checkout — only its descendants are.
            ".claude/jobs/0e077e62",
            ".local/share/teatree-worktrees/wt-4194",
            "workspace/t3-workspaces/t3-teatree/4194-handover/teatree",
            # ``jobs`` not directly under ``.claude`` is somebody else's directory.
            "ci/jobs/12345/wt",
            ".claude/worktrees/not-an-agent-dir",
        ],
    )
    def test_durable_paths_have_no_reason(self, tmp_path: Path, relative: str) -> None:
        assert volatile_reason(tmp_path / relative) == ""


class TestResolveCheckoutBaseDir:
    def test_none_resolves_to_the_durable_root_not_the_system_temp_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dispatched agent's TMPDIR is routinely its own job scratch."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("TMPDIR", str(tmp_path / ".claude" / "jobs" / "sess" / "tmp"))

        resolved = Path(resolve_checkout_base_dir(None))

        assert resolved == durable_checkout_root()
        assert volatile_reason(resolved) == ""
        assert resolved.is_dir()

    def test_refuses_an_explicit_volatile_base_dir(self, tmp_path: Path) -> None:
        volatile = tmp_path / ".claude" / "jobs" / "0e077e62" / "tmp"

        with pytest.raises(VolatileCheckoutPathError) as exc:
            resolve_checkout_base_dir(str(volatile))

        assert str(volatile) in str(exc.value)
        assert "job dir" in str(exc.value)

    def test_keeps_an_explicit_durable_base_dir(self, tmp_path: Path) -> None:
        assert resolve_checkout_base_dir(str(tmp_path)) == str(tmp_path)

    def test_expands_a_tilde_base_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert resolve_checkout_base_dir("~/checkouts") == str(tmp_path / "checkouts")
