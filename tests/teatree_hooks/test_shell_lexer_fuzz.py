r"""Fuzz / property corpus for the hand-rolled bash tokenizer (§3g #9).

``_shell_lexer`` is an adversarial-input tokenizer maintained by hand
(ANSI-C ``$'...'`` decode, metachar splitting, heredoc-body consumption).
It sits under the quote-scanner, banned-terms, publish-surface and
self-rescue gates, so a crash or a lost source span there silently blows a
hole in every one of those gates. Example-based tests (``test_shell_lexer``)
pin specific decodings; this file pins the *structural invariants* that must
hold for **every** input — the properties a cat-and-mouse tokenizer is most
likely to regress on when a new escape or metachar case is bolted on.

The generator is a self-contained, deterministic LCG (Knuth's MMIX
constants) rather than ``random`` — a fixed-seed corpus reproduces a
regression byte-for-byte across machines and Python builds, and it keeps the
test free of the stdlib PRNG (so no crypto-suitability lint carve-out).

Invariants asserted here, empirically validated to hold over the generated
corpus and required by the lexer's consumers.

Totality — ``tokenize`` never raises on any byte soup. An adversary controls
the command string; an unhandled ``IndexError`` mid-scan would crash the
PreToolUse hook and (fail-open) wave the command through.

Populated span — every emitted token carries a non-empty ``raw``. The
self-rescue env-prefix strip and the publish classifier read ``raw``
verbatim; an empty span desynchronises them.

Ordered, non-overlapping spans — each token's ``raw`` is locatable in the
source at or after the previous token's span. The tokens are a left-to-right
cover of the input, never a reordering or a fabrication.

Determinism — tokenizing the same string twice yields equal tokens.

Downstream totality — ``split_commands``, ``is_command_separator`` and
``raw_substitution_sees_live`` never raise on the lexer's own output, and
every token in a split segment is drawn from the original token list.
"""

import pytest

from teatree.hooks._shell_lexer import Token, is_command_separator, raw_substitution_sees_live, split_commands, tokenize

# A metachar-dense alphabet: single chars plus the multi-char openers that
# drive the lexer's hardest branches (command/arith substitution, ANSI-C
# quoting, heredoc, boolean operators, backtick). Sampling uniformly from
# this set produces the malformed, half-open, deeply-nested strings that a
# hand-rolled scanner is most likely to choke on.
_ALPHABET: tuple[str, ...] = (
    "a", "b", "c", " ", "\t", "\n", "\r", ";", "|", "&", "<", ">",
    "(", ")", "'", '"', "$", "\\", "{", "}", "=", "#",
    "$(", "`", "<<", "<<-", "$'", "&&", "||", "2>&1", "'\\''",
)  # fmt: skip

_FUZZ_ITERATIONS = 4000
_MAX_TOKENS = 60
_SEED = 0xC0FFEE
_MASK64 = (1 << 64) - 1


class _Lcg:
    """A tiny deterministic PRNG (Knuth MMIX LCG) — reproducible, import-free."""

    def __init__(self, seed: int) -> None:
        self._state = seed & _MASK64

    def next_int(self, bound: int) -> int:
        """A pseudo-random int in ``[0, bound)`` (bound assumed positive)."""
        self._state = (self._state * 6364136223846793005 + 1442695040888963407) & _MASK64
        # Use the high bits (better distributed than the low bits of an LCG).
        return (self._state >> 33) % bound

    def command(self) -> str:
        length = self.next_int(_MAX_TOKENS + 1)
        return "".join(_ALPHABET[self.next_int(len(_ALPHABET))] for _ in range(length))


def _assert_ordered_nonoverlapping_spans(command: str, tokens: list[Token]) -> None:
    """Every ``raw`` is findable in ``command`` at/after the prior span end."""
    cursor = 0
    for tok in tokens:
        assert tok.raw != "", f"empty raw span in {command!r}"
        idx = command.find(tok.raw, cursor)
        assert idx >= 0, f"raw {tok.raw!r} not found from {cursor} in {command!r}"
        cursor = idx + len(tok.raw)


