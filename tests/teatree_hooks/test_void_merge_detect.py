import pytest

from teatree.hooks.void_merge_detect import DENY_REASON, void_merge_deny_reason

_URL = "https://github.com/example-org/example-repo/pull/41"
_CALL = f"t3 teatree pr void-merge {_URL}"
_WRITE = f"{_CALL} --reason x --by alice"
_ARGV_TO_T3 = "import os,sys;a=sys.argv;os.execvp('t3',('t3','teatree',a.pop(1),a.pop(1),a.pop(1),'--by','alice'))"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(_WRITE, id="a-plain-write"),
        pytest.param(f"deploy/t3 teatree pr void-merge {_URL} --reason x --by alice --no-dry-run", id="unknown-flag"),
        pytest.param(f"cd /tmp && {_WRITE}", id="after-cd"),
        pytest.param(f"python manage.py pr void-merge {_URL} --reason x --by alice", id="through-manage-py"),
        pytest.param(f"echo ok; $({_WRITE})", id="in-a-substitution"),
        pytest.param(f"{_CALL} --reason --dry-run --by alice", id="dry-run-as-the-reason-value"),
        pytest.param(f"{_CALL} --reason x --by --help", id="help-as-the-by-value"),
        pytest.param(f"{_CALL} --reason x --by alice -- {_URL} --dry-run", id="dry-run-after-end-of-options"),
        pytest.param(
            f"t3 teatree pr --dry-run void-merge {_URL} --reason x --by alice", id="dry-run-before-void-merge"
        ),
        pytest.param(f"{_CALL} $R --dry-run --by alice", id="an-expansion-before-the-flag"),
        pytest.param(f"{_CALL} {{--reason,}} --dry-run --by alice", id="a-brace-list-before-the-flag"),
        pytest.param(f"{_CALL} --reason > /tmp/out --dry-run --by alice", id="a-redirection-before-the-flag"),
        pytest.param(f"echo --reason | xargs -I QQ {_CALL} QQ --dry-run --by alice", id="xargs-replace-before-flag"),
        pytest.param(f'python3 "-c{_ARGV_TO_T3}" pr void-merge {_URL} --dry-run', id="a-dry-run-handed-to-inline-code"),
        pytest.param(f"bash -c '{_WRITE}'", id="bash-c"),
        pytest.param(f'sh -c "{_WRITE}"', id="sh-c"),
        pytest.param(f"echo yes | script -qc '{_WRITE}' /dev/null", id="script-c-pty"),
        pytest.param(f"echo yes | unbuffer {_WRITE}", id="unbuffer-pty"),
        pytest.param(
            f"python3 -c \"import pty; pty.spawn(['t3', 'teatree', 'pr', 'void-merge', '{_URL}', '--by', 'alice'])\"",
            id="python-pty-spawn",
        ),
        pytest.param(f"env -u T3_DISPATCHED_AGENT {_WRITE}", id="env"),
        pytest.param(f"sudo -u alice {_WRITE}", id="sudo"),
        pytest.param(f"timeout 60 {_WRITE}", id="timeout"),
        pytest.param(f"echo {_URL} | xargs -I{{}} t3 teatree pr void-merge {{}} --reason x --by alice", id="xargs"),
        pytest.param(f"docker exec -it worker {_WRITE}", id="docker-exec"),
        pytest.param(f"ssh host '{_WRITE}'", id="ssh"),
        pytest.param(f"bash <<'EOF'\n{_WRITE}\nEOF", id="a-heredoc-an-inner-shell-reads"),
        pytest.param(f"X=void-merge; t3 teatree pr $X {_URL} --reason x --by alice", id="a-variable-holding-the-word"),
        pytest.param(
            f"bash <<'EOF'\n{_WRITE.replace('void-', "void''-")}\nEOF", id="a-quote-spliced-word-in-a-heredoc"
        ),
        pytest.param(
            f"bash <<'EOF'\n{_WRITE.replace('void-', 'void-\\\n')}\nEOF", id="a-line-continued-word-in-a-heredoc"
        ),
        pytest.param(f"t3 teatree pr void\\\n-merge {_URL} --reason x --by alice", id="a-line-continuation"),
        pytest.param(f"bash -c $'t3 teatree pr void\\x2dmerge {_URL} --reason x --by alice'", id="ansi-c-encoded"),
        pytest.param(
            f"t3 teatree pr $'\\x76oid-merge' {_URL} --dry-run\nbash <<'EOF'\n{_WRITE}\nEOF",
            id="a-decoded-dry-run-cannot-cover-a-literal-write",
        ),
        pytest.param(
            f"echo \"$(bash -c $'t3 teatree pr void\\x2dmerge {_URL} --reason x --by alice')\"",
            id="an-encoded-word-in-a-quoted-substitution",
        ),
        pytest.param(f"t3 $OVERLAY pr void-merge {_URL} --dry-run", id="an-expansion-before-pr"),
        pytest.param(f"grep -rn 'pr void-merge {_URL}' docs/", id="a-grep-mention"),
        pytest.param(f"echo '{_WRITE}' | bash", id="an-echo-piped-to-a-shell"),
        pytest.param(f"echo $({_CALL} --dry-run)", id="a-dry-run-inside-a-substitution"),
    ],
)
def test_a_command_naming_void_merge_is_refused_unless_it_is_a_standalone_read(command: str) -> None:
    assert void_merge_deny_reason(command) == DENY_REASON


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"{_CALL} --dry-run", id="a-dry-run"),
        pytest.param("t3 teatree pr void-merge --help", id="help"),
        pytest.param(f"cd /tmp && deploy/t3 teatree pr void-merge {_URL} --dry-run 2>&1 | tail -5", id="in-a-pipeline"),
        pytest.param(f"{_CALL} --reason 'history rewrite' --by alice --dry-run", id="the-flag-after-option-values"),
        pytest.param(f"{_CALL} --reason=--by --dry-run", id="a-glued-value-before-the-flag"),
        pytest.param("cd /work/5210-void-merge-transition/teatree && git status", id="a-branch-name-holding-the-word"),
        pytest.param("uv run pytest tests/test_void_merge_guard.py", id="the-python-module-name"),
        pytest.param("t3 teatree pr create 1844", id="another-pr-subcommand"),
        pytest.param("", id="empty"),
    ],
)
def test_a_standalone_read_and_unrelated_commands_pass(command: str) -> None:
    assert void_merge_deny_reason(command) is None
