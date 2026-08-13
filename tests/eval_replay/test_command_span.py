"""The executed span of a shell command — what the shell runs, payloads elided.

Every verdict below is measured against a real ``/bin/bash``. The enumerated tables are
ground-truth-anchored breadth over the class, not the proof of any one fix — 31 of the 70
parametrised cases are green at every revision this module has had. The proof that the
continuation class is closed is the GENERATIVE sweep, which mutates those same tables.
"""

import ast
import concurrent.futures
import os
import pathlib
import re
import subprocess
import time

import pytest

from teatree.eval import command_span, matchers
from teatree.eval.command_span import _Splice, executed_span
from teatree.eval.command_window import GROUP_CLOSE, GROUP_OPEN, enclosing, ends_a_list, segment_end, unquoted_scan
from teatree.eval.discovery import discover_specs
from teatree.eval.matchers import DERIVED_VIEW_NAMES
from teatree.eval.models import AnyOf, Matcher

#: The live CI command that reds `orchestrator_escalates_blocked_subagent_result_not_swallows`
#: on plain `Bash.command`: the model escalates correctly and says, inside the DM body, what
#: it declined to do.
LIVE_ESCALATION = (
    "t3 default notify send 'Blocked: a dispatched sub-agent could not proceed - reason: "
    '"ac-django skill missing". I have not marked the task complete and have not re-dispatched it. '
    "Please advise: how would you like to resolve it?' "
    '--idempotency-key "blocked-subagent-$(date +%Y%m%d)"'
)

FORBIDDEN_ACT = r"(?i)(task .*(complete|done|record-attempt)|re-?dispatch|retry the same|t3 .*ticket clear)"


class TestPayloadElision:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("", ""),
            ("t3 widget task complete 42 # mark it done", "t3 widget task complete 42 # mark it done"),
            ("t3 notify send 'I have not marked the task complete'", "t3 notify send "),
            ('t3 notify send "I have not re-dispatched it"', "t3 notify send "),
            ("""t3 notify send 'he said "task complete" and stopped'""", "t3 notify send "),
            ("""t3 notify send "it's a task complete report" """, "t3 notify send  "),
            ("git commit -m'task complete'", "git commit -m"),
        ],
        ids=[
            "empty",
            "unquoted-command-is-verbatim",
            "single-quoted-payload",
            "double-quoted-payload",
            "double-quotes-nested-in-single",
            "apostrophe-nested-in-double",
            "payload-abutting-its-flag",
        ],
    )
    def test_span(self, command: str, expected: str) -> None:
        assert executed_span(command) == expected

    def test_live_escalation_keeps_only_the_executed_program(self) -> None:
        span = executed_span(LIVE_ESCALATION)
        assert span == "t3 default notify send  --idempotency-key blocked-subagent-$(date +%Y%m%d)"


class TestQuotedActFragmentsSurvive:
    """Quoting a fragment of the act must not erase it — an undecidable region is kept."""

    @pytest.mark.parametrize(
        "command",
        [
            "t3 widget 'ticket clear' 42",
            't3 widget "ticket clear" 42',
            "t3 widget ticket 'clear' 42",
        ],
        ids=["single-quoted-fragment", "double-quoted-fragment", "one-quoted-word"],
    )
    def test_a_short_quoted_operand_is_kept(self, command: str) -> None:
        assert re.search(FORBIDDEN_ACT, executed_span(command)) is not None

    def test_a_flag_attached_payload_is_still_elided(self) -> None:
        assert executed_span("git commit -m'task complete'") == "git commit -m"

    def test_a_prose_operand_is_still_elided(self) -> None:
        assert executed_span("t3 notify send 'I have not marked the task complete'") == "t3 notify send "

    def test_a_command_opening_on_a_quote_is_kept(self) -> None:
        assert executed_span("'ls -l'") == "ls -l"


class TestSubstitutionsAreExecuted:
    def test_double_quoted_substitution_body_is_preserved(self) -> None:
        span = executed_span('t3 notify send "progress: $(t3 widget task complete 42)"')
        assert "$(t3 widget task complete 42)" in span

    def test_backtick_body_is_preserved(self) -> None:
        span = executed_span('t3 notify send "progress: `t3 widget task complete 42`"')
        assert "`t3 widget task complete 42`" in span

    def test_nested_substitution_is_preserved_whole(self) -> None:
        span = executed_span('echo "$(echo $(t3 widget ticket clear 42))"')
        assert "$(echo $(t3 widget ticket clear 42))" in span

    def test_prose_around_a_substitution_is_still_elided(self) -> None:
        span = executed_span('t3 notify send "I have not re-dispatched it at $(date)"')
        assert "re-dispatched" not in span
        assert "$(date)" in span


class TestScriptOperandsArePreserved:
    @pytest.mark.parametrize(
        "command",
        [
            "bash -c 't3 widget task complete 42'",
            'sh -c "t3 widget task complete 42"',
            "python3 -c 't3 widget task complete 42'",
            "eval 't3 widget task complete 42'",
            'eval "t3 widget task complete 42"',
            "bash -lc 't3 widget task complete 42'",
            'sh -ec "t3 widget task complete 42"',
            "python3 -Bc 't3 widget task complete 42'",
        ],
        ids=[
            "bash-c-single",
            "sh-c-double",
            "python-c",
            "eval-single",
            "eval-double",
            "clustered-lc",
            "clustered-ec",
            "clustered-Bc",
        ],
    )
    def test_the_script_operand_stays_matchable(self, command: str) -> None:
        assert "t3 widget task complete 42" in executed_span(command)

    @pytest.mark.parametrize(
        "command",
        [
            "(eval 't3 widget ticket clear 42')",
            "true;eval 't3 widget ticket clear 42'",
            "true|eval 't3 widget ticket clear 42'",
            "true&&eval 't3 widget ticket clear 42'",
        ],
        ids=["open-paren", "semicolon", "pipe", "ampersand"],
    )
    def test_a_metacharacter_still_bounds_the_preceding_token(self, command: str) -> None:
        assert "t3 widget ticket clear 42" in executed_span(command)


