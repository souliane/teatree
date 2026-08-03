# test-path: cross-cutting — asserts .gitignore covers what worktree provisioning writes; no single src/teatree/ mirror.
"""Provisioning must not leave a path that escalates the diff-scoped lane to FULL (#3994).

``t3 tool affected-tests`` is fail-safe TO FULL: any changed path it cannot classify runs
the whole suite. That doctrine is right, and it is why an UNIGNORED artifact is so
expensive — ``worktree provision`` writes ``.envrc`` into every worktree it creates, so
while that path was untracked and unignored the selector saw an unclassifiable change on
*every* ticket in *every* provisioned worktree. The scoped lane then ran ~34k tests
whatever the diff touched, which is exactly the whole-suite cost #3994 exists to remove.

Asserted against ``git check-ignore``, not against ``.gitignore``'s text: negations and
later patterns mean a literal grep can read as covered while git disagrees.
"""

from pathlib import Path

import pytest

from teatree.utils.run import run_allowed_to_fail

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Paths teatree's own provisioning writes into a worktree checkout. ``.envrc`` comes from
#: ``teatree.core.runners.worktree_provision._append_envrc_lines``; the env cache and its
#: cache dir from ``worktree start``.
PROVISIONING_ARTIFACTS: tuple[str, ...] = (".envrc", ".t3-env.cache", ".t3-cache/")

#: Anti-vacuity control: a tracked file must NOT be ignored. Without it a broken
#: ``git check-ignore`` invocation (wrong cwd, bad flag) would read as every path covered.
TRACKED_CONTROL = "pyproject.toml"


def _is_git_ignored(path: str) -> bool:
    # --no-index so the answer is the ignore rules alone; a tracked path is otherwise
    # reported un-ignored regardless of what .gitignore says, which would hide a regression.
    result = run_allowed_to_fail(
        ["git", "check-ignore", "--no-index", "-q", "--", path],
        expected_codes=None,
        cwd=_REPO_ROOT,
    )
    return result.returncode == 0


class TestProvisioningArtifactsAreIgnored:
    @pytest.mark.parametrize("artifact", PROVISIONING_ARTIFACTS)
    def test_provisioning_artifact_is_git_ignored(self, artifact: str) -> None:
        assert _is_git_ignored(artifact), (
            f"`worktree provision`/`start` writes {artifact} into every worktree, but it is not "
            "git-ignored -- the affected-tests lane sees an unclassifiable changed path and "
            "escalates to the FULL suite on every ticket (#3994). Add it to .gitignore."
        )

    def test_control_a_tracked_file_is_not_ignored(self) -> None:
        assert not _is_git_ignored(TRACKED_CONTROL), (
            "the ignore probe reports a tracked file as ignored -- it cannot distinguish "
            "covered from broken, so the assertions above prove nothing"
        )
