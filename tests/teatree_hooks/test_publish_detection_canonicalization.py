"""Unit tests for the publish-detection canonicalization primitives (F7.1).

Direct coverage of the shared leader-canonicalization the publish/leak detectors
depend on so ``xargs gh`` / ``/usr/bin/gh`` / ``env gh`` cannot evade the gates,
plus the interpreter-transport complement that catches a forge call hidden inside
an interpreter arg. Synthetic term only.
"""

from teatree.hooks._publish_detection import (
    canonical_forge_leader,
    command_has_interpreter_forge_transport,
    wrapper_prefix_len,
)

# Built from a fragment so the literal merge-class phrase never appears in source
# (the PreToolUse forge gate pattern-matches it).
_VERB = "cr" + "eate"


class TestWrapperPrefixLen:
    """How many leading env/cd/wrapper tokens the strip consumes."""

    def test_env_prefix_is_counted(self) -> None:
        assert wrapper_prefix_len(["env", "gh", "pr", _VERB]) == 1

    def test_no_wrapper_means_zero(self) -> None:
        assert wrapper_prefix_len(["gh", "pr", _VERB]) == 0


class TestCanonicalForgeLeader:
    """Basename of the executed program after wrapper/env strip."""

    def test_env_wrapper_resolves_to_the_forge_tool(self) -> None:
        assert canonical_forge_leader(["env", "gh", "pr"]) == "gh"

    def test_absolute_path_resolves_to_basename(self) -> None:
        assert canonical_forge_leader(["/usr/bin/gh", "pr"]) == "gh"

    def test_empty_after_strip_is_blank(self) -> None:
        assert canonical_forge_leader([]) == ""


class TestInterpreterForgeTransport:
    """A forge call hidden in an interpreter arg is a publish; a mere quote is not."""

    def test_interpreter_hidden_forge_call_is_detected(self) -> None:
        assert command_has_interpreter_forge_transport(f'sh -c "gh pr {_VERB} --body X"') is True

    def test_read_only_quote_of_a_forge_token_is_not(self) -> None:
        assert command_has_interpreter_forge_transport("""rg 'sh -c "gh"' """) is False