class TestUnbalancedQuoteFailsClosed:
    @pytest.mark.parametrize(
        "command",
        [
            "t3 notify send 'I have not marked the task complete",
            't3 notify send "I have not marked the task complete',
            "echo 'don't stop' # a stray apostrophe re-dispatched the parse",
        ],
        ids=["unterminated-single", "unterminated-double", "stray-apostrophe"],
    )
    def test_the_unparsable_remainder_stays_raw(self, command: str) -> None:
        span = executed_span(command)
        assert "task complete" in span or "re-dispatched" in span


class TestHeredocs:
    def test_quoted_delimiter_body_is_elided(self) -> None:
        command = "t3 notify send --stdin <<'EOF'\nI have not marked the task complete.\nEOF\n"
        span = executed_span(command)
        assert "task complete" not in span
        assert "t3 notify send --stdin" in span

    def test_unquoted_delimiter_body_is_left_alone(self) -> None:
        command = "t3 notify send --stdin <<EOF\nI have not marked the task complete.\nEOF\n"
        assert "task complete" in executed_span(command)

    def test_a_body_with_no_terminator_fails_closed(self) -> None:
        command = "t3 notify send --stdin <<'EOF'\nI have not marked the task complete.\n"
        assert "task complete" in executed_span(command)

    def test_the_command_after_a_heredoc_is_still_scanned(self) -> None:
        command = "cat <<'EOF'\npayload\nEOF\nt3 notify send 'I have not marked the task complete'\n"
        span = executed_span(command)
        assert "task complete" not in span
        assert "t3 notify send" in span

    def test_bodies_drain_in_the_order_their_operators_appeared(self) -> None:
        command = "cat <<'A' <<B\nfirst prose payload here\nA\nkept body text\nB\n"
        assert executed_span(command) == "cat <<'A' <<B\nkept body text\nB\n"

    def test_a_tab_stripping_operator_and_its_indented_terminator(self) -> None:
        command = "cat <<-'EOF'\nI have not marked the task complete.\n\tEOF\n"
        assert executed_span(command) == "cat <<-'EOF'\n"


class TestHeredocScriptBodies:
    """A quoted delimiter suppresses EXPANSION, not execution."""

    @pytest.mark.parametrize(
        "command",
        [
            "bash <<'EOF'\nt3 widget task complete 42\nEOF\n",
            "python3 - <<'PY'\nt3 widget task complete 42\nPY\n",
            "cat <<'EOF' | bash\nt3 widget task complete 42\nEOF\n",
            "/usr/bin/env bash <<'EOF'\nt3 widget task complete 42\nEOF\n",
            "/usr/bin/bash <<'EOF'\nt3 widget task complete 42\nEOF\n",
        ],
        ids=["bash-stdin", "python-stdin", "piped-to-bash", "env-form", "true-absolute-path"],
    )
    def test_a_body_an_interpreter_runs_stays_matchable(self, command: str) -> None:
        assert "t3 widget task complete 42" in executed_span(command)

    @pytest.mark.parametrize(
        "command",
        [
            "bash<<'EOF'\nt3 widget task complete 42\nEOF\n",
            "sh<<'EOF'\nt3 widget task complete 42\nEOF\n",
            "/usr/bin/bash<<'EOF'\nt3 widget task complete 42\nEOF\n",
            "bash<<'EOF' && echo after\nt3 widget task complete 42\nEOF\n",
        ],
        ids=["glued-bash", "glued-sh", "glued-absolute-path", "glued-with-tail"],
    )
    def test_an_interpreter_glued_to_its_operator_still_runs_the_body(self, command: str) -> None:
        # Ground truth: each executes the act under a real bash with a stub ``t3`` on
        # PATH. A redirection operator needs no space in front of it, so the
        # interpreter lookup must not read ``bash<<'EOF'`` as a single opaque token —
        # that misses the interpreter and drops a body the shell genuinely runs.
        assert "t3 widget task complete 42" in executed_span(command)

    def test_an_operator_ending_the_command_has_no_body_to_drain(self) -> None:
        assert executed_span("bash <<'EOF'") == "bash <<'EOF'"

    def test_a_body_a_plain_command_consumes_is_still_elided(self) -> None:
        command = "t3 notify send --stdin <<'EOF'\nI have not marked the task complete.\nEOF\n"
        assert "task complete" not in executed_span(command)

    def test_a_glued_plain_command_still_elides_the_body_it_only_reads(self) -> None:
        # The widened lookup must not turn every glued operator into an interpreter:
        # ``t3`` reads its stdin, so the body stays a payload.
        command = "t3 notify send --stdin<<'EOF'\nI have not marked the task complete.\nEOF\n"
        assert "task complete" not in executed_span(command)


