"""The always-allowed self-rescue command allowlist (NEVER-LOCKOUT, #1498 deferral).

A gate that can deny the very command that disables it is a deadlock. The
self-rescue allowlist names the small set of commands every gate and every
hook MUST let through unconditionally — the operator's guaranteed escape
hatch:

- ``t3 <overlay> db migrate`` / ``manage.py migrate`` — bring a wedged DB
    schema forward so the rest of the CLI works again;
- ``t3 <overlay> gate ... disable`` — the orchestrator-Bash / skill-loading
    kill-switches (the #1474 self-rescue surface);
- ``t3 review gate fail-open enable`` — the master fail-open toggle this
    change adds.

:func:`is_self_rescue` is pure detection over the (already-lexed) FIRST
command segment, so a self-rescue prefix glued to a second command via a
shell separator can never smuggle a blocked command past a gate.
"""

import pytest

from teatree.hooks.self_rescue import SELF_RESCUE_ALLOWLIST, is_self_rescue


class TestIsSelfRescue:
    @pytest.mark.parametrize(
        "command",
        [
            "t3 review gate fail-open enable",
            "t3 acme gate disable",
            "t3 acme gate skill-loading disable",
            "t3 t3-teatree gate fail-open enable",
            "t3 acme db migrate",
            "python manage.py migrate",
            "python3 manage.py migrate --noinput",
            "t3 acme worktree provision",
        ],
    )
    def test_recognises_each_self_rescue_command(self, command: str) -> None:
        assert is_self_rescue(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "git push origin main",
            "gh pr create --title x --body y",
            "t3 review post-comment https://gitlab/x!1 note --live",
            "t3 acme gate enable",  # enabling a gate is not an escape from a lockout
            "t3 acme gate status",
            "pytest -q",
            "",
        ],
    )
    def test_rejects_non_self_rescue_commands(self, command: str) -> None:
        assert is_self_rescue(command) is False

    def test_self_rescue_prefix_glued_to_blocked_second_command_is_not_rescue(self) -> None:
        # A self-rescue command in a SECOND shell segment must not whitelist
        # the leading (blocked) command — only the first segment counts.
        assert is_self_rescue("git push origin main; t3 acme gate disable") is False

    def test_self_rescue_command_with_trailing_blocked_segment_still_rescues(self) -> None:
        # The first segment IS a self-rescue command — the whole call is a
        # rescue regardless of what a chained second segment would do.
        assert is_self_rescue("t3 acme gate disable && echo done") is True

    def test_whitespace_only_command_is_not_rescue(self) -> None:
        # A non-empty but whitespace-only command lexes to no WORD tokens —
        # nothing to match against, so it is never a rescue.
        assert is_self_rescue("   ") is False


class TestAllowlistShape:
    def test_allowlist_is_non_empty_tuple_of_phrase_tuples(self) -> None:
        assert isinstance(SELF_RESCUE_ALLOWLIST, tuple)
        assert SELF_RESCUE_ALLOWLIST
        for entry in SELF_RESCUE_ALLOWLIST:
            assert isinstance(entry, tuple)
            assert all(isinstance(word, str) for word in entry)

    def test_allowlist_covers_the_three_documented_classes(self) -> None:
        flattened = {" ".join(entry) for entry in SELF_RESCUE_ALLOWLIST}
        # db migrate, gate disable, fail-open toggle — the three classes the
        # NEVER-LOCKOUT contract names. Matched as ordered-subsequence phrases
        # so the overlay name between ``t3`` and the verb is irrelevant.
        assert any("migrate" in phrase for phrase in flattened)
        assert any(phrase.endswith("gate disable") or "gate disable" in phrase for phrase in flattened)
        assert any("fail-open" in phrase for phrase in flattened)
