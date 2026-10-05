"""A raw `gh issue comment` / `glab issue note` is denied at the Bash boundary (#162).

`teatree.core.issue_hygiene` is the single issue-write facade: it decides whether a
note belongs in the description or a comment, refuses a ticket the owner or factory
bot did not file, and scrubs every outbound through the public-repo leak gate. A
raw `gh issue comment` typed into Bash reaches the same forge endpoint and answers
none of those questions — which is how a requirement lands in a comment no lane
will ever read.

The sibling REST gate (`raw_review_post_detect`) already covers `gh api`/`glab api`
writes to an issue-notes endpoint. This closes the CLI half of the SAME surface.

The inverse controls carry the weight. "Denies a note" is satisfied by a substring
match; what a regression would break is everything the gate must NOT touch — a
read, a label-only edit, the sweep's own `gh issue edit --body-file` fold, a
`gh issue create`, and a read-only tool that merely QUOTES the write spelling. An
over-blocking gate wedges a session doing nothing wrong.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.raw_issue_write_guard as guard
from teatree.hooks.hard_deny_registry import hard_deny_reason
from teatree.hooks.raw_issue_write_detect import is_raw_issue_write, raw_issue_write_deny_reason

#: The over-block matrix, one row per case: ``(id, command, refused, named)`` — ``named`` is the word a refusal
#: must name when only the shell decides the subcommand or verb. ONE table, read by both the detector path and
#: the detector-crash fallback, so the two can never drift apart.
MATRIX: tuple[tuple[str, str, bool, str], ...] = (
    ("A1", "gh pr view 12", False, ""),
    ("A2", "gh pr view $N", False, ""),
    ("A3", "gh pr list", False, ""),
    ("A4", "gh pr create --title t --body-file f.md", False, ""),
    ("A5", "gh -R o/r pr view 12", False, ""),
    ("A6", "gh pr view 12 -R o/r", False, ""),
    ("A7", "gh --repo o/r pr list", False, ""),
    ("A8", "gh --repo=o/r pr list", False, ""),
    ("A9", 'gh --repo="$R" pr view 12', False, ""),
    ("A10", 'gh -R"$R" pr list', False, ""),
    ("A11", 'gh -R "$R" pr view 12', False, ""),
    ("A12", 'glab --repo="$R" mr view 3', False, ""),
    ("A13", "glab -R o/r mr view $IID", False, ""),
    ("A14", "glab mr view $IID", False, ""),
    ("A15", "GH_HOST=github.com gh pr view 12", False, ""),
    ("A16", "GH_TOKEN=x gh pr list", False, ""),
    ("A17", "env GH_HOST=x gh pr view 12", False, ""),
    ("A18", "gh issue view 12", False, ""),
    ("A19", "gh issue list", False, ""),
    ("A20", "gh -R o/r issue view 12", False, ""),
    ("A21", 'gh issue view 12 -R "$R"', False, ""),
    ("A22", 'gh api "repos/$R/issues/12"', False, ""),
    ("A23", "gh pr view 12 --json body > ./issue-notes.md", False, ""),
    ("A24", "gh pr create --body-file ./docs/issue.md", False, ""),
    ("A25", 'gh pr comment 12 --body "see issue 5"', False, ""),
    ("A26-ls", "ls $HOME", False, ""),
    ("A26-echo", "echo issue comment", False, ""),
    ("A26-grep", 'grep -rn "gh issue comment" docs/', False, ""),
    ("A27-view", "glab issue view 3", False, ""),
    ("A27-list", "glab issue list", False, ""),
    ("P1-gh-create", "gh issue create --title t --body-file f.md", False, ""),
    ("P1-gh-edit", "gh issue edit 12 --add-label x", False, ""),
    ("P1-gh-close", "gh issue close 12", False, ""),
    ("P1-glab-create", "glab issue create -t t", False, ""),
    ("P1-glab-update", "glab issue update 3 -l x", False, ""),
    ("P1-fold", "gh issue edit $N --body-file merged.md", False, ""),
    ("P2-pr", "gh pr $V 12", False, ""),
    ("P2-mr", "glab mr $V 3", False, ""),
    ("P2-view", "gh issue view $N", False, ""),
    ("P2-list", "gh issue list --state $S", False, ""),
    ("P2-pr-comment", "gh pr comment $N --body x", False, ""),
    ("P2-mr-note", "glab mr note 12 -m x", False, ""),
    ("P3", 'gh api -X POST "repos/$R/issues/12/comments" -f body=x', False, ""),
    ("P4-search", 'gh search issues "x"', False, ""),
    ("P4-repo", "gh repo view $R", False, ""),
    ("P4-run", "gh run view $RID", False, ""),
    ("P4-release", "gh release list", False, ""),
    ("P4-version", "gh --version", False, ""),
    ("P4-auth", "gh auth status", False, ""),
    ("P4-help", "gh --help", False, ""),
    ("P5-ghq", "ghq get $R", False, ""),
    ("P5-gh-foo", "gh-foo issue comment 12", False, ""),
    ("P6-commit-message", 'git commit -m "fix: refuse gh issue comment 12 --body x"', False, ""),
    ("P6-heredoc-file", "cat > notes.md <<'EOF'\ngh issue comment 12 --body x\nEOF", False, ""),
    ("P6-heredoc-unquoted", "cat <<EOF > notes.md\ngh issue comment 12 --body x\nlater\nEOF\nls", False, ""),
    (
        "P6-heredoc-commit",
        "git commit -F - <<'EOF'\ngh issue comment 12 --body x\nit's (still) a mention\nEOF",
        False,
        "",
    ),
    (
        "P6-substituted-heredoc",
        "git commit -m \"$(cat <<'EOF'\ngh issue comment 12 --body x\ndon't run it\nEOF\n)\"",
        False,
        "",
    ),
    ("P6-trailing-comment", "ls  # gh issue comment 12", False, ""),
    ("P6-comment-line", "# gh issue comment 12 --body x\ngit status", False, ""),
    ("P6-comment-separator", "git status  # done; gh issue comment 12 --body x", False, ""),
    ("P6-printf-file", "printf 'gh issue comment 12\\n' >> notes.md", False, ""),
    ("P6-echo-file", "echo gh issue comment 12 > notes.md", False, ""),
    ("P6-quoted-operand", "echo 'do not run gh issue comment'", False, ""),
    ("P6-rg", 'rg "glab issue note" skills/', False, ""),
    ("P6-piped-grep", "cat notes.md | grep 'gh issue comment'", False, ""),
    (
        "P6-commit-code-span",
        "git commit -m \"$(cat <<'EOF'\nfix: `gh -R o/r issue comment 12` is refused\nEOF\n)\"",
        False,
        "",
    ),
    (
        "P6-pr-code-span",
        "gh pr create -t t --body \"$(cat <<'EOF'\nsee `gh issue -R o/r comment 12`, `gh $A`\nEOF\n)\"",
        False,
        "",
    ),
    (
        "P6-file-code-span",
        "cat > notes.md <<'EOF'\n`gh --repo=o/r issue comment 12` or $(gh issue $V 12)\nEOF",
        False,
        "",
    ),
    ("P6-double-quoted-delimiter", 'cat > notes.md <<"EOF"\n`gh issue comment 12`\nEOF', False, ""),
    ("P6-heredoc-in-substitution", "x=\"$(cat <<'EOF'\nit's `gh issue comment 1`\nEOF\n)\"; gh pr view 1", False, ""),
    ("P6-quoted-process-substitution", 'echo "<(gh issue comment 12)"', False, ""),
    ("P6-arithmetic-shift", "echo $((1<<2))\ngh issue view 3", False, ""),
    ("P6-here-string-mention", 'grep -c x <<< "gh issue comment 12"', False, ""),
    ("P6-backslash-delimiter", "cat > notes.md <<\\EOF\n`gh issue comment 12`\nEOF", False, ""),
    ("P6-escaped-substitution", "cat <<EOF\nrun \\$(gh issue comment 12) later\nEOF", False, ""),
    ("empty", "", False, ""),
    ("R1", "gh issue comment 12 --body x", True, ""),
    ("R2", "gh -R o/r issue comment 12 --body x", True, ""),
    ("R3", "gh issue -R o/r comment 12", True, ""),
    ("R4", "gh --repo=o/r issue comment 12", True, ""),
    ("R5", 'gh --repo="$R" issue comment 12', True, ""),
    ("R6", "gh is$X comment 12", True, "is$X"),
    ("R7", "gh -R o/r is$X comment 12", True, "is$X"),
    ("R8", "gh issue $V 12", True, "$V"),
    ("R9", "gh issue -R o/r $V 12", True, "$V"),
    ("R10", "gh $(echo issue) comment 12", True, "$(echo"),
    ("R11", "gh `echo issue` comment 12", True, "`echo"),
    ("R12", "gh $'\\x69ssue' comment 12", True, ""),
    ("R13", "gh ${SUB} comment 12", True, "${SUB}"),
    ("R14", "gh --repo=o/r $(echo issue) comment 12", True, "$(echo"),
    ("R15", "GH_HOST=x gh issue comment 12", True, ""),
    ("R16", "env gh issue comment 12", True, ""),
    ("R17", "glab issue note 3 -m x", True, ""),
    ("R18", "glab -R o/r issue note 3 -m x", True, ""),
    ("R20-double", 'gh "issue" comment 12', True, ""),
    ("R20-single", "gh 'issue' comment 12", True, ""),
    ("R20-empty-quotes", 'gh i""ssue comment 12', True, ""),
    ("R20-backslash", "gh is\\sue comment 12", True, ""),
    ("R21-and", 'cd "$D" && gh issue comment 12 --body x', True, ""),
    ("R21-semicolon", "true; gh issue comment 12", True, ""),
    ("R21-pipe", "echo x | gh issue comment 12 -F -", True, ""),
    ("R21-subshell", "(gh issue comment 12)", True, ""),
    ("R22-command", "command gh issue comment 12", True, ""),
    ("R22-path", "/opt/homebrew/bin/gh issue comment 12", True, ""),
    ("R22-nohup", "nohup gh issue comment 12", True, ""),
    ("R23-args", "gh $ARGS", True, "$ARGS"),
    ("R23-all", 'gh "$@"', True, "$@"),
    ("R24-gh-repo", "GH_REPO=o/r gh issue comment 12", True, ""),
    ("R24-continuation", "gh \\\nissue comment 12", True, ""),
    ("R24-glab-comment", "glab issue comment 3 -m x", True, ""),
    ("note-message", "glab issue note 12 --message 'a requirement'", True, ""),
    ("note-create", "glab issue note create 12 -m x", True, ""),
    ("body-file", "gh issue comment 12 --body-file /tmp/note.md", True, ""),
    ("redirect-glued", "gh 2>/dev/null issue comment 12", True, ""),
    ("redirect-spaced", "gh > out issue comment 12", True, ""),
    ("redirect-dup", "gh 2>&1 issue comment 12", True, ""),
    ("redirect-both", "gh &>/dev/null issue comment 12", True, ""),
    ("here-string", "cat <<< x\ngh issue comment 12", True, ""),
    ("glued-short", "gh -Ro/r issue comment 12", True, ""),
    ("verb-glued-long", "gh issue --repo=o/r comment 12", True, ""),
    ("leaf-option-first", "gh -b x issue comment 12", True, ""),
    ("expanded-option-name", "gh -$X o/r issue comment 12", True, "-$X"),
    ("assigned-subcommand", "X=sue; gh is$X comment 12 --body x", True, "is$X"),
    ("default-expansion", "gh is${X:-sue} comment 12 --body x", True, "is${X:-sue}"),
    ("printf-substitution", "gh $(printf 'is%sue' s) comment 12 --body x", True, "$(printf"),
    ("quoted-substitution", 'gh "$(printf is%sue s)" comment 12 --body x', True, "$(printf"),
    ("ansi-c-fragment", "gh i$'\\x73'sue comment 12 --body x", True, ""),
    ("substituted-note", "x=$(gh issue comment 12 --body y)", True, ""),
    ("glab-expanded", "glab is$X note 12 -m x", True, "is$X"),
    (
        "R25-unquoted-substitution",
        'git commit -m "$(cat <<EOF\nnote: $(gh issue comment 12 --body x)\nEOF\n)"',
        True,
        "",
    ),
    ("R25-unquoted-backtick", "cat <<EOF\n`gh issue comment 12`\nEOF", True, ""),
    ("R26-here-string-mid", "cat <<< x\ngh issue comment 12 --body y\necho done", True, ""),
    ("R26-empty-heredoc", "cat > f <<EOF\nEOF\ngh issue comment 12 --body x", True, ""),
    ("R26-tab-delimiter", "cat <<-EOF\n\tbody\n\tEOF\ngh issue comment 12", True, ""),
    ("R26-tab-delimiter-then-bare", "cat <<-EOF\n\tbody\n\tEOF\ngh issue comment 12\nEOF", True, ""),
    ("R26-here-string-then-its-word", "cat <<< x\ngh issue comment 12\nx", True, ""),
    ("R26-quoted-operator", 'echo "<<EOF"\ngh issue comment 12', True, ""),
    ("R26-quoted-bare-operator", 'echo "<<" EOF\ngh issue comment 12', True, ""),
    ("R26-two-heredocs-one-line", "cat <<A <<B\na\nA\nb\nB\ngh issue comment 12", True, ""),
    ("R26-two-quoted-heredocs", "cat <<'A' <<'B'\na\nA\nb\nB\ngh issue comment 12", True, ""),
    (
        "R27-apostrophe-in-heredoc",
        "cat > n.md <<'EOF'\nI can't\nEOF\nurl=$(gh issue comment 12 --body-file n.md)",
        True,
        "",
    ),
    (
        "R27-apostrophe-unquoted",
        "cat > n.md <<EOF\nI can't\nEOF\nurl=$(gh issue comment 12 --body-file n.md)",
        True,
        "",
    ),
    ("R27-apostrophe-in-comment", "# don't\nurl=$(gh issue comment 12 --body x)", True, ""),
    ("R27-after-heredoc-substitution", "x=\"$(cat <<'EOF'\nit's\nEOF\n)\"; y=$(gh issue comment 1)", True, ""),
    ("R28-timeout", "timeout 30 gh issue comment 12", True, ""),
    ("R28-timeout-options", "timeout -k 5 --signal TERM 30s gh issue comment 12", True, ""),
    ("R28-nice", "nice -n 5 gh issue comment 12", True, ""),
    ("R28-nice-legacy", "nice -10 gh issue comment 12", True, ""),
    ("R28-stdbuf", "stdbuf -oL gh issue comment 12", True, ""),
    ("R28-setsid", "setsid gh issue comment 12", True, ""),
    ("R29-case-arm", "case x in x) gh issue comment 12;; esac", True, ""),
    ("R29-case-arm-line", "case $x in\n  a|b) gh issue comment 12;;\nesac", True, ""),
    ("R29-function", "f() { gh issue comment 12; }; f", True, ""),
    ("R29-function-keyword", "function f { gh issue comment 12; }", True, ""),
    ("R29-process-substitution", "cat <(gh issue comment 12)", True, ""),
    ("R30-options-end", "gh -- issue comment 12", True, ""),
    ("R30-options-end-verb", "gh issue -- comment 12", True, ""),
)


def _detector(command: str) -> str | None:
    return guard.issue_write_deny_reason(command)


def _crash_fallback(command: str) -> str | None:
    with patch("teatree.hooks.raw_issue_write_detect.raw_issue_write_deny_reason", side_effect=RuntimeError("boom")):
        return guard.issue_write_deny_reason(command)


PATHS = {"detector": _detector, "crash-fallback": _crash_fallback}

#: Every ``issue`` verb each forge ships; the guard refuses exactly the note verbs (``comment``, ``note``, unioned).
_GH_ISSUE_VERBS = ("close", "comment", "create", "delete", "develop", "edit", "list", "lock", "note", "pin")
_GLAB_ISSUE_VERBS = ("board", "close", "comment", "create", "delete", "list", "note", "reopen", "subscribe")
_ISSUE_VERBS = {
    "gh": (*_GH_ISSUE_VERBS, "reopen", "status", "transfer", "unlock", "unpin", "view"),
    "glab": (*_GLAB_ISSUE_VERBS, "unsubscribe", "update", "view"),
}


class TestTheMatrix:
    """Every row on both paths: the detector, and the stdlib reading a detector crash falls back to."""

    @pytest.mark.parametrize("path", PATHS)
    @pytest.mark.parametrize(("command", "refused", "named"), [pytest.param(*row[1:], id=row[0]) for row in MATRIX])
    def test_a_row_is_refused_only_when_it_may_be_an_issue_note(
        self, path: str, command: str, *, refused: bool, named: str
    ) -> None:
        reason = PATHS[path](command)
        assert (reason is not None) is refused, reason
        if named:
            assert named in (reason or "")
            assert "literally" in (reason or "")

    @pytest.mark.parametrize(("command", "refused"), [pytest.param(*row[1:3], id=row[0]) for row in MATRIX])
    def test_the_detector_answers_the_same_for_both_lanes(self, command: str, *, refused: bool) -> None:
        assert is_raw_issue_write(command) is refused
        assert (raw_issue_write_deny_reason(command) is not None) is refused

    @pytest.mark.parametrize("path", PATHS)
    @pytest.mark.parametrize(
        ("program", "verb"), [(program, verb) for program, verbs in _ISSUE_VERBS.items() for verb in verbs]
    )
    def test_exactly_the_note_verbs_are_refused(self, path: str, program: str, verb: str) -> None:
        refused = PATHS[path](f"{program} -R o/r issue {verb} 12") is not None
        assert refused is (verb in {"comment", "note"})


class TestTheNoteIsDenied:
    """The reasons: the facade for a note, the undecided word and the escape for an expansion."""

    def test_unexpected_detector_error_denies(self) -> None:
        reason = _crash_fallback("gh issue comment 12 --body x")
        assert reason is not None
        assert "denied" in reason

    def test_a_literal_note_names_the_purpose_routed_remedy(self) -> None:
        reason = raw_issue_write_deny_reason("gh -R o/r issue comment 12 --body x") or ""
        assert reason.startswith("BLOCKED:")
        assert "--purpose" in reason
        assert "ticket comment" in reason

    def test_the_reason_names_the_sanctioned_removal_path(self) -> None:
        assert "delete-issue-note" in (raw_issue_write_deny_reason("glab issue note 12 -m x") or "")

    @pytest.mark.parametrize("path", PATHS)
    def test_an_undecided_word_names_the_escape_that_passes(self, path: str) -> None:
        reason = PATHS[path]('gh --repo=o/r "$SUB" comment 12') or ""
        assert "`$SUB`" in reason
        assert "Spell the subcommand and verb literally" in reason
        assert PATHS[path]('gh pr view 12 --repo "$R"') is None


class TestTheHandler:
    """The PreToolUse arm: Bash only, deny on a note, pass everything else."""

    def test_a_non_bash_tool_is_untouched(self) -> None:
        event = {"tool_name": "Edit", "tool_input": {"command": "gh issue comment 1 --body x"}}
        assert guard.handle_block_raw_issue_write(event) is False

    def test_a_bash_read_is_untouched(self) -> None:
        event = {"tool_name": "Bash", "tool_input": {"command": "gh issue view 1"}}
        assert guard.handle_block_raw_issue_write(event) is False

    def test_the_router_re_exports_the_same_handler(self) -> None:
        assert router.handle_block_raw_issue_write is guard.handle_block_raw_issue_write
        assert router.handle_block_raw_issue_write in router._HANDLERS["PreToolUse"]

    def test_both_lanes_refuse_it(self) -> None:
        # Lane B's pydantic-ai shell reads the same registry the cold subprocess does.
        assert (hard_deny_reason("gh issue comment 12 --body x") or "").startswith("BLOCKED:")
        assert hard_deny_reason("gh issue view 12") is None


class TestColdImport:
    """The live PreToolUse hook is a bare ``python3`` subprocess with no Django configured."""

    def test_imports_with_stdlib_only_no_django(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import raw_issue_write_guard as s; "
                    "assert 'django' not in sys.modules, 'django imported at module top'; "
                    "assert not any(m == 'teatree' or m.startswith('teatree.') for m in sys.modules), "
                    "'teatree imported at module top'; "
                    "print(s.handle_block_raw_issue_write({'tool_name': 'Edit', 'tool_input': {}}))"
                ),
                str(Path(router.__file__).resolve().parent),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"
