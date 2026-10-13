import pytest

from teatree.hooks.void_merge_detect import DENY_REASON, void_merge_deny_reason

_URL = "https://github.com/example-org/example-repo/pull/41"
_CALL = f"t3 teatree pr void-merge {_URL}"
_WRITE = f"{_CALL} --reason x --by alice"
_ARGV_TO_T3 = "import os,sys;a=sys.argv;os.execvp('t3',('t3','teatree',a.pop(1),a.pop(1),a.pop(1),'--by','alice'))"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"t3 teatree $GROUP void-merge {_URL} --dry-run", id="the-pr-word-must-be-literal"),
        pytest.param(f"t3 $OVERLAY pr void-merge {_URL} --dry-run", id="the-overlay-word-must-be-literal"),
        pytest.param(f'python3 "-c{_ARGV_TO_T3}" pr void-merge {_URL} --dry-run', id="only-t3-runs-the-read"),
        pytest.param(f"{_CALL} --reason --dry-run --by alice", id="an-option-value-is-not-a-flag"),
        pytest.param(f"{_CALL} --reason x --by alice -- {_URL} --dry-run", id="end-of-options-stops-the-scan"),
        pytest.param(f"{_CALL} $R --dry-run --by alice", id="a-rewritten-word-before-the-flag"),
        pytest.param(f"bash <<'EOF'\n{_WRITE.replace('void-', "void''-")}\nEOF", id="a-quote-spliced-literal"),
        pytest.param(f"bash <<'EOF'\n{_WRITE.replace('void-', 'void-\\\n')}\nEOF", id="a-line-continued-literal"),
        pytest.param(f"bash -c $'t3 teatree pr void\\x2dmerge {_URL} --by alice'", id="a-shell-decoded-word"),
        pytest.param(
            f"echo \"$(bash -c $'t3 teatree pr void\\x2dmerge {_URL} --by alice')\"",
            id="a-decoded-word-in-a-substitution",
        ),
        pytest.param(
            f"t3 teatree pr $'\\x76oid-merge' {_URL} --dry-run\nbash <<'EOF'\n{_WRITE}\nEOF",
            id="a-decoded-read-cannot-cover-a-literal-write",
        ),
    ],
)
def test_a_command_naming_void_merge_is_refused_unless_it_is_a_standalone_read(command: str) -> None:
    assert void_merge_deny_reason(command) == DENY_REASON


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"cd /tmp && deploy/t3 teatree pr void-merge {_URL} --dry-run 2>&1 | tail -5", id="in-a-pipeline"),
        pytest.param("cd /work/5210-void-merge-transition/teatree && git status", id="a-branch-name-holding-the-word"),
        pytest.param("grep -rn 'void.merge' src/", id="a-dotted-search-pattern"),
        pytest.param("git commit -m 'fix(merge void): hold the restored ticket'", id="the-phrase-merge-void"),
    ],
)
def test_a_standalone_read_and_unrelated_commands_pass(command: str) -> None:
    assert void_merge_deny_reason(command) is None


@pytest.mark.parametrize("escape", ["the Grep tool", "`void.merge`", "`merge void`", "`git commit -F <message file>`"])
def test_the_refusal_names_the_escapes_that_run_no_void(escape: str) -> None:
    assert escape in DENY_REASON
