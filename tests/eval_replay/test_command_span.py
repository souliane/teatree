"""The executed span of a shell command — what the shell runs, payloads elided."""

import re

import pytest

from teatree.eval import matchers
from teatree.eval.command_span import executed_span
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
        ],
        ids=["bash-stdin", "python-stdin", "piped-to-bash", "absolute-interpreter-path"],
    )
    def test_a_body_an_interpreter_runs_stays_matchable(self, command: str) -> None:
        assert "t3 widget task complete 42" in executed_span(command)

    def test_an_operator_ending_the_command_has_no_body_to_drain(self) -> None:
        assert executed_span("bash <<'EOF'") == "bash <<'EOF'"

    def test_a_body_a_plain_command_consumes_is_still_elided(self) -> None:
        command = "t3 notify send --stdin <<'EOF'\nI have not marked the task complete.\nEOF\n"
        assert "task complete" not in executed_span(command)


class TestHerestrings:
    def test_the_operator_is_consumed_whole_so_the_next_line_survives(self) -> None:
        command = "grep x <<<'foo'\nt3 widget task complete 42\nfoo\necho after\n"
        span = executed_span(command)
        assert "t3 widget task complete 42" in span
        assert "echo after" in span


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