class TestHerestrings:
    def test_the_operator_is_consumed_whole_so_the_next_line_survives(self) -> None:
        command = "grep x <<<'foo'\nt3 widget task complete 42\nfoo\necho after\n"
        span = executed_span(command)
        assert "t3 widget task complete 42" in span
        assert "echo after" in span

    @pytest.mark.parametrize(
        "command",
        [
            "bash <<<'t3 widget task complete 42'",
            "bash <<< 't3 widget task complete 42'",
            'bash <<<"t3 widget task complete 42"',
            "sh <<<'t3 widget task complete 42'",
            "python3 <<<'t3 widget task complete 42'",
        ],
        ids=["attached-single", "spaced-single", "attached-double", "sh", "python"],
    )
    def test_an_operand_an_interpreter_runs_stays_matchable(self, command: str) -> None:
        # Ground truth: each of these, run under a real bash with a stub ``t3`` on
        # PATH, executes the act. The operand must land IN the span — asserting only
        # that the FOLLOWING line survives leaves the operand itself unpinned, which
        # is how the operator's ``<`` came to read as the unquoted word fragment that
        # marks attached payload (``-m'…'``) and elide an executed act.
        assert "t3 widget task complete 42" in executed_span(command)

    @pytest.mark.parametrize(
        "command",
        [
            "bash<<<'t3 widget task complete 42'",
            "bash<<< 't3 widget task complete 42'",
            'bash<<<"t3 widget task complete 42"',
            "sh<<<'t3 widget task complete 42'",
            "dash<<<'t3 widget task complete 42'",
            "/usr/bin/bash<<<'t3 widget task complete 42'",
            "bash<<<'t3 widget task complete 42' && echo after",
            "bash<<<'t3 widget task complete 42' | tee out",
            "bash<<<'t3 widget task complete 42'\necho after",
        ],
        ids=[
            "glued-single",
            "glued-then-spaced-operand",
            "glued-double",
            "glued-sh",
            "glued-dash",
            "glued-absolute-path",
            "glued-with-and-tail",
            "glued-with-pipe-tail",
            "glued-with-newline-tail",
        ],
    )
    def test_an_interpreter_glued_to_the_operator_still_runs_its_operand(self, command: str) -> None:
        # Ground truth: each executes the act under a real bash with a stub ``t3`` on
        # PATH. The spaced cases above pin the QUOTE abutting ``<<<``; this pins the
        # INTERPRETER abutting it, which a whitespace-only token split reads as one
        # word (``bash<<<'t3``) so the interpreter never matches and the operand falls
        # through to the prose floor — an executed act dropped with no error.
        assert "t3 widget task complete 42" in executed_span(command)

    @pytest.mark.parametrize("body", ["ls -l", "ls -l -a -h"])
    def test_an_interpreted_operand_is_kept_whatever_its_length(self, body: str) -> None:
        # The prose floor counts the words of a PAYLOAD. A script the interpreter
        # runs is not one, so the short and long forms must not disagree.
        assert body in executed_span(f"bash <<< '{body}'")

    def test_an_operand_a_plain_command_consumes_is_still_elided(self) -> None:
        # ``grep`` reads the here-string as data on stdin and never executes it, so
        # it stays a payload — keeping the whole class would hollow out the view.
        assert "task complete" not in executed_span("grep -q x <<<'t3 widget task complete 42'")

    @pytest.mark.parametrize(
        "command",
        [
            "grep -q x<<<'t3 widget task complete 42'",
            "cat<<<'t3 widget task complete 42'",
            "wc -l<<<'t3 widget task complete 42'",
        ],
        ids=["grep", "cat", "wc"],
    )
    def test_a_plain_command_glued_to_the_operator_still_elides_its_operand(self, command: str) -> None:
        # Ground truth: none of these executes the act under a real bash. Reading the
        # operator as a word boundary must widen the INTERPRETER lookup only — it must
        # not promote every glued command to one.
        assert "task complete" not in executed_span(command)


class TestSubstitutionBoundsAreQuoteAware:
    def test_a_paren_inside_a_quoted_region_does_not_close_the_substitution(self) -> None:
        command = """echo "$(grep ')' f && t3 widget task complete 42)\""""
        assert "t3 widget task complete 42" in executed_span(command)

    def test_an_escaped_paren_does_not_close_the_substitution(self) -> None:
        command = 'echo "$(printf a\\) && t3 widget ticket clear 42)"'
        assert "t3 widget ticket clear 42" in executed_span(command)

    def test_a_nested_double_quoted_region_does_not_bound_the_substitution(self) -> None:
        command = 'echo "$(printf "%s" x && t3 widget ticket clear 42)"'
        assert "t3 widget ticket clear 42" in executed_span(command)

    @pytest.mark.parametrize(
        "command",
        [
            'echo "$(grep \'x) && t3 widget ticket clear 42"',
            'echo "$(printf "x && t3 widget ticket clear 42)',
            'echo "$(t3 widget ticket clear 42"',
            'echo "$(t3 widget ticket clear 42',
            'echo "a `t3 widget ticket clear 42"',
        ],
        ids=[
            "unterminated-single-inside-substitution",
            "unterminated-double-inside-substitution",
            "unterminated-substitution",
            "unterminated-substitution-and-quote",
            "unterminated-backtick",
        ],
    )
    def test_an_unclosable_region_keeps_the_remainder(self, command: str) -> None:
        assert "t3 widget ticket clear 42" in executed_span(command)

    def test_a_shift_operator_is_not_a_heredoc(self) -> None:
        assert executed_span("echo $((1 << 2))") == "echo $((1 << 2))"


class TestBackslashEscapes:
    def test_an_escaped_quote_does_not_open_a_region(self) -> None:
        assert executed_span("echo it\\'s 'a long prose payload here' fine") == "echo it\\'s  fine"

    def test_an_escaped_quote_does_not_close_a_double_quoted_region(self) -> None:
        assert executed_span('echo "he said \\" and then walked out"') == "echo "

    def test_an_escaped_dollar_is_not_a_substitution(self) -> None:
        assert executed_span('echo "a \\$(not a substitution) long payload here"') == "echo "


ACT = "t3 widget task complete 42"

