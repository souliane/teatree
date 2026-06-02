"""Tests for the destination-KIND classifier (#1530).

:func:`teatree.hooks.publish_destination_kind.classify_bash_destination`
decides whether a Bash publish lands on an EXTERNAL FORGE (the forge
auto-links bare refs -> the bare-reference gate must NOT fire) or a
USER-FACING surface (a human reads it raw -> the gate enforces). The
classification is fail-safe toward user-facing: a single user-facing or
unclassifiable publish segment makes the whole command user-facing.
"""

import pytest

from teatree.hooks.publish_destination_kind import DestinationKind, classify_bash_destination


class TestExternalForge:
    @pytest.mark.parametrize(
        "command",
        [
            'gh pr create --title t --body "see #1764"',
            'gh issue create --title t --body "b"',
            "gh issue comment 5 --body x",
            "gh pr comment 5 --body x",
            "gh pr edit 5 --body x",
            'glab mr create --title t --description "d"',
            "glab mr note 5 --message x",
            "glab issue create --title t",
            "glab mr update 5 --description x",
            # A forge ``api`` WRITE (effective POST) is an external-forge surface.
            "gh api repos/o/r/issues/5/comments -f body='see #1764'",
            "glab api projects/g%2Fp/issues -f body='x'",
            "gh api repos/o/r/issues --input -",
            # Behind a benign ``cd`` prefix the segment still classifies.
            'cd /repo && gh pr create --body "x"',
            # t3 forge wrappers.
            "t3 teatree review post-comment 5 --body x",
            "t3 teatree ticket create-issue --title t",
        ],
    )
    def test_classifies_external_forge(self, command: str) -> None:
        assert classify_bash_destination(command) is DestinationKind.EXTERNAL_FORGE


class TestUserFacing:
    @pytest.mark.parametrize(
        "command",
        [
            "git commit -m 'see #1764'",
            "t3 teatree notify send 'see #1764' --idempotency-key k",
            "t3 teatree slack react C123 1.2 eyes",
            'curl -d \'{"text":"#1764"}\' https://slack.com/api/chat.postMessage',
            # Unclassifiable / non-publish.
            "ls -la",
            "echo hello",
            "",
        ],
    )
    def test_classifies_user_facing(self, command: str) -> None:
        assert classify_bash_destination(command) is DestinationKind.USER_FACING

    def test_read_only_forge_api_is_user_facing_default(self) -> None:
        # A read-only ``gh api`` GET is not an external-forge WRITE; with no
        # forge post segment it falls back to user-facing (and is harmless —
        # a read carries no body the gate would scan).
        assert classify_bash_destination("gh api repos/o/r/commits/main") is DestinationKind.USER_FACING

    def test_forge_post_chained_with_user_facing_is_user_facing(self) -> None:
        # A user-facing segment anywhere demotes the whole command (fail-safe):
        # a chained ``git commit`` body must not ride the forge relaxation.
        command = "gh pr create --body x && git commit -m 'see #1764'"
        assert classify_bash_destination(command) is DestinationKind.USER_FACING

    def test_forge_post_chained_with_inert_segment_stays_forge(self) -> None:
        # An inert ``cd``/``echo`` segment is neither user-facing nor forge; a
        # forge post beside it still classifies external-forge.
        command = 'echo start && gh pr create --body "see #1764"'
        assert classify_bash_destination(command) is DestinationKind.EXTERNAL_FORGE
