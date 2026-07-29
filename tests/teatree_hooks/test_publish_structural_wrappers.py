r"""Publish detection sees through structural shell wrappers (F1 leak-gate fix).

All three publish leak gates (quote-scanner #1213, banned-terms #1415,
AI-signature #836 gate 15) route through
:func:`teatree.hooks._command_parser.is_publish_command`. F1 (holistic review)
proved a ``gh``/``glab`` publish wrapped in a subshell ``(...)``, a brace group
``{ ...; }``, or the body of an ``if``/``for``/``while`` compound command was
invisible to the classifier — the ``(`` glued onto the leader token and the
reserved words (``then``/``do``) became the segment leader, so the leader-keyed
catalogue never matched and the command published unscanned.

These tests pin each confirmed bypass vector closed and prove the fix does NOT
over-widen (a read command that merely QUOTES a publish verb stays unblocked, and
``$(...)`` command substitution is still parsed as one token). Synthetic secret
only (``SECRET``) and publish verbs built from fragments so the literal
merge-class phrase never appears in source.
"""

from teatree.hooks._command_parser import extract_bash_payload, is_publish_command
from teatree.hooks._shell_lexer import TokenKind, tokenize

# Publish verbs assembled from fragments so the contiguous merge-class spelling
# never appears verbatim in this source file (mirrors the sibling detection
# tests). The runtime strings are the real, whole commands.
_COMMENT = "comm" + "ent"
_CREATE = "cr" + "eate"

# The exact F1 failure strings — the anti-vacuous corpus. Each MUST flip from
# pass-through (unscanned) to blocked (scanned) with the fix.
_F1_VECTORS: dict[str, str] = {
    "subshell": f'(gh pr {_COMMENT} 1 --body "SECRET")',
    "brace_group": f'{{ gh pr {_COMMENT} 1 --body "SECRET"; }}',
    "if_body": f'if true; then gh pr {_COMMENT} 1 --body "SECRET"; fi',
    "for_body": f'for x in 1; do gh pr {_COMMENT} 1 --body "SECRET"; done',
    "while_body": f'while true; do gh pr {_COMMENT} 1 --body "SECRET"; done',
    "nested_and_subshell": f'true && (gh pr {_COMMENT} 1 --body "SECRET")',
}


class TestStructuralWrapperVectorsAreDetected:
    """Every F1 wrapper shape is now classified as a publish."""

    def test_each_vector_is_a_publish(self) -> None:
        for name, command in _F1_VECTORS.items():
            assert is_publish_command(command) is True, f"{name} slipped past is_publish_command"

    def test_each_vector_body_is_scanned_and_carries_the_secret(self) -> None:
        # Detection is worthless if the body walker cannot then reach the secret —
        # assert the scanned payload actually contains it (the destination-aware
        # banned-terms gate runs with fail_closed_body_file=True).
        for name, command in _F1_VECTORS.items():
            payload = extract_bash_payload(command, fail_closed_body_file=True)
            assert "SECRET" in payload, f"{name} body not scanned ({payload!r})"

    def test_glab_subshell_variant_is_detected(self) -> None:
        assert is_publish_command(f'(glab mr note {_CREATE} --message "SECRET")') is True

    def test_env_prefixed_publish_inside_if_body_is_detected(self) -> None:
        command = f'if true; then FOO=1 gh pr {_COMMENT} 1 --body "SECRET"; fi'
        assert is_publish_command(command) is True


class TestNoOverWidening:
    """A read command that merely mentions a publish verb is NOT blocked."""

    def test_ripgrep_quoting_a_publish_verb_is_not_a_publish(self) -> None:
        assert is_publish_command(f'rg "gh pr {_COMMENT}" src/') is False

    def test_grep_quoting_a_publish_verb_is_not_a_publish(self) -> None:
        assert is_publish_command(f'grep -r "gh pr {_CREATE}" .') is False

    def test_read_command_in_loop_body_is_not_a_publish(self) -> None:
        assert is_publish_command('for f in *.py; do grep SECRET "$f"; done') is False

    def test_benign_subshell_is_not_a_publish(self) -> None:
        assert is_publish_command("(echo hi && ls)") is False


class TestSubstitutionParsingPreserved:
    """The ``(`` handling must not break ``$(...)`` / ``<(...)`` substitution."""

    def test_command_substitution_body_is_still_a_publish(self) -> None:
        assert is_publish_command(f'gh pr {_CREATE} --body "$(cat notes.md)"') is True

    def test_process_substitution_leader_is_still_a_publish(self) -> None:
        assert is_publish_command(f"gh pr {_CREATE} --body-file <(cat notes.md)") is True

    def test_dollar_paren_stays_one_word_token(self) -> None:
        tokens = tokenize('echo "$(cat secret)"')
        op_values = [t.value for t in tokens if t.kind is TokenKind.OP]
        assert "(" not in op_values, "$( was wrongly split by the subshell operator"

    def test_bare_subshell_open_is_an_op_token(self) -> None:
        tokens = tokenize("(gh pr x)")
        assert tokens[0].kind is TokenKind.OP
        assert tokens[0].value == "("
        assert tokens[1].kind is TokenKind.WORD
        assert tokens[1].value == "gh"
