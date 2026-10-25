"""Script text a command hands to a nested shell."""

import pytest

from teatree.core.gates.shell_payloads import here_strings, heredoc_scripts, piped_shell_payloads
from teatree.hooks._shell_lexer import tokenize


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("true && bash <<EOF\nrun\nEOF", ["run\n"]),
        ("cat <<'EOF' | sh\nrun\nEOF", ["run\n"]),
        ("bash <<-EOF\n\trun\n\tEOF", ["\trun\n"]),
        ("bash <<EOF\nrun\nEOF \nmore\nEOF", ["run\nEOF \nmore\n"]),
        ("cat <<A; bash <<B\none\nA\ntwo\nB", ["one\n", "two\n"]),
        ("bash <<EOF\nunterminated", ["unterminated"]),
        ("cat <<EOF\nrun\nEOF", []),
        ("cat <<A\nbash <<B\nrun\nB\nA", []),
    ],
)
def test_heredoc_scripts_are_the_bodies_a_shell_on_the_head_line_may_read(command: str, expected: list[str]) -> None:
    assert heredoc_scripts(command) == expected


def test_here_strings_take_the_glued_or_following_word() -> None:
    assert here_strings(["bash", "<<<run a", "<<<", "run b"], ["bash", "<<<'run a'", "<<<", "'run b'"]) == [
        "run a",
        "run b",
    ]


def test_a_trailing_here_string_operator_has_no_payload() -> None:
    assert here_strings(["bash", "<<<"], ["bash", "<<<"]) == []


def test_piped_echo_into_a_shell_is_a_payload() -> None:
    assert piped_shell_payloads(tokenize("printf '%s' 'git x' | bash")) == ["git x"]