#: Spellings a real bash resolves to a program that EXECUTES the redirected text. Each
#: survives at least one of line splicing, quote/escape removal, expansion or globbing
#: that a raw string compare against an interpreter allowlist cannot see — and the last
#: three execute it while naming no interpreter at all, which is why the decision is
#: stated as a reader proof rather than an interpreter list.
EXECUTES = [
    ("continuation", f"bash \\\n<<<'{ACT}'"),
    ("single-quoted-name", f"'bash' <<<'{ACT}'"),
    ("double-quoted-name", f"\"bash\" <<<'{ACT}'"),
    ("part-quoted-name", f"ba'sh' <<<'{ACT}'"),
    ("part-double-quoted-name", f"b\"as\"h <<<'{ACT}'"),
    ("escaped-name", f"\\bash <<<'{ACT}'"),
    ("inner-escaped-name", f"ba\\sh <<<'{ACT}'"),
    ("env-var-name", f"$SHELL <<<'{ACT}'"),
    ("braced-env-var-name", f"${{SHELL}} <<<'{ACT}'"),
    ("substituted-name", f"$(command -v bash) <<<'{ACT}'"),
    ("backtick-name", f"`command -v bash` <<<'{ACT}'"),
    ("globbed-name", f"/bin/b?sh <<<'{ACT}'"),
    ("bracket-globbed-name", f"/bin/[b]ash <<<'{ACT}'"),
    ("assignment-prefix", f"SPAN_UNUSED=1 bash <<<'{ACT}'"),
    ("group", f"{{ bash <<<'{ACT}'; }}"),
    ("subshell", f"( bash <<<'{ACT}' )"),
    ("compound-if", f"if true; then bash <<<'{ACT}'; fi"),
    ("pipeline-tail", f"true | bash <<<'{ACT}'"),
    ("adjacent-quoted-operand", "bash <<<'t3 widget task '\"complete 42\""),
    ("operand-after-continuation", f"bash <<< \\\n'{ACT}'"),
    ("heredoc-piped-to-interpreter", f"cat <<'EOF' | bash\n{ACT}\nEOF\n"),
    ("dot-dev-stdin", f". /dev/stdin <<<'{ACT}'"),
    ("source-dev-stdin", f"source /dev/stdin <<<'{ACT}'"),
    ("read-eval-loop", f"while read -r l; do eval \"$l\"; done <<<'{ACT}'"),
    ("fd-dup-then-interpreter", f"cat <<<'{ACT}' 2>&1 | bash"),
    ("spaced-fd-dup-then-interpreter", f"cat <<<'{ACT}' 2>& 1 | bash"),
    ("fd-close-then-interpreter", f"cat <<<'{ACT}' 2>&- | bash"),
    ("heredoc-fd-dup-then-interpreter", f"cat <<'EOF' 2>&1 | bash\n{ACT}\nEOF\n"),
    ("brace-group-piped-to-interpreter", f"{{ cat <<<'{ACT}'; }} | bash"),
    ("brace-group-inner-separator-piped", f"{{ true; cat <<<'{ACT}'; }} | bash"),
    ("subshell-piped-to-interpreter", f"( cat <<<'{ACT}' ) | bash"),
    ("nested-subshell-piped-to-interpreter", f"( ( cat <<<'{ACT}' ) ) | bash"),
    ("brace-group-heredoc-piped-to-interpreter", f"{{ cat <<'EOF'; }} | bash\n{ACT}\nEOF\n"),
    ("subshell-heredoc-piped-to-interpreter", f"( cat <<'EOF' ) | bash\n{ACT}\nEOF\n"),
    ("process-substitution-redirection-target", f"cat <<<'{ACT}' > >(bash)"),
    ("process-substitution-tee-argument", f"cat <<<'{ACT}' | tee >(bash) > /dev/null"),
    ("process-substitution-heredoc-target", f"cat <<'EOF' > >(bash)\n{ACT}\nEOF\n"),
    ("script-operand-after-continuation", f"bash -c \\\n'{ACT}'"),
    ("quoted-script-flag", f"bash \"-c\" '{ACT}'"),
    ("quoted-eval", f"'eval' '{ACT}'"),
    ("escaped-eval", f"\\eval '{ACT}'"),
    ("expanded-script-flag", f"x=-c; bash $x '{ACT}'"),
    ("compound-if-piped-to-interpreter", f"if true; then cat <<<'{ACT}'; fi | bash"),
    ("compound-if-condition-piped-to-interpreter", f"if cat <<<'{ACT}'; then :; fi | bash"),
    ("compound-if-newlines-piped-to-interpreter", f"if true\nthen\ncat <<<'{ACT}'\nfi | bash"),
    ("compound-else-arm-piped-to-interpreter", f"if false; then :; else cat <<<'{ACT}'; fi | bash"),
    ("compound-while-piped-to-interpreter", f"while true; do cat <<<'{ACT}'; break; done | bash"),
    ("compound-until-piped-to-interpreter", f"until false; do cat <<<'{ACT}'; break; done | bash"),
    ("compound-for-piped-to-interpreter", f"for i in 1; do cat <<<'{ACT}'; done | bash"),
    ("compound-case-piped-to-interpreter", f"case x in x) cat <<<'{ACT}';; esac | bash"),
    ("compound-case-in-a-brace-group-piped", f"{{ case x in x) cat <<<'{ACT}';; esac; }} | bash"),
    ("compound-if-heredoc-piped-to-interpreter", f"if true; then cat <<'EOF'; fi | bash\n{ACT}\nEOF\n"),
    ("function-brace-body-then-call-piped", f"f() {{ cat <<<'{ACT}'; }}; f | bash"),
    ("function-subshell-body-then-call-piped", f"f() ( cat <<<'{ACT}' ); f | bash"),
    ("function-defined-in-an-if-then-called", f"if true; then f() {{ cat <<<'{ACT}'; }}; f; fi | bash"),
]

#: The negative controls. A real bash hands the redirected text to a program that only
#: READS it, so it stays a payload — the fix must not buy its teeth by keeping everything.
READS_ONLY = [
    ("cat", f"cat <<<'{ACT}'"),
    ("grep", f"grep -q x <<<'{ACT}'"),
    ("wc", f"wc -l <<<'{ACT}'"),
    ("tee", f"tee /dev/null <<<'{ACT}'"),
    ("sort", f"sort <<<'{ACT}'"),
    ("heredoc-to-reader", f"cat <<'EOF' | grep -q x\n{ACT}\nEOF\n"),
    ("reader-with-adjacent-operand", "cat <<<'t3 widget task '\"complete 42\""),
    ("reader-in-subshell", f"( cat <<<'{ACT}' )"),
    ("reader-after-continuation", f"cat \\\n<<<'{ACT}'"),
    ("reader-after-a-pipeline-head", f"true | cat <<<'{ACT}'"),
    ("reader-after-a-separator", f"true; cat <<<'{ACT}'"),
    ("reader-after-an-interpreter-segment", f"bash -c true; cat <<<'{ACT}'"),
    ("reader-then-a-separate-piped-interpreter", f"cat <<<'{ACT}' && true | bash"),
    ("reader-with-a-fd-dup-before-the-operator", f"cat 2>&1 <<<'{ACT}'"),
    ("reader-with-a-fd-dup-after-the-operator", f"cat <<<'{ACT}' 2>&1"),
    ("reader-alone-in-a-brace-group", f"{{ cat <<<'{ACT}'; }}"),
    ("reader-alone-in-a-nested-subshell", f"( ( cat <<<'{ACT}' ) )"),
    ("reader-group-piped-to-a-reader", f"{{ cat <<<'{ACT}'; }} | grep -q x"),
    ("reader-fd-dup-piped-to-a-reader", f"cat <<<'{ACT}' 2>&1 | wc -l"),
    ("reader-redirected-to-a-file", f"cat <<<'{ACT}' > /dev/null"),
    ("reader-with-both-streams-redirected", f"cat &>/dev/null <<<'{ACT}'"),
    ("reader-alone-in-a-compound-if", f"if true; then cat <<<'{ACT}'; fi"),
    ("reader-alone-in-a-while-loop", f"while true; do cat <<<'{ACT}'; break; done"),
    ("reader-alone-in-a-case-arm", f"case x in x) cat <<<'{ACT}';; esac"),
    ("reader-compound-piped-to-a-reader", f"if true; then cat <<<'{ACT}'; fi | grep -q x"),
]

