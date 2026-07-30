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

:func:`is_self_rescue` is pure detection over EVERY shell segment of the
command, with a STRICT contiguous positional match per segment: the call
rescues only when every segment is itself a self-rescue command and nothing
else. A blocked command can be smuggled neither across a shell separator
(``&&``/``;``/``|``/``||``/newline), nor via an embedded command/process
substitution (``$(...)`` / backticks / ``<(...)``), nor as a leading,
interior, or trailing positional within a single segment.
"""

import pytest

from teatree.hooks.self_rescue import OVERLAY, SELF_RESCUE_ALLOWLIST, is_self_rescue

# An overlay-name token used to instantiate the OVERLAY wildcard slot when
# rendering an allowlist entry to a runnable command string.
_OVERLAY_NAME = "acme"


def _entry_to_command(entry: tuple[object, ...]) -> str:
    """Render an allowlist entry to a runnable command string.

    The OVERLAY wildcard becomes a concrete overlay name; ``manage.py`` is
    run via ``python`` so the rendered string is a real invocation.
    """
    parts = [_OVERLAY_NAME if token is OVERLAY else str(token) for token in entry]
    if parts and parts[0] == "manage.py":
        parts = ["python", *parts]
    return " ".join(parts)


class TestIsSelfRescue:
    @pytest.mark.parametrize(
        "command",
        [
            "t3 review gate fail-open enable",
            "t3 acme gate disable",
            "t3 acme gate skill-loading disable",
            "t3 acme gate config-overwrite disable",
            "t3 acme gate main-clone disable",  # #2844 #3 — main-clone kill-switch self-rescue
            "t3 t3-teatree gate main-clone disable",
            "t3 acme gate raw-merge disable",  # FIX-EXPEDITE PART B — raw-merge kill-switch self-rescue
            "t3 t3-teatree gate raw-merge disable",
            "t3 t3-teatree gate fail-open enable",
            "t3 acme db migrate",
            "python manage.py migrate",
            "python3 manage.py migrate --noinput",
            "t3 acme worktree provision",
            "FOO=bar t3 acme gate disable",  # legit KEY=val env-assignment prefix
            "A=1 B=2 t3 acme gate disable",  # multiple bare assignments
            "'t3' acme gate disable",  # quoted argv[0] decodes to t3 — shell runs it as t3
            '"t3" acme gate disable',
            "t''3 acme gate disable",  # interior quotes decode away to t3
        ],
    )
    def test_recognises_each_self_rescue_command(self, command: str) -> None:
        assert is_self_rescue(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            # A QUOTED pseudo-assignment is NOT an env assignment in the shell —
            # the word decodes to ``A=1`` but bash runs it as a command literally
            # named ``A=1``. The matcher must mirror the shell: do not strip it,
            # so it becomes argv[0] and the segment is not a rescue.
            "'A'=1 t3 acme gate disable",
            '"A"=1 t3 acme gate disable',
            "A''=1 t3 acme gate disable",
            'A""=1 t3 acme gate disable',
            "'A=1' t3 acme gate disable",  # whole word quoted — literal command 'A=1'
        ],
    )
    def test_quoted_pseudo_assignment_prefix_is_not_stripped(self, command: str) -> None:
        assert is_self_rescue(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "git push origin main",
            "gh pr create --title x --body y",
            "t3 review post-comment https://gitlab/x!1 note --live",
            "t3 acme gate enable",  # enabling a gate is not an escape from a lockout
            "t3 acme gate status",
            "t3 acme gate",  # truncated — segment runs out before the entry completes
            "t3 acme",  # overlay slot fills but no verb follows
            "pytest -q",
            "",
        ],
    )
    def test_rejects_non_self_rescue_commands(self, command: str) -> None:
        assert is_self_rescue(command) is False

    def test_self_rescue_prefix_glued_to_blocked_second_command_is_not_rescue(self) -> None:
        # A self-rescue command in a SECOND shell segment must not whitelist
        # the leading (blocked) command.
        assert is_self_rescue("git push origin main; t3 acme gate disable") is False

    def test_raw_merge_disable_chained_to_raw_merge_is_not_rescue(self) -> None:
        # FIX-EXPEDITE PART B: the raw-merge kill-switch glued to a raw merge
        # must NOT rescue the raw merge — the second segment is not a rescue.
        assert is_self_rescue("t3 acme gate raw-merge disable && gh pr merge 7 --squash") is False

    @pytest.mark.parametrize(
        "command",
        [
            # The rescue tokens trail a DIFFERENT leading command in the SAME
            # segment (no shell separator), so the ordered-subsequence tail
            # must NOT match — the segment's command is git/python/env, not a
            # rescue. The match is anchored at the segment's command head.
            "git push origin main t3 gate disable",
            "env git push origin main t3 gate disable",
            "python -c pass manage.py migrate",
            "git commit -m t3 db migrate",
            "env t3 acme gate disable",  # ``env <cmd>`` is a different program
            "echo t3 acme gate disable",
        ],
    )
    def test_blocked_command_cannot_smuggle_rescue_tokens_in_same_segment(self, command: str) -> None:
        assert is_self_rescue(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "t3 acme gate disable && git push origin main",
            "t3 acme gate disable; git push origin main",
            "t3 acme gate disable | tee /tmp/x",
            "t3 acme gate disable || git push origin main",
            "t3 acme gate disable\ngit push origin main",
            "t3 acme gate disable && echo done",
        ],
    )
    def test_self_rescue_prefix_glued_to_trailing_command_is_not_rescue(self, command: str) -> None:
        # A self-rescue first segment glued to ANY second segment is not a
        # rescue: the chained command (here a blocked ``git push``) must not
        # ride in on the self-rescue prefix. Every segment must itself be a
        # self-rescue command for the whole call to rescue.
        assert is_self_rescue(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "t3 acme gate disable $(git push origin main)",
            "t3 acme gate disable `git push origin main`",
            "t3 acme db migrate $(rm -rf /)",
            "t3 acme gate disable <(git push origin main)",
            "t3 acme gate disable >(tee /tmp/x)",
            "t3 acme db migrate && echo `whoami`",
        ],
    )
    def test_self_rescue_command_with_embedded_substitution_is_not_rescue(self, command: str) -> None:
        # A command/process substitution embeds an arbitrary command we cannot
        # vet, so a self-rescue command carrying one is not a rescue — the
        # embedded command must not ride in.
        assert is_self_rescue(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            # Self-rescue matching does NOT support I/O redirects by design: a
            # rescue is the bare command plus flags. EVERY redirect form —
            # valid-per-zsh or not — makes the segment a non-rescue. Rejecting
            # is strictly safer (the operator still runs the bare command) and
            # keeps the security matcher free of shell-redirect-grammar parsing.
            #
            # Basic redirects with a target.
            "t3 acme gate disable > /tmp/out",
            "t3 acme gate disable >> /tmp/out",
            "t3 acme db migrate < /tmp/in",
            "t3 acme gate disable 2> /tmp/err",
            "t3 acme gate disable 2>> /tmp/err",
            "t3 acme gate disable <> /tmp/io",
            "t3 acme gate disable <<< /tmp/here",
            "t3 acme gate disable >/tmp/out",  # attached target
            # Combined stdout+stderr and fd-duplication (valid zsh, still rejected).
            "t3 acme gate disable &> /tmp/all",
            "t3 acme gate disable &>> /tmp/all",
            "t3 acme gate disable 2>&1",
            "t3 acme gate disable 1>&2",
            "t3 acme gate disable >&2",
            "t3 acme gate disable 2>&-",
            "t3 acme gate disable >& /tmp/all",
            "t3 acme gate disable <&0",
            "t3 acme gate disable >&git",
            # Targetless bare operators (parse errors in zsh).
            "t3 acme gate disable >",
            "t3 acme gate disable >>",
            "t3 acme gate disable <",
            "t3 acme gate disable 2>",
            "t3 acme gate disable <<<",
            "t3 acme gate disable &>",
            # ``&``-after-heredoc forms (parse errors in zsh).
            "t3 acme gate disable <<&EOF",
            "t3 acme gate disable <>& /dev/null",
            # A redirect operator in a value-flag's value position must not be
            # mistaken for the flag's value — the segment is still rejected.
            "t3 acme gate disable --reason >",
            # A redirect operator glued into an env-assignment VALUE must not
            # be hidden from the redirect-rejection by the env-prefix strip:
            # the token is not a clean assignment, so it falls through to
            # ``argv[0]`` and the segment is rejected (round 9).
            "FOO=> t3 acme gate disable",
            "FOO=>> t3 acme gate disable",
            "FOO=2>&1 t3 acme gate disable",
            "FOO=&> t3 acme gate disable",
            "FOO=<<<x t3 acme gate disable",
            "A=ok FOO=> t3 acme gate disable",  # clean prefix then a redirect-bearing one
            # A redirect glued into a value-flag's value is split off as its
            # own token by the operator-aware tokenization and then rejected
            # (round 10) — the redirect no longer hides behind the leading
            # ``F`` of ``FOO=>``.
            "t3 acme gate disable --reason FOO=>",
        ],
    )
    def test_redirects_are_unsupported_by_design_and_rejected(self, command: str) -> None:
        assert is_self_rescue(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            # A lone ``-`` / ``--`` is a positional, not a flag, so it rejects
            # like any other bare positional after the entry (round 10).
            "t3 acme gate disable -",
            "t3 acme gate disable --",
            "t3 acme gate disable - --yes",
            # An env-assignment value with a further ``=`` is not a clean
            # assignment, so the token is not stripped — it becomes ``argv[0]``
            # and the segment is rejected (round 10).
            "FOO=bar= t3 acme gate disable",
            "FOO=bar=baz t3 acme gate disable",
        ],
    )
    def test_lone_dash_and_double_equals_value_are_not_rescue(self, command: str) -> None:
        assert is_self_rescue(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "FOO=bar t3 acme gate disable",  # plain assignment prefix
            "FOO+=bar t3 acme gate disable",  # append-assignment prefix
            "A=1 B+=2 t3 acme gate disable",  # mixed plain + append run
            "FOO=bar-baz t3 acme gate disable",  # value with a dash
            "PATH=/usr/bin t3 acme gate disable",  # value with slashes
            "FOO= t3 acme gate disable",  # empty value
        ],
    )
    def test_env_assignment_prefix_including_append_still_rescues(self, command: str) -> None:
        # A leading run of unquoted ``NAME=val`` / ``NAME+=val`` assignments
        # whose value is a PLAIN word (no redirect/operator chars) is shell
        # env-prefix and is stripped; the real ``argv[0]`` follows.
        assert is_self_rescue(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            # A redirect followed by a bare positional is still a smuggle —
            # the trailing command word is not a redirect target.
            "t3 acme gate disable 2>&1 git push",
            "t3 acme gate disable > /tmp/out git push",
        ],
    )
    def test_redirect_then_bare_positional_is_not_rescue(self, command: str) -> None:
        assert is_self_rescue(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            # A blocked command (``git push``) sits BETWEEN the overlay slot
            # and the verb — the strict contiguous match must reject it; the
            # rescue tokens are not a free-floating subsequence.
            "t3 acme git push gate disable",
            "t3 review git push gate fail-open enable",
            "t3 review post-comment https://example.invalid gate disable --live",
        ],
    )
    def test_blocked_command_smuggled_between_entry_tokens_is_not_rescue(self, command: str) -> None:
        assert is_self_rescue(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            # A bare positional token TRAILS the matched rescue entry — a
            # self-rescue command takes no positional arguments, so a trailing
            # command word / URL / path rejects the segment.
            "t3 acme gate disable git push",
            "t3 acme gate disable https://example.invalid",
            "t3 acme db migrate rm -rf /tmp/x",
            "t3 acme gate fail-open enable git push",
            "t3 acme worktree provision git push",
        ],
    )
    def test_bare_positional_trailing_the_entry_is_not_rescue(self, command: str) -> None:
        assert is_self_rescue(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "t3 acme gate disable --yes",
            "t3 acme gate skill-loading disable --yes",
            "t3 acme gate fail-open enable --force",
            "t3 acme db migrate --no-input",
            "t3 acme gate disable --reason wedged",  # value-flag + plain-word value
            "t3 acme gate disable --reason cleanup --yes",  # value then another flag
        ],
    )
    def test_trailing_flags_after_the_entry_still_rescue(self, command: str) -> None:
        # Only ``-``-prefixed flags (and a recognised value-flag's PLAIN-word
        # value) are allowed after the entry — these stay a rescue.
        assert is_self_rescue(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "t3 acme gate disable --reason=cleanup",  # glued value-flag, plain value
            "t3 acme gate disable --reason=",  # glued EMPTY value — ``--reason ''``
            "t3 acme gate disable --reason cleanup",  # separate value-flag form
        ],
    )
    def test_value_flag_in_glued_and_separate_forms_still_rescues(self, command: str) -> None:
        # A recognised value-flag is allowed in both ``--name value`` and glued
        # ``--name=value`` forms (including an empty glued value). These pass via
        # the CLOSED trailing-token policy (a matched flag SHAPE), not a
        # permissive ``any --``-prefixed-token catch-all.
        assert is_self_rescue(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            # A glued value-flag whose VALUE carries a shell operator / redirect
            # / substitution character must reject under the closed policy. The
            # operator-aware lexer already splits most of these into their own
            # tokens; the glued-value regex is the closing backstop.
            "t3 acme gate disable --reason=a;b",
            "t3 acme gate disable --reason=a|b",
            "t3 acme gate disable --reason=>",
            "t3 acme gate disable --reason=$(x)",
            "t3 acme gate disable --reason=`x`",
            "t3 acme gate disable --reason=a&b",
        ],
    )
    def test_glued_value_flag_with_operator_value_is_not_rescue(self, command: str) -> None:
        assert is_self_rescue(command) is False

    def test_trailing_whitespace_after_a_rescue_still_rescues(self) -> None:
        # A trailing space is stripped by the shell (and by the lexer, which
        # emits no empty WORD token), so it is BENIGN: the command is the bare
        # rescue. Asserted explicitly so the intentional acceptance is pinned.
        assert is_self_rescue("t3 acme gate disable ") is True

    def test_chain_of_only_self_rescue_segments_still_rescues(self) -> None:
        # Every segment is itself a self-rescue command — the whole call is a
        # rescue. Self-rescue must stay always-allowed for the genuine case.
        assert is_self_rescue("t3 acme gate disable && t3 acme db migrate") is True

    def test_whitespace_only_command_is_not_rescue(self) -> None:
        # A non-empty but whitespace-only command lexes to no WORD tokens —
        # nothing to match against, so it is never a rescue.
        assert is_self_rescue("   ") is False


class TestAllowlistShape:
    def test_allowlist_is_non_empty_tuple_of_token_pattern_tuples(self) -> None:
        assert isinstance(SELF_RESCUE_ALLOWLIST, tuple)
        assert SELF_RESCUE_ALLOWLIST
        for entry in SELF_RESCUE_ALLOWLIST:
            assert isinstance(entry, tuple)
            # Each token is a literal string or the single OVERLAY wildcard.
            assert all(token is OVERLAY or isinstance(token, str) for token in entry)

    def test_each_entry_has_at_most_one_overlay_slot(self) -> None:
        # The OVERLAY wildcard consumes exactly one token; more than one per
        # entry would re-open arbitrary intervening tokens between literals.
        for entry in SELF_RESCUE_ALLOWLIST:
            assert sum(token is OVERLAY for token in entry) <= 1

    def test_allowlist_covers_the_documented_classes(self) -> None:
        flattened = {" ".join(t for t in entry if isinstance(t, str)) for entry in SELF_RESCUE_ALLOWLIST}
        # db migrate, worktree provision, gate disable, skill-loading gate
        # disable, and the fail-open toggle — the classes the NEVER-LOCKOUT
        # contract names.
        assert any("db migrate" in phrase for phrase in flattened)
        assert any("worktree provision" in phrase for phrase in flattened)
        assert any(phrase.endswith("gate disable") for phrase in flattened)
        assert any("skill-loading disable" in phrase for phrase in flattened)
        assert any("fail-open enable" in phrase for phrase in flattened)
        assert any("manage.py migrate" in phrase for phrase in flattened)


class TestEveryEntryEnforcesContiguity:
    """Prove the strict contiguity rule for EVERY allowlist entry.

    Each entry gets a positive (the exact rescue command rescues) and a
    negative (a bare positional appended to the entry rejects), so adding a
    new entry without its own contiguity coverage fails this sweep.
    """

    @pytest.mark.parametrize("entry", SELF_RESCUE_ALLOWLIST, ids=_entry_to_command)
    def test_exact_entry_command_is_a_rescue(self, entry: tuple[object, ...]) -> None:
        assert is_self_rescue(_entry_to_command(entry)) is True

    @pytest.mark.parametrize("entry", SELF_RESCUE_ALLOWLIST, ids=_entry_to_command)
    def test_entry_with_trailing_bare_positional_is_not_a_rescue(self, entry: tuple[object, ...]) -> None:
        # A bare positional appended to any otherwise-exact entry must reject.
        assert is_self_rescue(_entry_to_command(entry) + " git push") is False

    @pytest.mark.parametrize("entry", SELF_RESCUE_ALLOWLIST, ids=_entry_to_command)
    def test_entry_with_trailing_flag_is_a_rescue(self, entry: tuple[object, ...]) -> None:
        # A trailing ``-``-prefixed flag never disqualifies a genuine entry.
        assert is_self_rescue(_entry_to_command(entry) + " --yes") is True
