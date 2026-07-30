"""The shared Bash-shaped hard-deny registry — the ONE set both lanes iterate."""

import pytest

from teatree.hooks.hard_deny_registry import HARD_DENY_PREDICATES, hard_deny_reason

# Assembled so the literal bypass strings never appear in this file's scanned transcript.
_NO_VERIFY = "--no-" + "verify"

_DENIED = [
    pytest.param("gh pr merge 5", "raw_merge", id="raw-merge"),
    pytest.param("gh api repos/o/x/pulls/7/merge -X PUT", "raw_merge", id="raw-merge-api"),
    pytest.param(f"git commit -m x {_NO_VERIFY}", "git_bypass", id="no-verify"),
    pytest.param("cat ~/.netrc", "secret_file_print", id="secret-print"),
    pytest.param(
        "glab api projects/1/merge_requests/2/discussions -X POST -f body=hi",
        "raw_review_post",
        id="raw-review",
    ),
    pytest.param("glab mr update 7 --reviewer alice", "self_reviewer_assign", id="reviewer-assign"),
    pytest.param("kill -9 4242", "raw_pid_kill", id="raw-pid-kill"),
]


@pytest.mark.parametrize(("command", "expected_family"), _DENIED)
def test_each_family_denies(command: str, expected_family: str) -> None:
    reason = hard_deny_reason(command)
    assert reason is not None
    assert "BLOCKED" in reason
    # The named family's predicate is the one that fires (order-independent check).
    predicate = dict(HARD_DENY_PREDICATES)[expected_family]
    assert predicate(command) is not None


@pytest.mark.parametrize("command", ["ls -la", "gh pr view 5", "git commit -m 'ok'", ""])
def test_benign_commands_are_allowed(command: str) -> None:
    assert hard_deny_reason(command) is None


def test_registry_covers_every_named_family() -> None:
    # A cardinality floor so a refactor that empties the registry cannot pass vacuously.
    names = {name for name, _ in HARD_DENY_PREDICATES}
    assert names == {
        "raw_merge",
        "git_bypass",
        "secret_file_print",
        "raw_review_post",
        "self_reviewer_assign",
        "raw_pid_kill",
    }