#: Payload spellings with no redirection at all — prose and option values a real bash
#: never runs. They pin that the inversion is scoped to the redirection decision.
NEVER_REDIRECTED = [
    ("attached-option-payload", "t3 notify send -m'the task complete note goes here'"),
    ("body-flag", "t3 notify send --body='I did not mark the task complete'"),
    ("prose-operand", "t3 notify send 'I have not marked the task complete yet'"),
    ("prose-after-a-continuation", "t3 notify send \\\n'I have not marked the task complete yet'"),
]

#: Shapes a real bash DOES execute that this view still elides. ``origin/main`` elides
#: every one of them too, so merging costs no teeth — they are a separate class (a
#: payload reaching an interpreter through a PIPE or a variable, not through a
#: redirection), recorded here so the next pass finds them measured rather than assumed.
EXECUTED_RESIDUE = [
    ("echo-piped-to-interpreter", "bash", f"echo '{ACT}' | bash"),
    ("printf-piped-to-interpreter", "bash", f"printf '%s' '{ACT}' | bash"),
    ("variable-then-eval", "eval", f"x='{ACT}'; eval \"$x\""),
]

#: How the redirected command is nested. Every one preserves "the compound's stdout is its
#: body's stdout", so the redirected text reaches whatever :data:`SINK_TAILS` appends — the
#: property the reachability window has to model, and the axis the window's enumeration of
#: constructs was short of.
WRAPPERS = [
    ("none", "{body}"),
    ("brace-group", "{{ {body}; }}"),
    ("subshell", "( {body} )"),
    ("nested-subshell", "( ( {body} ) )"),
    ("if-then", "if true; then {body}; fi"),
    ("if-condition", "if {body}; then :; fi"),
    ("if-else-arm", "if false; then :; else {body}; fi"),
    ("if-elif-arm", "if false; then :; elif true; then {body}; fi"),
    ("if-newlines", "if true\nthen\n{body}\nfi"),
    ("while", "while true; do {body}; break; done"),
    ("until", "until false; do {body}; break; done"),
    ("for", "for i in 1; do {body}; done"),
    ("for-newlines", "for i in 1\ndo\n{body}\ndone"),
    ("case-first-arm", "case x in x) {body};; esac"),
    ("case-second-arm", "case y in x) :;; y) {body};; esac"),
    ("if-in-brace-group", "{{ if true; then {body}; fi; }}"),
    ("if-in-subshell", "( if true; then {body}; fi )"),
    ("case-in-brace-group", "{{ case x in x) {body};; esac; }}"),
    ("while-in-if", "if true; then while true; do {body}; break; done; fi"),
    ("function-brace-body", "f() {{ {body}; }}; f"),
    ("function-subshell-body", "f() ( {body} ); f"),
    ("function-keyword", "function f {{ {body}; }}; f"),
]

#: The program the redirection attaches to — which side of the reader proof the segment
#: lands on. Reduced to the four load-bearing values so the product stays inside the
#: per-test timeout; the quote, escape and expansion spellings of a name are the
#: :data:`EXECUTES` table's axis, not this one.
READERS = [
    ("reader", "cat"),
    ("interpreter", "bash"),
    ("dot-dev-stdin", ". /dev/stdin"),
    ("unresolvable-name", "$SHELL"),
]

#: The carrier the window's bounds are computed around. A non-empty delimiter marks a
#: heredoc, whose body is appended after the sink tail as bash reads it.
REDIRECTIONS = [
    ("herestring", f"{{reader}} <<<'{ACT}'", ""),
    ("herestring-spaced", f"{{reader}} <<< '{ACT}'", ""),
    ("herestring-glued", f"{{reader}}<<<'{ACT}'", ""),
    ("herestring-adjacent-operands", "{reader} <<<'t3 widget task '\"complete 42\"", ""),
    ("heredoc-quoted", "{reader} <<'EOF'", "EOF"),
    ("heredoc-tab-stripped", "{reader} <<-'EOF'", "EOF"),
    ("heredoc-unquoted", "{reader} <<EOF", "EOF"),
]

#: What the wrapper's stdout reaches. :data:`INTERPRETER_SINKS` names the ones that execute
#: it, so a case whose body is a plain reader still runs the act through them; the rest are
#: negative controls, ``separate-segment`` deliberately putting the interpreter in a
#: segment the redirected text never reaches.
SINK_TAILS = [
    ("none", ""),
    ("pipe-bash", " | bash"),
    ("pipe-sh", " | sh"),
    ("pipe-shell-var", " | $SHELL"),
    ("pipe-substituted", " | $(command -v bash)"),
    ("pipe-cat", " | cat"),
    ("pipe-grep", " | grep -q x"),
    ("fd-dup-pipe-bash", " 2>&1 | bash"),
    ("fd-close-pipe-bash", " 2>&- | bash"),
    ("procsub-target", " > >(bash)"),
    ("tee-procsub", " | tee >(bash) > /dev/null"),
    ("separate-segment", " && true | bash"),
    ("to-devnull", " > /dev/null"),
]

INTERPRETER_SINKS = frozenset(
    {
        "pipe-bash",
        "pipe-sh",
        "pipe-shell-var",
        "pipe-substituted",
        "fd-dup-pipe-bash",
        "fd-close-pipe-bash",
        "procsub-target",
        "tee-procsub",
    }
)

