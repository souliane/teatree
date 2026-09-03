"""The Bash write-target resolver shared by the plan gate and the main-clone guard (#4091/#4092).

Both gates keyed on the ``Edit``/``Write`` tool names, so every file written
through the shell reached neither. These tests pin what the shared resolver
classifies as a write — and, just as load-bearing, what it does NOT: a
read-only command contributes no target, and an unpinnable target is reported
as ``unresolved`` rather than guessed at, so each consumer can apply its own
posture (the main-clone guard allows, the plan gate warns).
"""

from pathlib import Path

import pytest

from teatree.hooks.write_targets import bash_write_targets


class TestRedirectTargets:
    def test_plain_redirect(self) -> None:
        assert bash_write_targets("echo hi > src/app/x.py").targets == ("src/app/x.py",)

    def test_append_and_unspaced_forms(self) -> None:
        assert bash_write_targets("echo hi >>src/x.py").targets == ("src/x.py",)

    def test_heredoc_to_file(self) -> None:
        command = "cat > src/app/models.py <<'EOF'\nclass A:\n    pass\nEOF\n"
        assert bash_write_targets(command).targets == ("src/app/models.py",)

    def test_fd_duplication_is_not_a_write_target(self) -> None:
        result = bash_write_targets("pytest -q tests/ 2>&1 | tail -20")
        assert result.targets == ()
        assert result.unresolved is False

    def test_substituted_target_is_unresolved_not_guessed(self) -> None:
        result = bash_write_targets("echo hi > $OUT/x.py")
        assert result.targets == ()
        assert result.unresolved is True


class TestInPlaceEditors:
    def test_sed_in_place(self) -> None:
        assert bash_write_targets("sed -i 's/old/new/' src/app/x.py").targets == ("src/app/x.py",)

    def test_sed_in_place_with_backup_suffix_and_expression_flag(self) -> None:
        command = "sed -i.bak -e 's/a/b/' src/a.py src/b.py"
        assert bash_write_targets(command).targets == ("src/a.py", "src/b.py")

    def test_sed_without_in_place_is_read_only(self) -> None:
        result = bash_write_targets("sed -n '1,20p' src/app/x.py")
        assert result.targets == ()
        assert result.unresolved is False

    def test_tee_operands_are_targets(self) -> None:
        assert bash_write_targets("printf 'x' | tee -a src/app/x.py").targets == ("src/app/x.py",)


class TestCopyMoveTargets:
    def test_cp_destination_only(self) -> None:
        assert bash_write_targets("cp /tmp/new.py src/app/x.py").targets == ("src/app/x.py",)

    def test_mv_destination_only(self) -> None:
        assert bash_write_targets("mv src/a.py src/b.py").targets == ("src/b.py",)

    def test_install_target_directory_flag(self) -> None:
        assert bash_write_targets("install -m 644 -t src/app /tmp/x.py").targets == ("src/app",)

    def test_git_mv_destination(self) -> None:
        assert bash_write_targets("git -C /repo mv src/a.py src/b.py").targets == ("src/b.py",)


class TestInterpreterHeredocs:
    def test_python_heredoc_literal_open_write(self) -> None:
        command = 'python3 - <<PY\nopen("src/app/x.py", "w").write("hi")\nPY\n'
        result = bash_write_targets(command)
        assert result.targets == ("src/app/x.py",)
        assert result.unresolved is False

    def test_python_heredoc_pathlib_write_text(self) -> None:
        command = "python3 - <<'PY'\nfrom pathlib import Path\nPath('src/app/y.py').write_text('hi')\nPY\n"
        assert bash_write_targets(command).targets == ("src/app/y.py",)

    def test_python_heredoc_write_through_a_variable_is_unresolved(self) -> None:
        command = 'python3 - <<PY\np = sys.argv[1]\nopen(p, "w").write("hi")\nPY\n'
        result = bash_write_targets(command)
        assert result.targets == ()
        assert result.unresolved is True

    def test_python_heredoc_read_only_is_not_a_write(self) -> None:
        command = 'python3 - <<PY\nprint(open("src/app/x.py").read())\nPY\n'
        result = bash_write_targets(command)
        assert result.targets == ()
        assert result.unresolved is False

    def test_python_dash_c_literal_write(self) -> None:
        command = 'python3 -c \'open("src/app/z.py", "w").write("hi")\''
        assert bash_write_targets(command).targets == ("src/app/z.py",)

    def test_shell_heredoc_body_is_classified_recursively(self) -> None:
        command = "bash <<SH\nsed -i 's/a/b/' src/app/x.py\nSH\n"
        assert bash_write_targets(command).targets == ("src/app/x.py",)