def _assert_all_invariants(command: str) -> None:
    tokens = tokenize(command)  # totality — must not raise
    _assert_ordered_nonoverlapping_spans(command, tokens)

    # Determinism: a second pass yields structurally equal tokens.
    assert tokenize(command) == tokens

    # Downstream consumers never raise and stay consistent with the tokens.
    segments = split_commands(tokens)
    assert isinstance(segments, list)
    original = {id(tok) for tok in tokens}
    for segment in segments:
        assert isinstance(segment, list)
        for tok in segment:
            assert id(tok) in original, "split_commands fabricated a token"
    for tok in tokens:
        # These are plain predicates over a token; assert only that they are
        # total (no raise) — their semantics are pinned in the example tests.
        assert isinstance(is_command_separator(tok), bool)
    assert isinstance(raw_substitution_sees_live(command, ("$(", "`", "${")), bool)


class TestShellLexerFuzzCorpus:
    def test_seeded_fuzz_preserves_invariants(self) -> None:
        """A seeded (reproducible) sweep of metachar-dense byte soup.

        The seed is fixed so a regression reproduces deterministically; the
        alphabet is metachar-heavy so the hard lexer branches actually fire.
        """
        rng = _Lcg(_SEED)
        for _ in range(_FUZZ_ITERATIONS):
            _assert_all_invariants(rng.command())

    def test_generator_is_deterministic_across_runs(self) -> None:
        """Two LCGs from the same seed emit an identical corpus (reproducible)."""
        a = _Lcg(_SEED)
        b = _Lcg(_SEED)
        assert [a.command() for _ in range(50)] == [b.command() for _ in range(50)]

    # Curated adversarial strings — the shapes a hand-rolled bash tokenizer
    # historically mishandles: half-open constructs, escape-at-EOF, deeply
    # nested substitution, mixed line endings, heredoc-looking fragments.
    @pytest.mark.parametrize(
        "command",
        [
            "",
            " ",
            "\\",  # lone trailing backslash (line-continuation with no next line)
            "\\\n",  # backslash-newline (line continuation) at EOF
            "'",  # unterminated single quote
            '"',  # unterminated double quote
            "$'",  # unterminated ANSI-C quote
            "$'\\x",  # truncated ANSI-C hex escape
            "$'\\u12",  # truncated ANSI-C unicode escape
            "$'\\0",  # truncated ANSI-C octal escape
            "$'\\'",  # escaped quote inside ANSI-C, then EOF
            "`",  # lone backtick
            "$(",  # unterminated command substitution
            "$((",  # unterminated arithmetic expansion
            "${",  # unterminated parameter expansion
            "$($($(",  # deeply nested, all unterminated
            "a$(b`c$'d'e`f)g",  # nested sub + backtick + ansi-c interleaved
            "<<",  # bare heredoc operator, no delimiter
            "cat <<'EOF'",  # heredoc with no body and no closing delimiter
            "cat <<-EOF\n\tbody\n\tEOF",  # tab-stripped heredoc
            "cat << EOF\nline1\nline2\nEOF\nnext",  # full heredoc then a command
            "a;;;|||&&&\n\n\r\r",  # metachar storm
            "'A'=1",  # quoted assignment-shaped word (the raw invariant case)
            "x 2>&1 >>log <in |& y",  # redirect-ish words and operators
            "\r\n\r\n",  # only CRLF operators
            "#comment only",  # a pure comment line
            "a #trailing comment\nb",  # comment then a real command
            "$'\\xff\\377\\u00e9\\U0001F600'",  # dense valid ANSI-C escapes
        ],
    )
    def test_adversarial_strings_hold_invariants(self, command: str) -> None:
        _assert_all_invariants(command)

    def test_long_metachar_run_does_not_blow_the_stack(self) -> None:
        """A long run of nested openers must scan iteratively, not recurse to death."""
        _assert_all_invariants("$(" * 500)
        _assert_all_invariants("`" * 500)
        _assert_all_invariants("a | " * 500)