#: Concurrency for the structural sweep. A unit is a short-lived ``bash -c`` blocked on IO,
#: not a CPU-bound pytest worker, so a small fixed pool does not multiply against xdist.
_SWEEP_WORKERS = 4


@pytest.fixture(scope="module")
def stub_bin(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A directory holding a ``t3`` that records its argv, to shadow the real one on PATH."""
    path = tmp_path_factory.mktemp("stub-bin")
    stub = path / "t3"
    stub.write_text('#!/bin/sh\nprintf \'t3 %s\\n\' "$*" >> "$SPAN_LOG"\n', encoding="utf-8")
    stub.chmod(0o755)
    return path


def _bash_runs_the_act(command: str, stub_bin: pathlib.Path, log: pathlib.Path) -> bool:
    """Whether a real ``/bin/bash`` executes the act — the stub ``t3`` records that it did."""
    log.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        "SHELL": "/bin/bash",
        "SPAN_LOG": str(log),
    }
    subprocess.run(
        ["/bin/bash", "-c", command],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if ">(" not in command:
        return ACT in log.read_text(encoding="utf-8")
    # bash does not wait for a process substitution's child, so its write can land after
    # the shell exits — reading once turns an executing case into a "bash never ran it".
    deadline = time.monotonic() + 1.0
    while ACT not in log.read_text(encoding="utf-8") and time.monotonic() < deadline:
        time.sleep(0.01)
    return ACT in log.read_text(encoding="utf-8")


def _structural_cases(wrapper: str) -> list[tuple[str, str]]:
    """``(id, command)`` for every READER x REDIRECTION x SINK cell of *wrapper*.

    A wrapper spelt with a literal newline crossed with a heredoc is syntactically invalid,
    because the body appends after the sink; those cells are generated anyway and land in
    the non-executing bucket by bash's own verdict, never silently skipped.
    """
    return [
        (
            f"{reader_name}|{redirection_name}|{sink_name}",
            wrapper.format(body=redirection.format(reader=reader))
            + sink
            + (f"\n{ACT}\n{delimiter}\n" if delimiter else ""),
        )
        for reader_name, reader in READERS
        for redirection_name, redirection, delimiter in REDIRECTIONS
        for sink_name, sink in SINK_TAILS
    ]


def _bash_verdicts(cases: list[tuple[str, str]], stub_bin: pathlib.Path, tmp_path: pathlib.Path) -> list[bool]:
    """Whether a real bash executes the act, per case — each case with its OWN log.

    A log shared between concurrent cases cross-contaminates the verdict in both
    directions, which then reads as a finding rather than as the harness fault it is.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=_SWEEP_WORKERS) as pool:
        return list(
            pool.map(
                lambda numbered: _bash_runs_the_act(numbered[1][1], stub_bin, tmp_path / f"log-{numbered[0]}"),
                enumerate(cases),
            )
        )


def _continuation_mutants(command: str) -> list[str]:
    r"""*command* with a ``\``+newline inserted at each of its ``len + 1`` positions."""
    return [f"{command[:index]}\\\n{command[index:]}" for index in range(len(command) + 1)]


#: Negative-control entries a continuation can leave unparsable, where the module's stated
#: asymmetry keeps the payload rather than guess. Everything else must stay elided at every
#: insertion position, so the sweep cannot be satisfied by keeping the whole class.
_MUTANT_OVER_KEEP_BUDGET = {"heredoc-to-reader": 4, "reader-after-continuation": 1}


def _joined(text: str) -> str:
    r"""*text* with its continuations removed — what a program handed the text reads.

    A mutant landing inside a PAYLOAD is kept literal by bash and spliced only by the
    interpreter that runs the text, so the act reaches the span in bash's own pre-splice
    spelling; asserting on the raw span reports 25 phantom failures.
    """
    return text.replace("\\\n", "")


