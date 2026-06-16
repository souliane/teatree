"""Tests for the ``handle_route_away_mode_question`` PreToolUse hook (#58).

Integration-first: the real ``hook_router`` handler is invoked with a
PreToolUse payload synthesised in-process, and the assertion is on
the JSON stdout + the ``DeferredQuestion`` row that landed in the
test DB. The load-bearing §807 interop test is at the bottom:
synthesising a transcript with a hook-converted ``AskUserQuestion``
tool_use and asserting the structured-question Stop gate then
returns ``None`` (gate satisfied — the call is *structurally
complete*, just converted at the PreToolUse layer).
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _LOOP_PROMPT, handle_enforce_structured_question, handle_route_away_mode_question
from teatree.core import availability
from teatree.core.availability import LIVE_TURN_FRESHNESS, PresenceHeartbeat
from teatree.core.models.deferred_question import DeferredQuestion

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _ask_payload(question: str, options: list[dict] | None = None, **extra: str) -> dict:
    payload: dict = {
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {"question": question, "options": options or []},
            ],
        },
    }
    payload.update(extra)
    return payload


def _stdout(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


@pytest.fixture(autouse=True)
def _force_away(monkeypatch: pytest.MonkeyPatch) -> None:
    """All tests in this module exercise the away-mode branch.

    The mode resolver normally reads from disk; for these unit-tests
    we force the resolver result so the hook is exercised under a
    deterministic state without touching the user's real config.
    """
    monkeypatch.setattr(router, "_resolved_away_mode", lambda: True)


class TestAwayModeConversion:
    def test_records_deferred_question_and_emits_deny(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_route_away_mode_question(_ask_payload("Should I ship?"))
        assert result is True
        out = _stdout(capsys)
        assert out["permissionDecision"] == "deny"
        assert "DeferredQuestion" in out["permissionDecisionReason"]
        rows = list(DeferredQuestion.objects.all())
        assert len(rows) == 1
        assert rows[0].question == "Should I ship?"
        assert rows[0].is_pending is True

    def test_captures_session_and_tool_use_id(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_route_away_mode_question(
            _ask_payload(
                "X or Y?",
                options=[{"label": "X"}, {"label": "Y"}],
                session_id="sess-42",
                tool_use_id="toolu_42",
            )
        )
        capsys.readouterr()  # drain
        row = DeferredQuestion.objects.get(question="X or Y?")
        assert row.session_id == "sess-42"
        assert row.tool_use_id == "toolu_42"
        assert json.loads(row.options_json) == [{"label": "X"}, {"label": "Y"}]

    def test_reason_names_the_recorded_row_id(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_route_away_mode_question(_ask_payload("How?"))
        out = _stdout(capsys)
        row = DeferredQuestion.objects.latest("created_at")
        assert f"#{row.pk}" in out["permissionDecisionReason"]
        assert f"answer {row.pk}" in out["permissionDecisionReason"]

    def test_empty_question_fails_open(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_route_away_mode_question(_ask_payload(""))
        assert result is False
        assert _stdout(capsys) == {}
        assert DeferredQuestion.objects.count() == 0

    def test_non_askuserquestion_tool_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_route_away_mode_question({"tool_name": "Bash", "tool_input": {}})
        assert result is False
        assert _stdout(capsys) == {}


class TestPresentModeDoesNotIntercept:
    def test_present_mode_skips_the_handler(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(router, "_resolved_away_mode", lambda: False)
        result = handle_route_away_mode_question(_ask_payload("Should I ship?"))
        assert result is False
        assert _stdout(capsys) == {}
        assert DeferredQuestion.objects.count() == 0


class TestUserDrivenTurnRendersLiveEvenWhenAway:
    """#189: a fresh same-session user prompt renders the question LIVE.

    The whole point of ``/checking`` (and "shoot me questions from here"):
    when the user is the one driving THIS turn — a fresh live prompt this
    turn, in this session — their ``AskUserQuestion`` must render in-client
    even under a manual-away override, with NO availability flip. The
    handler must NOT defer and must NOT create a ``DeferredQuestion`` row.
    """

    def test_user_driven_away_turn_renders_live_and_does_not_defer(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(router, "_is_live_user_turn", lambda _data: True)
        result = handle_route_away_mode_question(_ask_payload("Approve A or B?", session_id="s-live"))
        assert result is False
        assert _stdout(capsys) == {}
        assert DeferredQuestion.objects.count() == 0

    def test_loop_driven_away_turn_still_defers_invariant_9(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # THE must-not-regress test: a loop-driven / no-fresh-prompt turn under
        # manual-away MUST still capture the question durably + emit the deny.
        monkeypatch.setattr(router, "_is_live_user_turn", lambda _data: False)
        result = handle_route_away_mode_question(_ask_payload("Approve A or B?", session_id="s-loop"))
        assert result is True
        out = _stdout(capsys)
        assert out["permissionDecision"] == "deny"
        assert "DeferredQuestion" in out["permissionDecisionReason"]
        assert DeferredQuestion.objects.count() == 1

    def test_unknown_live_turn_signal_fails_safe_to_deferring(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A missing/unknown presence signal must default to the safe (defer)
        # path — never silently render live and lose the away capture.
        monkeypatch.setattr(router, "_is_live_user_turn", lambda _data: False)
        result = handle_route_away_mode_question(_ask_payload("Ship?", session_id="s-unknown"))
        assert result is True
        assert DeferredQuestion.objects.count() == 1


class TestLoopTurnDefersThroughRealPredicateInvariant9:
    """Invariant 9, exercised through the REAL ``_is_live_user_turn``.

    The sibling class monkeypatches the predicate, so it cannot prove the
    production escape leaves invariant 9 intact. This drives the real
    predicate end-to-end: an autonomous / loop-driven turn has no prior
    same-session ``UserPromptSubmit`` heartbeat, so the real predicate
    returns ``False`` and the question is captured durably.
    """

    @pytest.fixture(autouse=True)
    def _empty_presence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "availability_presence"
        monkeypatch.setattr(availability, "PRESENCE", PresenceHeartbeat(locate=lambda: target))

    def test_loop_turn_with_no_heartbeat_defers(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_route_away_mode_question(_ask_payload("Approve A or B?", session_id="s-loop"))
        assert result is True
        out = _stdout(capsys)
        assert out["permissionDecision"] == "deny"
        assert DeferredQuestion.objects.count() == 1


class TestSelfPumpTurnWithFreshUserPromptRendersLive:
    """#2155: a fresh user prompt during a self-pump loop renders the question live.

    The end-to-end reproduction of the reported high-irritation bug, driven
    through the REAL ``handle_record_presence`` recording seam and the REAL
    ``_is_live_user_turn`` predicate (no monkeypatch of either) — exactly the
    path a live keystroke and the away-mode gate take.

    The loop owner is self-pumping; the user types a genuine fresh prompt the
    harness delivers prefixed by the loop continuation text. ``UserPromptSubmit``
    must record presence for that live keystroke so the next
    ``AskUserQuestion`` renders in-client instead of deferring to a
    ``DeferredQuestion``. The invariant-9 anchor (a PURE loop tick, no user
    text → still defers) lives in the second test so the must-render escape is
    proven an escape, not a defanged gate.
    """

    @pytest.fixture(autouse=True)
    def _isolated_presence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "availability_presence"
        monkeypatch.setattr(availability, "PRESENCE", PresenceHeartbeat(locate=lambda: target))

    def test_fresh_user_prompt_prefixed_by_loop_text_renders_live(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "owner"
        # 1. The user's live keystroke arrives during the self-pump, delivered
        #    by the harness prefixed with the loop continuation text.
        router.handle_record_presence(
            {"prompt": f"{_LOOP_PROMPT}\n\nactually, ask me which option you prefer", "session_id": session_id}
        )
        # 2. The AskUserQuestion on this self-pump-bridged turn must render live.
        result = handle_route_away_mode_question(_ask_payload("Approve A or B?", session_id=session_id))
        assert result is False, "a fresh same-session user prompt this turn must render the question live"
        assert _stdout(capsys) == {}
        assert DeferredQuestion.objects.count() == 0

    def test_pure_loop_tick_still_defers_invariant_9(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "owner"
        # A pure autonomous tick (no genuine user text) must NOT stamp presence,
        # so its AskUserQuestion still defers durably — invariant 9 intact.
        router.handle_record_presence({"prompt": _LOOP_PROMPT, "session_id": session_id})
        result = handle_route_away_mode_question(_ask_payload("Approve A or B?", session_id=session_id))
        assert result is True
        out = _stdout(capsys)
        assert out["permissionDecision"] == "deny"
        assert DeferredQuestion.objects.count() == 1


class TestWalkThroughSecondQuestionStaysLive:
    """#2058: a multi-question walk-through keeps EVERY question live.

    The exact bug: under a manual-away override, a user-invoked ``/checking``
    walk-through renders its FIRST question live (fresh same-session prompt),
    the user answers, an intervening background task-notification turn fires
    (which does NOT refresh the presence heartbeat), and the SECOND question
    lands past :data:`LIVE_TURN_FRESHNESS` — so the pre-fix code deferred it,
    asking only one question live and minting a duplicate ``DeferredQuestion``.

    The fix slides the live window forward each time an already-live question
    renders, so the whole user-driven chain stays live. The must-not-regress
    invariant-9 sibling (a loop-driven turn that was never live still defers)
    lives in ``TestLoopTurnDefersThroughRealPredicateInvariant9``.
    """

    @pytest.fixture(autouse=True)
    def _isolated_presence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "availability_presence"
        monkeypatch.setattr(availability, "PRESENCE", PresenceHeartbeat(locate=lambda: target))

    def test_second_question_after_notification_turn_still_renders_live(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session_id = "s-checking"
        # 1. User prompt lands (UserPromptSubmit heartbeat) — the user drives.
        t_prompt = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
        availability.PRESENCE.record(session_id=session_id, now=t_prompt)

        # Drive time through the real predicate by patching the clock the hook
        # reads, so this exercises the production path end to end. The hook calls
        # the methods on the PRESENCE singleton, so patch the instance methods.
        clock = {"now": t_prompt + timedelta(seconds=20)}
        heartbeat = availability.PRESENCE
        real_is_live = heartbeat.is_live_user_turn
        real_refresh = heartbeat.refresh_live_turn
        monkeypatch.setattr(
            heartbeat, "is_live_user_turn", lambda **kw: real_is_live(session_id=kw["session_id"], now=clock["now"])
        )
        monkeypatch.setattr(
            heartbeat, "refresh_live_turn", lambda **kw: real_refresh(session_id=kw["session_id"], now=clock["now"])
        )

        # 2. First question renders live (within the window) and is answered.
        first = handle_route_away_mode_question(_ask_payload("Approve item 1?", session_id=session_id))
        assert first is False, "first question must render live, not defer"
        assert _stdout(capsys) == {}
        assert DeferredQuestion.objects.count() == 0

        # 3. Background task-notification turn fires + the user reads/answers in
        # client. Wall-time advances PAST the original window — without the
        # slide the second question would age out.
        clock["now"] = t_prompt + timedelta(seconds=20) + LIVE_TURN_FRESHNESS - timedelta(seconds=10)
        assert clock["now"] - t_prompt > LIVE_TURN_FRESHNESS

        # 4. Second question in the SAME walk-through must STILL render live.
        second = handle_route_away_mode_question(_ask_payload("Approve item 2?", session_id=session_id))
        assert second is False, "second question must still render live, not defer (#2058)"
        assert _stdout(capsys) == {}
        assert DeferredQuestion.objects.count() == 0


class TestAwayModeMirrorsToSlack:
    """In away mode the question must ALSO reach the user's Slack DM (#182).

    The user reads Slack, not the CLI. The away-mode handler runs FIRST
    and denies, short-circuiting the PreToolUse chain before the present-
    mode ``handle_mirror_question_to_slack`` would run — so the away-mode
    handler is the only place that can mirror an away-mode question to
    Slack. Without this the question is recorded durably but never
    surfaces to the user until they happen to run ``t3 questions list``.
    """

    def test_away_question_posts_to_slack_and_still_denies(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        with (
            patch.object(router, "_perform_slack_post", return_value="1700.0001") as mock_post,
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
        ):
            result = handle_route_away_mode_question(
                _ask_payload("Ship it?", options=[{"label": "Yes"}, {"label": "No"}], session_id="s-1")
            )
        assert result is True
        out = _stdout(capsys)
        assert out["permissionDecision"] == "deny"
        mock_post.assert_called_once()
        slack_cfg, questions = mock_post.call_args.args
        assert slack_cfg == ("tok/ref", "U1")
        assert questions[0]["question"] == "Ship it?"

    def test_slack_post_is_idempotent_across_retries(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        with (
            patch.object(router, "_perform_slack_post", return_value="1700.0001") as mock_post,
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
        ):
            handle_route_away_mode_question(_ask_payload("Ship it?", session_id="s-1", tool_use_id="t-9"))
            capsys.readouterr()  # drain
            handle_route_away_mode_question(_ask_payload("Ship it?", session_id="s-1", tool_use_id="t-9"))
        assert mock_post.call_count == 1

    def test_slack_post_failure_does_not_block_the_deny(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        with (
            patch.object(router, "_perform_slack_post", side_effect=RuntimeError("slack down")),
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
        ):
            result = handle_route_away_mode_question(_ask_payload("Ship it?", session_id="s-1"))
        assert result is True
        out = _stdout(capsys)
        assert out["permissionDecision"] == "deny"
        assert DeferredQuestion.objects.count() == 1

    def test_mirror_posts_only_the_recorded_question_for_multi_question_payload(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        payload = {
            "tool_name": "AskUserQuestion",
            "tool_input": {
                "questions": [
                    {"question": "Answerable?", "options": []},
                    {"question": "Unrecorded?", "options": []},
                ]
            },
            "session_id": "s-1",
        }
        with (
            patch.object(router, "_perform_slack_post", return_value="1700.0001") as mock_post,
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
        ):
            handle_route_away_mode_question(payload)
        capsys.readouterr()
        _slack_cfg, questions = mock_post.call_args.args
        assert [q["question"] for q in questions] == ["Answerable?"]
        assert DeferredQuestion.objects.count() == 1


class TestAwayCaptureStoresMirrorFields:
    """Away-mode capture stores the mirror fields the #1174 matcher binds on."""

    def test_records_slack_ts_channel_generation_and_run(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        with (
            patch.object(router, "_perform_slack_post", return_value="1700.0009"),
            patch.object(router, "_slack_config_from_toml", return_value=("tok/ref", "U1")),
            patch.object(router, "_read_dm_channel_cache", return_value="D-away"),
        ):
            handle_route_away_mode_question(
                _ask_payload("Ship?", options=[{"label": "Yes"}], session_id="s-9", run_id="run-9")
            )
        capsys.readouterr()
        row = DeferredQuestion.objects.latest("created_at")
        assert row.slack_ts == "1700.0009"
        assert row.slack_channel == "D-away"
        assert row.generation == 1
        assert row.run_id == "run-9"
        assert row.options_hash != ""


class TestSection807InteropGate:
    """The load-bearing §807 interop test.

    BLUEPRINT §17.1 invariant 9 promises that the away-mode path is a
    *sanctioned destination* for the same ``AskUserQuestion`` tool call
    — converted at the ``PreToolUse`` layer — never an inline prose
    fallback. A converted call still emits a ``tool_use`` block in the
    transcript (the PreToolUse deny denies *execution* but the tool_use
    itself is recorded). The §807 ``handle_enforce_structured_question``
    Stop gate reads the transcript's last assistant turn, sees that a
    ``AskUserQuestion`` tool_use occurred, and returns ``None`` —
    indicating the structured-question gate is satisfied.
    """

    def _transcript_with_tool_use(self, tmp_path: Path) -> Path:
        path = tmp_path / "transcript.jsonl"
        entries = [
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "do it"}]}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Should I proceed? Recording for later."},
                        {"type": "tool_use", "name": "AskUserQuestion", "input": {}},
                    ],
                },
            },
        ]
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        return path

    def test_converted_question_satisfies_807_gate(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = self._transcript_with_tool_use(tmp_path)
        result = handle_enforce_structured_question({"transcript_path": str(transcript)})
        assert result is None
        # No 'block' decision was written.
        out = capsys.readouterr().out.strip()
        assert out == ""

    def test_inline_question_without_tool_use_still_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Sanity: §807 gate still fires when there is no tool call.

        Without this assertion the previous test could be passing
        because the §807 gate is broken in general — we want to prove
        it is the tool_use block specifically that satisfies the gate.
        """
        path = tmp_path / "transcript.jsonl"
        entries = [
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "do it"}]}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Should I proceed? Please choose A or B."},
                    ],
                },
            },
        ]
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        result = handle_enforce_structured_question({"transcript_path": str(path)})
        assert result is True
