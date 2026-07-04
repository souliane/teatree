"""Tests for the classifier-relax PreToolUse allow hook.

The sanctioned Classifier Denial Protocol (skills/rules/SKILL.md §
"Classifier Denial Protocol") ends with:

- Step 3: AskUserQuestion with options "Allow it (relax classifier)" /
    "Keep the denial (do it differently)".
- Step 4: Edit ~/.claude/settings.json permissions.allow when the user affirms.

Without a PreToolUse allow, the agent's Edit call is blocked again by the
harness on the same permission that was just denied — completing the sanctioned
flow would be impossible even after explicit user approval.

The handler ``handle_allow_classifier_relax_settings_write`` emits
``{"permissionDecision": "allow"}`` ONLY when ALL of the following hold:

1. The tool being called is ``Edit`` or ``Write``.
2. The target file path resolves to ``~/.claude/settings.json``.
3. The recent transcript contains an ``AskUserQuestion`` tool_use whose
    ``input.questions`` list includes "Allow it (relax classifier)" (the
    verbatim option label from the protocol).
4. The user turn immediately after that AskUserQuestion affirms the relax
    (the response text matches an affirmative-relax pattern).

Any condition failing => the handler returns ``None`` without emitting
anything, leaving all subsequent handlers — including any deny handler — in
play.  This is a narrowly scoped allow, NOT a blanket self-modification bypass.

Threat model (documented here and in the implementation):

WHAT THIS ALLOWS: Edit/Write to ~/.claude/settings.json ONLY when there is
transcript evidence of the exact Step-3 user approval from the sanctioned
classifier-relax flow. The evidence requires both a specific AskUserQuestion
option text AND an affirmative user response.

WHAT THIS DOES NOT ALLOW:

- Any other target path (not settings.json).
- Edit/Write without transcript evidence of the Step-3 approval.
- Replay attacks: the transcript is ephemeral per session; a fabricated
    prior-turn cannot be injected through the hook data alone (the hook
    receives only the transcript_path, not the content directly).
- Approving "do X without running AskUserQuestion first" — the AskUserQuestion
    tool call itself must appear in the same session transcript.

RESIDUAL RISK: A transcript file the agent can write could in theory be
crafted to contain a forged AskUserQuestion block. This is mitigated by:
(a) the hook path and the transcript are both controlled by the harness, not
the agent, in normal operation; (b) the sanctioned flow is narrow and
well-documented so detection patterns are specific; (c) the allow emitted is
only for settings.json, not arbitrary paths.

Integration-style: real ``hook_router`` handler, real transcript JSONL written
under ``tmp_path``; only stdin/stdout are exercised through the handler.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.classifier_relax_gate import validate_relax_write
from hooks.scripts.hook_router import handle_allow_classifier_relax_settings_write

# ── Transcript helpers (mirrors test_structured_question_hook.py) ─────


def _assistant(text: str, tool_uses: list[dict] | None = None) -> dict:
    """Build a minimal assistant transcript entry.

    ``tool_uses`` is a list of dicts with ``name`` and ``input`` keys.
    """
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    content.extend({"type": "tool_use", "name": tu["name"], "input": tu.get("input", {})} for tu in tool_uses or [])
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _user(text: str = "go") -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _write_transcript(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _ask_question_tool(options: list[str]) -> dict:
    """Build an AskUserQuestion tool_use block with the given option strings."""
    return {"name": "AskUserQuestion", "input": {"questions": [{"question": "?", "options": options}]}}


def _settings_write_tool(name: str = "Edit") -> dict:
    """Build an Edit/Write tool_use block targeting ~/.claude/settings.json."""
    return {
        "type": "tool_use",
        "name": name,
        "input": {"file_path": str(Path("~/.claude/settings.json").expanduser())},
    }


def _settings_json_path() -> str:
    """Return the resolved path to ~/.claude/settings.json."""
    return str(Path("~/.claude/settings.json").expanduser())


def _decision(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


# ── Test data helpers ─────────────────────────────────────────────────


def _sanctioned_transcript(tmp_path: Path, user_affirm: str = "Allow it (relax classifier)") -> Path:
    """Full Step-3 + Step-4 transcript: AskUserQuestion with relax option, user affirms."""
    return _write_transcript(
        tmp_path,
        [
            _user("file the issue"),
            _assistant(
                "The command was denied. Choose:",
                tool_uses=[_ask_question_tool(["Allow it (relax classifier)", "Keep the denial (do it differently)"])],
            ),
            _user(user_affirm),
        ],
    )


# ── Tests for allow happy path ────────────────────────────────────────


class TestClassifierRelaxAllow:
    """The handler must emit ``{"permissionDecision": "allow"}`` for the happy path."""

    def test_allow_edit_settings_json_after_affirmative(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Edit ~/.claude/settings.json is allowed after sanctioned Step-3 approval."""
        transcript = _sanctioned_transcript(tmp_path)

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {"permissionDecision": "allow"}
        assert result is True

    def test_allow_write_settings_json_after_affirmative(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Write ~/.claude/settings.json is allowed when the payload is schema-valid (#857)."""
        transcript = _sanctioned_transcript(tmp_path)

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": _settings_json_path(),
                    "content": '{"permissions": {"allow": ["Bash(gh issue create *)"]}}',
                },
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {"permissionDecision": "allow"}
        assert result is True

    def test_allow_with_tilde_path(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The tilde form ~/.claude/settings.json is normalised before comparison."""
        transcript = _sanctioned_transcript(tmp_path)

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "~/.claude/settings.json"},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {"permissionDecision": "allow"}
        assert result is True

    def test_allow_when_user_says_allow_it(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """User response "Allow it" (substring of the full option) is treated as affirmative."""
        transcript = _sanctioned_transcript(tmp_path, user_affirm="Allow it")

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {"permissionDecision": "allow"}
        assert result is True

    def test_allow_when_user_says_yes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """User response "yes" is treated as affirmative."""
        transcript = _sanctioned_transcript(tmp_path, user_affirm="yes")

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {"permissionDecision": "allow"}
        assert result is True

    def test_allow_when_user_says_relax_classifier(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """User response "relax classifier" (the protocol shorthand) is affirmative.

        Note: a bare "relax" is intentionally NOT affirmative (review
        Findings 3/4) — it false-matched "please relax the check". The
        explicit protocol shorthand "relax classifier" is.
        """
        transcript = _sanctioned_transcript(tmp_path, user_affirm="relax classifier")

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {"permissionDecision": "allow"}
        assert result is True


# ── Tests for wrong tool / wrong path ────────────────────────────────


class TestClassifierRelaxWrongTarget:
    """The handler must return ``None`` (no output) when the target is not settings.json."""

    def test_noop_for_other_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A file other than ~/.claude/settings.json must not be allowed."""
        transcript = _sanctioned_transcript(tmp_path)

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(tmp_path / "other.json")},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {}
        assert result is not True

    def test_noop_for_bash_tool(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Bash tool is not Edit/Write — must not be allowed."""
        transcript = _sanctioned_transcript(tmp_path)

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo 'hi'"},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {}
        assert result is not True

    def test_noop_for_read_tool(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Read tool is not Edit/Write — must not be allowed."""
        transcript = _sanctioned_transcript(tmp_path)

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {}
        assert result is not True


# ── Tests for missing / wrong transcript evidence ─────────────────────


class TestClassifierRelaxNoEvidence:
    """The handler must return ``None`` when transcript evidence is absent or wrong."""

    def test_noop_when_no_ask_question_in_transcript(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """No AskUserQuestion call in transcript => no allow."""
        transcript = _write_transcript(
            tmp_path,
            [
                _user("edit settings"),
                _assistant("I will edit the file now."),
            ],
        )

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {}
        assert result is not True

    def test_noop_when_ask_question_lacks_relax_option(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AskUserQuestion without 'Allow it (relax classifier)' option => no allow."""
        transcript = _write_transcript(
            tmp_path,
            [
                _user("which approach?"),
                _assistant(
                    "Choose:",
                    tool_uses=[_ask_question_tool(["Option A", "Option B"])],
                ),
                _user("Option A"),
                _assistant("I will edit the file now."),
            ],
        )

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {}
        assert result is not True

    def test_noop_when_user_declined(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """User chose 'Keep the denial' => no allow."""
        transcript = _write_transcript(
            tmp_path,
            [
                _user("file the issue"),
                _assistant(
                    "Choose:",
                    tool_uses=[
                        _ask_question_tool(["Allow it (relax classifier)", "Keep the denial (do it differently)"])
                    ],
                ),
                _user("Keep the denial (do it differently)"),
            ],
        )

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {}
        assert result is not True

    def test_noop_when_user_response_is_ambiguous(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """User response that does not match any affirmative pattern => no allow."""
        transcript = _write_transcript(
            tmp_path,
            [
                _user("file the issue"),
                _assistant(
                    "Choose:",
                    tool_uses=[
                        _ask_question_tool(["Allow it (relax classifier)", "Keep the denial (do it differently)"])
                    ],
                ),
                _user("I'm not sure, let me think about it"),
            ],
        )

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {}
        assert result is not True

    def test_noop_when_transcript_path_missing(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No transcript_path in data => no allow (fail-safe)."""
        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
            }
        )

        assert _decision(capsys) == {}
        assert result is not True

    def test_noop_when_transcript_file_missing(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Nonexistent transcript file => no allow (fail-safe)."""
        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(tmp_path / "nonexistent.jsonl"),
            }
        )

        assert _decision(capsys) == {}
        assert result is not True


# ── Tests for router wiring ───────────────────────────────────────────


class TestClassifierRelaxWiring:
    """The handler must be registered first in the PreToolUse chain."""

    def test_registered_in_pretooluse_handlers(self) -> None:
        """handle_allow_classifier_relax_settings_write is in PreToolUse handlers."""
        assert handle_allow_classifier_relax_settings_write in router._HANDLERS["PreToolUse"]

    def test_registered_first_in_pretooluse_handlers(self) -> None:
        """The allow handler must be first — it must fire before any deny handler.

        The router short-circuits on the first ``True`` return. If a deny
        handler fires first (e.g. a blanket-ban on settings.json edits) the
        allow never gets a chance to run. Being first ensures the sanctioned
        allow is evaluated before any deny.
        """
        assert router._HANDLERS["PreToolUse"][0] is handle_allow_classifier_relax_settings_write


# ── Deny-branch coverage (review Finding 1) ───────────────────────────


class TestClassifierRelaxDenyBranchCoverage:
    """Cover the early-`continue`/`return False` deny branches in the scan.

    Each transcript here is constructed so the scan reaches a specific deny
    branch and returns no allow — these were untested before the cold review.
    """

    def test_noop_when_assistant_content_block_is_not_a_dict(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An assistant content block that is a plain string (not a dict) is skipped.

        The AskUserQuestion-with-relax option lives only inside a non-dict
        block, so the scan never sees a valid approval => no allow.
        """
        bad_entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    "Allow it (relax classifier)",  # plain string, not a dict
                    {"type": "text", "text": "no tool call here"},
                ],
            },
        }
        transcript = _write_transcript(tmp_path, [_user("file the issue"), bad_entry, _user("yes go ahead")])

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {}
        assert result is not True

    def test_noop_when_non_user_entry_interleaved_before_user_response(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An assistant turn between the AskUserQuestion and the user response.

        The scan must skip the interleaved non-user entry and still resolve
        the user's response correctly. Here the only following user turn is a
        decline, so the interleave path is exercised AND the result is no allow.
        """
        transcript = _write_transcript(
            tmp_path,
            [
                _user("file the issue"),
                _assistant(
                    "Choose:",
                    tool_uses=[
                        _ask_question_tool(["Allow it (relax classifier)", "Keep the denial (do it differently)"])
                    ],
                ),
                _assistant("(waiting for your answer)"),  # interleaved non-user entry
                _user("Keep the denial (do it differently)"),
            ],
        )

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {}
        assert result is not True

    def test_noop_when_ask_question_relax_is_last_entry_no_user_turn(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AskUserQuestion-with-relax at the very end, NO subsequent user turn => no allow.

        This guards against an allow being emitted before the user has
        actually answered the question.
        """
        transcript = _write_transcript(
            tmp_path,
            [
                _user("file the issue"),
                _assistant(
                    "Choose:",
                    tool_uses=[
                        _ask_question_tool(["Allow it (relax classifier)", "Keep the denial (do it differently)"])
                    ],
                ),
            ],
        )

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {}
        assert result is not True


# ── Per-write consent / consume-once (review Finding 2) ───────────────


class TestClassifierRelaxConsumeOnce:
    """One approval authorises exactly the NEXT settings.json write, not all later ones.

    A stale earlier approval, once a settings.json write has already been
    completed against it, must NOT authorise a second, later, unrelated
    settings.json write — that would be a replay of consumed consent.
    """

    def test_stale_approval_followed_by_completed_write_does_not_authorise_replay(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Replay attempt must be DENIED.

        Sequence: AskUserQuestion-relax -> user affirms -> settings.json Edit
        already happened (consent consumed) -> later, an unrelated second
        settings.json write is attempted with NO fresh approval. Deny.
        """
        transcript = _write_transcript(
            tmp_path,
            [
                _user("file the issue"),
                _assistant(
                    "Choose:",
                    tool_uses=[
                        _ask_question_tool(["Allow it (relax classifier)", "Keep the denial (do it differently)"])
                    ],
                ),
                _user("Allow it (relax classifier)"),
                # The approval is consumed by this completed settings.json write:
                _assistant("Adding the rule.", tool_uses=[_settings_write_tool("Edit")]),
                _user("now do something else"),
                _assistant("Working on the unrelated task."),
            ],
        )

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {}
        assert result is not True

    def test_fresh_approval_after_a_prior_consumed_one_still_authorises(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A NEW approval after a prior consumed one re-authorises the next write.

        Consume-once must not be "one approval per session forever" — a
        genuine second escalation (new AskUserQuestion-relax + new affirmative)
        with no settings write since it must still be allowed.
        """
        transcript = _write_transcript(
            tmp_path,
            [
                _user("file the issue"),
                _assistant(
                    "Choose:",
                    tool_uses=[
                        _ask_question_tool(["Allow it (relax classifier)", "Keep the denial (do it differently)"])
                    ],
                ),
                _user("Allow it (relax classifier)"),
                _assistant("Adding the rule.", tool_uses=[_settings_write_tool("Edit")]),
                _user("now another command got denied"),
                _assistant(
                    "Choose again:",
                    tool_uses=[
                        _ask_question_tool(["Allow it (relax classifier)", "Keep the denial (do it differently)"])
                    ],
                ),
                _user("Allow it (relax classifier)"),
            ],
        )

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {"permissionDecision": "allow"}
        assert result is True


# ── Affirmative-pattern precision (review Findings 3/4) ────────────────


class TestClassifierRelaxAffirmativePrecision:
    """The affirmative detector must not over-deny nor be loosely spoofable."""

    def test_mid_sentence_affirmative_is_accepted(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        r"""A genuine affirmative not at the start of the message still counts.

        The old ``^yes\b`` anchor over-denied "Actually, yes — go ahead".
        """
        transcript = _sanctioned_transcript(tmp_path, user_affirm="Actually, yes — go ahead and allow it")

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {"permissionDecision": "allow"}
        assert result is True

    def test_please_relax_the_check_is_not_affirmative(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        r"""'please relax the check' must NOT be read as selecting the relax option.

        The old loose ``relax\b`` substring produced this false positive.
        """
        transcript = _sanctioned_transcript(tmp_path, user_affirm="please relax the check on line length")

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {}
        assert result is not True


# ── Helper-level defensive deny branches (review Finding 1) ───────────


class TestClassifierRelaxHelperDefensiveBranches:
    """Every malformed-structure deny branch in the scan helpers is covered.

    A security-critical allow handler must have no untested deny path: each
    helper returns the fail-closed value on every malformed shape.
    """

    def test_non_dict_post_approval_block_does_not_consume_consent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A non-dict content block after the approval is skipped by the scan.

        The call-site ``isinstance(block, dict)`` guard means a stray non-dict
        block does not register as a settings write (would otherwise either
        crash or wrongly consume/ignore consent). The allow still stands.
        """
        entry_with_non_dict_block = {
            "type": "assistant",
            "message": {"role": "assistant", "content": ["a bare string block", {"type": "text", "text": "ok"}]},
        }
        transcript = _write_transcript(
            tmp_path,
            [
                _user("file the issue"),
                _assistant(
                    "Choose:",
                    tool_uses=[
                        _ask_question_tool(["Allow it (relax classifier)", "Keep the denial (do it differently)"])
                    ],
                ),
                _user("Allow it (relax classifier)"),
                entry_with_non_dict_block,
            ],
        )

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {"permissionDecision": "allow"}
        assert result is True

    def test_block_is_settings_write_false_for_non_edit_tool(self) -> None:
        block = {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
        assert router._block_is_settings_write(block) is False

    def test_block_is_settings_write_true_for_settings_edit(self) -> None:
        assert router._block_is_settings_write(_settings_write_tool("Write")) is True

    def test_settings_json_target_resolves_from_module_constant(self) -> None:
        """The hoisted module constant expands to the same resolved path (nit).

        ``_SETTINGS_JSON_PATH`` is the single (unexpanded) source of truth;
        ``_settings_json_target()`` must still resolve to the HOME-sensitive
        absolute path used everywhere else.
        """
        assert router._SETTINGS_JSON_PATH == "~/.claude/settings.json"
        assert router._settings_json_target() == _settings_json_path()
        assert router._settings_json_target() == str(Path(router._SETTINGS_JSON_PATH).expanduser())

    def test_block_is_settings_write_false_on_path_expansion_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A path that raises on expanduser() fails closed (no allow)."""

        def _boom(_self: Path) -> Path:
            msg = "unresolvable path"
            raise RuntimeError(msg)

        monkeypatch.setattr(Path, "expanduser", _boom)
        block = {"type": "tool_use", "name": "Edit", "input": {"file_path": "~/x"}}
        assert router._block_is_settings_write(block) is False

    def test_ask_question_relax_option_false_for_non_tool_use(self) -> None:
        assert router._ask_question_has_relax_option({"type": "text", "text": "hi"}) is False

    def test_ask_question_relax_option_false_when_questions_not_a_list(self) -> None:
        block = {"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": "oops"}}
        assert router._ask_question_has_relax_option(block) is False

    def test_ask_question_relax_option_skips_non_dict_question(self) -> None:
        block = {
            "type": "tool_use",
            "name": "AskUserQuestion",
            "input": {"questions": ["not a dict", {"options": ["Allow it (relax classifier)"]}]},
        }
        assert router._ask_question_has_relax_option(block) is True

    def test_ask_question_relax_option_skips_when_options_not_a_list(self) -> None:
        block = {
            "type": "tool_use",
            "name": "AskUserQuestion",
            "input": {"questions": [{"options": "oops"}]},
        }
        assert router._ask_question_has_relax_option(block) is False

    def test_ask_question_relax_option_matches_structured_label_dict(self) -> None:
        """Option given as a dict with a 'label' key (not a bare string)."""
        block = {
            "type": "tool_use",
            "name": "AskUserQuestion",
            "input": {"questions": [{"options": [{"label": "Allow it (relax classifier)"}]}]},
        }
        assert router._ask_question_has_relax_option(block) is True

    def test_ask_question_relax_option_repr_substring_does_not_falsely_match(self) -> None:
        """A non-relax option whose text merely contains the phrase must NOT match.

        Guards review Finding 5: the old code did ``OPTION in str(options)``;
        a structured option with the phrase embedded in a longer
        non-selectable string must not be treated as the verbatim option.
        """
        block = {
            "type": "tool_use",
            "name": "AskUserQuestion",
            "input": {"questions": [{"options": ["Discuss: Allow it (relax classifier) tradeoffs"]}]},
        }
        assert router._ask_question_has_relax_option(block) is False

    def test_consume_once_non_write_entries_after_approval_still_allow(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Entries after the approval that are NOT settings writes don't consume it.

        Covers the consume-once loop's non-write branch: the loop iterates a
        post-approval entry, finds no settings write, and the allow stands.
        """
        transcript = _write_transcript(
            tmp_path,
            [
                _user("file the issue"),
                _assistant(
                    "Choose:",
                    tool_uses=[
                        _ask_question_tool(["Allow it (relax classifier)", "Keep the denial (do it differently)"])
                    ],
                ),
                _user("Allow it (relax classifier)"),
                _assistant("Reading the file first."),  # post-approval, not a settings write
            ],
        )

        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": _settings_json_path()},
                "transcript_path": str(transcript),
            }
        )

        assert _decision(capsys) == {"permissionDecision": "allow"}
        assert result is True


# ── #857: content-schema validation of the relax write payload ─────────


class TestRelaxWriteSchemaValidation:
    """Unit coverage for the #857 payload validator (pure, no transcript)."""

    def test_valid_write_content_passes(self) -> None:
        assert validate_relax_write("Write", {"content": '{"permissions": {"allow": ["Bash(gh pr view *)"]}}'}) is None

    def test_invalid_json_write_refused(self) -> None:
        reason = validate_relax_write("Write", {"content": "{not json"})
        assert reason is not None
        assert "valid JSON" in reason

    def test_non_object_top_level_refused(self) -> None:
        reason = validate_relax_write("Write", {"content": '["a", "b"]'})
        assert reason is not None
        assert "JSON object" in reason

    def test_non_string_allow_entry_refused(self) -> None:
        reason = validate_relax_write("Write", {"content": '{"permissions": {"allow": [123]}}'})
        assert reason is not None
        assert "list of strings" in reason

    def test_blanket_bash_wildcard_in_write_refused(self) -> None:
        reason = validate_relax_write("Write", {"content": '{"permissions": {"allow": ["Bash(*)"]}}'})
        assert reason is not None
        assert "blanket-wildcard" in reason

    def test_bare_bash_rule_in_write_refused(self) -> None:
        reason = validate_relax_write("Write", {"content": '{"permissions": {"allow": ["Bash"]}}'})
        assert reason is not None
        assert "blanket-wildcard" in reason

    def test_automode_allow_validated(self) -> None:
        reason = validate_relax_write("Write", {"content": '{"autoMode": {"allow": ["Bash(* *)"]}}'})
        assert reason is not None
        assert "blanket-wildcard" in reason

    def test_edit_new_string_blanket_rule_refused(self) -> None:
        reason = validate_relax_write("Edit", {"new_string": '    "Bash(:*)",'})
        assert reason is not None
        assert "blanket-wildcard" in reason

    def test_edit_scoped_new_string_passes(self) -> None:
        assert validate_relax_write("Edit", {"new_string": '    "Bash(gh issue create *)",'}) is None


class TestRelaxWriteSchemaDeniesHandler:
    """The handler DENIES a sanctioned-but-malformed write, refusing pre-persist (#857)."""

    def test_malformed_write_denied_even_with_approval(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _sanctioned_transcript(tmp_path)
        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": _settings_json_path(), "content": "{ broken json"},
                "transcript_path": str(transcript),
            }
        )
        decision = _decision(capsys)
        assert (
            decision.get("permissionDecision") == "deny"
            or decision.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        )
        assert result is True

    def test_blanket_wildcard_write_denied_even_with_approval(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _sanctioned_transcript(tmp_path)
        result = handle_allow_classifier_relax_settings_write(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": _settings_json_path(),
                    "content": '{"permissions": {"allow": ["Bash(* *)"]}}',
                },
                "transcript_path": str(transcript),
            }
        )
        assert result is True
        assert _decision(capsys) != {"permissionDecision": "allow"}