class TestQuotedRedirectCharactersAreArguments:
    """A `>` inside quotes is an ARGUMENT, never a redirect operator.

    Redirection is shell SYNTAX, so it must be recognised on the verbatim source
    span (``Token.raw``), not on the quote-decoded value. Reading the decoded
    value turned `grep -rn '>' src/` into "writes src/" — the whole source tree —
    and both gates denied it. Everyday commands; the guards became unusable.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "grep -rn '>' src/",
            'rg -n ">>" src/teatree',
            'git commit -m "> blockquote note"',
            "grep '>>>' src/app/x.py",
            'gh pr comment 1 --body "> quoted reply"',
            'printf "%s" ">"',
            "grep -n '<' src/app/x.py",
        ],
    )
    def test_quoted_redirect_char_is_not_a_write(self, command: str) -> None:
        result = bash_write_targets(command)
        assert result.targets == ()
        assert result.unresolved is False

    def test_a_real_redirect_alongside_a_quoted_one_still_counts(self) -> None:
        # The anti-vacuous companion: quoting disarms only the QUOTED `>`.
        assert bash_write_targets("grep -rn '>' src/ > /tmp/hits.txt").targets == ("/tmp/hits.txt",)


class TestProcessSubstitutionIsNotAnOutputRedirect:
    """`>(...)` is a process substitution, not a `> path` redirect (#4127).

    ``_REDIRECT_RE`` matched its leading `>` and emitted the rest of the split
    token as a literal relative path — `tee >(gzip -c > /tmp/a.gz)` resolved to
    `(gzip` and `/tmp/a.gz)`, so both gates denied a legitimate command while
    naming a path that does not exist. The honest answer is `unresolved`: bash
    expands the substitution to a `/dev/fd/N` at run time, so what the inner
    command writes is not statically knowable — the same posture `echo hi > $OUT`
    already gets.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "cat in.txt | tee >(gzip -c > /tmp/a.gz)",
            "make 2> >(tee /tmp/err.log)",
            "tee >(gzip)",
        ],
    )
    def test_output_substitution_is_unresolved_not_a_literal_path(self, command: str) -> None:
        result = bash_write_targets(command)
        assert result.targets == ()
        assert result.unresolved is True

    def test_input_substitution_is_not_a_write_at_all(self) -> None:
        result = bash_write_targets("diff <(sort a) <(sort b)")
        assert result.targets == ()
        assert result.unresolved is False

    def test_a_real_redirect_sharing_the_line_is_still_resolved(self) -> None:
        # The anti-vacuous companion: reporting the substitution unresolved must
        # not blind the resolver to a genuine `> path` on the same line.
        result = bash_write_targets("make 2> >(tee /tmp/err.log) > src/app/out.txt")
        assert result.targets == ("src/app/out.txt",)
        assert result.unresolved is True


class TestLeaderPrefixesAreSkipped:
    """`command mv` is the shell-alias-safe spelling the house rules mandate."""

    @pytest.mark.parametrize("prefix", ["command", "env", "nohup", "time"])
    def test_prefixed_writer_is_still_a_write(self, prefix: str) -> None:
        assert bash_write_targets(f"{prefix} cp /tmp/new.py src/app/x.py").targets == ("src/app/x.py",)


class TestReadOnlyCommandsAreNeverWrites:
    def test_grep_and_cat_contribute_nothing(self) -> None:
        result = bash_write_targets("grep -rn 'needle' src/ && cat src/app/x.py")
        assert result.targets == ()
        assert result.unresolved is False

    def test_bsd_sed_empty_suffix_does_not_leak_the_script_as_a_target(self) -> None:
        assert bash_write_targets("sed -i '' 's/a/b/' src/app/x.py").targets == ("src/app/x.py",)

    def test_git_status_and_log_contribute_nothing(self) -> None:
        result = bash_write_targets("git status --short && git log --oneline -5")
        assert result.targets == ()
        assert result.unresolved is False


class TestResolvedPaths:
    def test_relative_target_is_anchored_to_the_base(self) -> None:
        result = bash_write_targets("echo hi > src/x.py")
        assert result.resolved_paths(Path("/repo")) == (Path("/repo/src/x.py"),)

    def test_absolute_target_ignores_the_base(self) -> None:
        result = bash_write_targets("echo hi > /tmp/scratch.py")
        assert result.resolved_paths(Path("/repo")) == (Path("/tmp/scratch.py"),)

    def test_relative_target_without_a_base_resolves_to_nothing(self) -> None:
        assert bash_write_targets("echo hi > src/x.py").resolved_paths(None) == ()


class TestInterpreterHeredocWithAPunctuatedDelimiter:
    r"""A heredoc delimiter is a shell WORD, not an identifier.

    The delimiter grammar was ``\\w+``, so ``<<'PY-1'`` paired with no body at
    all — and the segment then reported NO targets AND NO unresolved target,
    which both write gates read as "this command writes nothing". A write gate
    that cannot see a write is the failure; not being able to PIN the path is a
    different, honest answer the module already has a word for.
    """

    def test_a_punctuated_delimiter_still_yields_its_literal_target(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        command = f"python3 - <<'PY-1'\nopen({str(target)!r}, 'w').write('x')\nPY-1\n"
        result = bash_write_targets(command)
        assert str(target) in result.targets

    def test_an_unreadable_interpreter_heredoc_body_is_reported_unresolved(self) -> None:
        # The delimiter opens a body this module cannot pair (a mismatched
        # terminator). The command demonstrably feeds an interpreter a script,
        # so "writes nothing" is the one answer it must not give.
        command = "python3 - <<'PY_BODY'\nopen('/tmp/x', 'w').write('x')\nPY_OTHER\n"
        result = bash_write_targets(command)
        assert result.targets == ()
        assert result.unresolved is True
        assert result.writes_something is True

    def test_a_plain_command_with_no_heredoc_is_untouched(self) -> None:
        assert bash_write_targets("git status").writes_something is False