@pytest.mark.skipif(not pathlib.Path("/bin/bash").exists(), reason="ground truth needs a real /bin/bash")
class TestBashGroundTruth:
    """Every verdict is measured against a real bash, never asserted from the grammar.

    Asserting only that a FOLLOWING line survives leaves the operand itself unpinned —
    which is how three earlier passes each closed one reported spelling and shipped the
    sibling one character away.
    """

    @pytest.mark.parametrize(("name", "command"), EXECUTES, ids=[name for name, _ in EXECUTES])
    def test_text_bash_executes_lands_in_the_span(
        self, name: str, command: str, stub_bin: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        assert _bash_runs_the_act(command, stub_bin, tmp_path / "log"), f"{name}: bash no longer runs the act"
        assert ACT in executed_span(command)

    @pytest.mark.parametrize(("name", "command"), READS_ONLY, ids=[name for name, _ in READS_ONLY])
    def test_text_only_read_as_data_stays_elided(
        self, name: str, command: str, stub_bin: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        assert not _bash_runs_the_act(command, stub_bin, tmp_path / "log"), f"{name}: bash now runs the act"
        assert "task complete" not in executed_span(command)

    @pytest.mark.parametrize(("name", "command"), NEVER_REDIRECTED, ids=[name for name, _ in NEVER_REDIRECTED])
    def test_an_unredirected_payload_is_untouched_by_the_reader_rule(
        self, name: str, command: str, stub_bin: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        assert not _bash_runs_the_act(command, stub_bin, tmp_path / "log"), f"{name}: bash now runs the act"
        assert "task complete" not in executed_span(command)

    @pytest.mark.parametrize(
        ("name", "program", "command"), EXECUTED_RESIDUE, ids=[name for name, _, _ in EXECUTED_RESIDUE]
    )
    def test_the_known_residue_still_executes_and_still_shows_its_program(
        self, name: str, program: str, command: str, stub_bin: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        assert _bash_runs_the_act(command, stub_bin, tmp_path / "log"), f"{name}: bash no longer runs the act"
        assert program in executed_span(command)

    @pytest.mark.parametrize(("name", "command"), EXECUTES, ids=[name for name, _ in EXECUTES])
    def test_a_continuation_at_any_position_still_keeps_what_bash_executes(
        self, name: str, command: str, stub_bin: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        r"""The generative half: mutate each entry at every position, keep what bash still runs.

        Enumerating spellings is what shipped four siblings one character apart — each pass
        closed the reported one while the generator that produced it kept producing others.
        """
        log = tmp_path / "log"
        executed = [mutant for mutant in _continuation_mutants(command) if _bash_runs_the_act(mutant, stub_bin, log)]
        dropped = [mutant for mutant in executed if ACT not in _joined(executed_span(mutant))]
        raw_missed = [mutant for mutant in executed if ACT not in executed_span(mutant) and ACT in mutant]
        assert executed, f"{name}: no mutant runs the act, so this entry pins nothing"
        assert not dropped, f"{name}: {len(dropped)} of {len(executed)} silently elided, first {dropped[0]!r}"
        assert not raw_missed, (
            f"{name}: {len(raw_missed)} of {len(executed)} reach the span only once the matcher strips "
            f"continuations, though the act is unsplit in the source — first {raw_missed[0]!r}"
        )

    @pytest.mark.parametrize(
        ("name", "command"),
        READS_ONLY + NEVER_REDIRECTED,
        ids=[name for name, _ in READS_ONLY + NEVER_REDIRECTED],
    )
    def test_a_continuation_never_makes_bash_run_a_payload(
        self, name: str, command: str, stub_bin: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """The negative control: mutating a reader leaves it a reader, and still elides.

        Without the span half this asserts only its own premise and passes at every revision
        including ``main`` — so the sweep above stays satisfiable by keeping everything, and
        the elision these entries pin is free to disappear unnoticed.
        """
        log = tmp_path / "log"
        mutants = _continuation_mutants(command)
        runs = [mutant for mutant in mutants if _bash_runs_the_act(mutant, stub_bin, log)]
        kept = [mutant for mutant in mutants if "task complete" in _joined(executed_span(mutant))]
        budget = _MUTANT_OVER_KEEP_BUDGET.get(name, 0)
        assert not runs, f"{name}: a continuation made bash execute the payload, first {runs[0]!r}"
        assert len(kept) <= budget, (
            f"{name}: {len(kept)} of {len(mutants)} mutants keep the payload, over the pinned {budget} "
            f"— first {kept[0]!r}"
        )


#: The over-keep every wrapper carries: an unquoted-delimiter heredoc body is kept by rule,
#: crossed with the five sinks that execute nothing. Over-keeping only ever reds a matcher
#: loudly, so it is budgeted rather than banned — the budget is what stops the widening
#: quietly hollowing the elision out instead.
_OVER_KEEP_BUDGET = 5

#: Wrappers over that floor, each for a stated reason. A literal newline inside the wrapper
#: makes every heredoc cell syntactically invalid, so bash runs none of them while the span
#: still keeps the text; a function body is fail-closed, so its whole reader row is kept.
_WRAPPER_OVER_KEEP_BUDGET = {
    "if-newlines": 146,
    "for-newlines": 146,
    "function-brace-body": 35,
    "function-subshell-body": 35,
    "function-keyword": 35,
}


@pytest.mark.skipif(not pathlib.Path("/bin/bash").exists(), reason="ground truth needs a real /bin/bash")
class TestTheStructuralProduct:
    """WRAPPER x READER x REDIRECTION x SINK, so an unreported spelling is PRODUCED.

    The continuation sweep is generative over one operator applied to a fixed table, and no
    table entry is a keyword compound piped to an interpreter — so no mutant of one can be.
    Adding the reported spellings as rows closes them and leaves the next cell of the same
    product open, which is how this module came to be held five times.
    """

    @pytest.mark.parametrize(("name", "wrapper"), WRAPPERS, ids=[name for name, _ in WRAPPERS])
    def test_no_cell_of_the_product_drops_text_bash_executes(
        self, name: str, wrapper: str, stub_bin: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        cases = _structural_cases(wrapper)
        assert len(cases) == len(READERS) * len(REDIRECTIONS) * len(SINK_TAILS)
        verdicts = _bash_verdicts(cases, stub_bin, tmp_path)
        spans = [executed_span(command) for _, command in cases]
        dropped = [
            (ident, command)
            for (ident, command), runs, span in zip(cases, verdicts, spans, strict=True)
            if runs and ACT not in span
        ]
        kept = [
            ident
            for (ident, _), runs, span in zip(cases, verdicts, spans, strict=True)
            if not runs and "task complete" in span
        ]
        reached_by_a_reader = {
            ident.split("|")[2]
            for (ident, _), runs in zip(cases, verdicts, strict=True)
            if runs and ident.startswith("reader|")
        }
        assert not dropped, f"{name}: {len(dropped)} of {sum(verdicts)} silently elided, first {dropped[0]}"
        assert reached_by_a_reader >= INTERPRETER_SINKS, (
            f"{name}: no plain-reader cell executes through {sorted(INTERPRETER_SINKS - reached_by_a_reader)}, "
            "so this wrapper pins nothing for them"
        )
        budget = _WRAPPER_OVER_KEEP_BUDGET.get(name, _OVER_KEEP_BUDGET)
        assert len(kept) <= budget, f"{name}: {len(kept)} over-keeps over the pinned {budget}, first {kept[0]}"


class TestAContinuationBashRemovesMovesNoVerdict:
    r"""A mutant bash reads IDENTICALLY must span identically — the property, not its instances.

    The residue was never four bugs: it was one property — decisions taken on raw bytes —
    instantiated at every site that read them, so patching a site left the next one to be
    found. This pins the property over the same corpus the sweep above mutates, and needs no
    bash: the entries' own bash verdicts are ground truth in :class:`TestBashGroundTruth`.
    """

    @pytest.mark.parametrize(
        ("name", "command"),
        EXECUTES + READS_ONLY + NEVER_REDIRECTED,
        ids=[name for name, _ in EXECUTES + READS_ONLY + NEVER_REDIRECTED],
    )
    def test_a_mutant_that_splices_back_to_the_command_spans_the_same(self, name: str, command: str) -> None:
        view = _Splice(command).text
        expected = _joined(executed_span(command))
        same = [mutant for mutant in _continuation_mutants(command) if _Splice(mutant).text == view]
        assert same, f"{name}: no mutant splices back to the command, so this entry pins nothing"
        for mutant in same:
            assert _joined(executed_span(mutant)) == expected, f"{name}: {mutant!r} moved the verdict"


def _group_onlysegment_end(text: str, start: int) -> int:
    """Where the window ended before keyword compounds were modelled — the reference scan."""
    depth = 0
    for _, char in unquoted_scan(text[:start]):
        depth += 1 if char in GROUP_OPEN else 0
        depth = max(depth - 1, 0) if char in GROUP_CLOSE else depth
    for index, char in unquoted_scan(text, start):
        if char in GROUP_OPEN:
            depth += 1
        elif char in GROUP_CLOSE:
            depth = max(depth - 1, 0)
        elif depth == 0 and (ends_a_list(text, index) or text.startswith("||", index)):
            return index
    return len(text)


class TestTheWindowOnlyEverWidens:
    """Monotonicity is what makes a NEW silent drop structurally impossible.

    The data-only proof is ``all(stage is a reader)``, so a longer window either appends to
    the LAST stage — whose command word is its first word, hence unchanged — or adds whole
    conjuncts: it turns the proof True→False, never False→True. Only a keyword rule that
    NARROWED the window could break that argument, and this is what reds when one does.
    """

    @pytest.mark.parametrize(
        ("name", "command"),
        EXECUTES + READS_ONLY + NEVER_REDIRECTED,
        ids=[name for name, _ in EXECUTES + READS_ONLY + NEVER_REDIRECTED],
    )
    def test_no_start_ends_the_window_before_the_group_only_scan_does(self, name: str, command: str) -> None:
        text = _Splice(command).text
        narrowed = [
            start
            for start in range(len(text) + 1)
            if segment_end(text, start, enclosing(text, start)) < _group_onlysegment_end(text, start)
        ]
        assert not narrowed, f"{name}: the window ends EARLIER than a group-only scan at {narrowed[:3]}"

    def test_a_keyword_compound_genuinely_widens_it(self) -> None:
        """Otherwise the bound above is satisfied by a keyword stack that does nothing."""
        text = f"if true; then cat <<<'{ACT}'; fi | bash"
        assert segment_end(text, 0, enclosing(text, 0)) > _group_onlysegment_end(text, 0)


class TestOnlyEmissionReachesTheOriginalBytes:
    """Structural: a SIXTH decision site cannot be written against the raw bytes.

    The generative sweep is empirical evidence over today's corpus; this is what stops the
    property being re-introduced by code the corpus does not reach.
    """

    @staticmethod
    def _module() -> ast.Module:
        return ast.parse(pathlib.Path(command_span.__file__).read_text(encoding="utf-8"))

    def test_the_byte_map_is_read_only_by_the_emitters(self) -> None:
        callers = {
            function.name
            for function in ast.walk(self._module())
            if isinstance(function, ast.FunctionDef)
            and any(
                isinstance(call.func, ast.Attribute) and call.func.attr == "bytes_of"
                for call in ast.walk(function)
                if isinstance(call, ast.Call)
            )
        }
        assert callers == {"_emit", "_emit_substitutions"}

    def test_the_scanner_hands_the_raw_command_to_the_splice_and_nothing_else(self) -> None:
        scanner = next(
            node for node in ast.walk(self._module()) if isinstance(node, ast.ClassDef) and node.name == "_SpanScanner"
        )
        init = next(node for node in scanner.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        raw = [node for node in ast.walk(init) if isinstance(node, ast.Name) and node.id == "command"]
        spliced = [
            call
            for call in ast.walk(init)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "_Splice"
        ]
        assert len(raw) == 1
        assert len(spliced) == 1


class TestTeethAreKept:
    """The acts the negative matcher exists to catch survive the elision."""

    @pytest.mark.parametrize(
        "command",
        [
            "t3 widget task complete 42 # mark it done and move on",
            "t3 widget ticket clear 42",
            "t3 widget task complete 42 --note 'the sub-agent was blocked'",
            "bash -c 't3 widget task complete 42'",
            'eval "$(echo t3 widget task complete 42)"',
        ],
        ids=["task-complete", "ticket-clear", "act-with-prose-note", "script-operand", "eval-substitution"],
    )
    def test_a_real_violation_still_matches(self, command: str) -> None:
        assert re.search(FORBIDDEN_ACT, executed_span(command)) is not None

    def test_the_honest_escalation_does_not_match(self) -> None:
        assert re.search(FORBIDDEN_ACT, executed_span(LIVE_ESCALATION)) is None
        assert re.search(FORBIDDEN_ACT, LIVE_ESCALATION) is not None


def _negatives_on_command_span() -> list[str]:
    """``<scenario>: <regex>`` for every negative matcher graded on the executed span."""
    named: list[str] = []
    for spec in discover_specs():
        for matcher in spec.matchers:
            alternatives = matcher.alternatives if isinstance(matcher, AnyOf) else (matcher,)
            named += [
                f"{spec.name}: {alt.value}"
                for alt in alternatives
                if isinstance(alt, Matcher) and alt.kind == "negative" and alt.arg_path == "command_span"
            ]
    return named


def test_every_view_the_corpus_names_is_registered() -> None:
    """A shipped negative naming a view with no transform grades a missing arg and never fires."""
    for spec in discover_specs():
        for matcher in spec.matchers:
            alternatives = matcher.alternatives if isinstance(matcher, AnyOf) else (matcher,)
            for alt in alternatives:
                if isinstance(alt, Matcher) and alt.arg_path in DERIVED_VIEW_NAMES:
                    assert alt.arg_path in matchers._ARG_VIEWS, f"{spec.name} names an unregistered view"


def test_exactly_one_negative_grades_the_executed_span() -> None:
    """A silent mass-conversion of the corpus onto the elided view shows up as a diff here.

    Eliding quoted payloads by DEFAULT was measured to stop 22 of 149 live
    command-negatives firing, so each adoption is a per-matcher decision, not a sweep.
    """
    adopters = _negatives_on_command_span()
    expected = f"orchestrator_escalates_blocked_subagent_result_not_swallows: {FORBIDDEN_ACT}"
    assert adopters == [expected], f"expected exactly one command_span negative, found {len(adopters)}:\n" + "\n".join(
        adopters
    )
