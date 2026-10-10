# test-path: cross-cutting
# Drives hooks/scripts/hook_router.py question routing over the transcript reader; no src/teatree mirror.
"""The live-turn escape in ``handle_mirror_question_to_slack`` (#189, #2058, #2155).

Integration-first: the real ``hook_router`` handler is invoked with a PreToolUse payload
synthesised in-process whose transcript says what the owner did, read by the REAL
``_is_live_user_turn`` predicate. The load-bearing §807 interop test is at
the bottom: a transcript carrying a hook-converted ``AskUserQuestion`` tool_use satisfies
the structured-question Stop gate, because the call is *structurally complete* — just
converted at the PreToolUse layer.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _LOOP_PROMPT, handle_enforce_structured_question, handle_mirror_question_to_slack
from teatree.core import notify as notify_module
from teatree.core.modelkit.owner_decision import OWNER_QUESTION_ROUTE, OwnerDecision
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _ask_payload(question: str, options: list[dict] | None = None, **extra: str) -> dict:
    payload: dict = {
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": question, "options": options or []}]},
    }
    payload.update(extra)
    return payload


def _slack_backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D1"
    backend.post_message.return_value = {"ok": True, "ts": "1700.0001"}
    backend.get_permalink.return_value = "https://acme.slack.com/archives/D1/p1700"
    return backend


def _drain_to(backend: MagicMock) -> None:
    """Run the real first-post drain against *backend* — what the tick poster does."""
    with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
        drain_unmirrored_deferred_questions(user_id="U1", backend=backend)


def _transcript(tmp_path: Path, *entries: dict) -> str:
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return str(path)


def _ago(seconds: float) -> str:
    return (datetime.now(tz=UTC) - timedelta(seconds=seconds)).isoformat()


def _owner_typed(text: str, seconds_ago: float = 1) -> dict:
    return {
        "type": "user",
        "origin": {"kind": "human"},
        "timestamp": _ago(seconds_ago),
        "message": {"role": "user", "content": text},
    }


def _owner_answered(seconds_ago: float) -> dict:
    return {
        "type": "user",
        "timestamp": _ago(seconds_ago),
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "A"}]},
        "toolUseResult": {"answers": {"Approve item 1?": "yes"}},
    }


def _stdout(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


@pytest.fixture(autouse=True)
def _loop_driven_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A loop-owning session, so only the live-turn predicate decides the verdict."""
    monkeypatch.setattr(router, "_session_drives_loop", lambda _session: True)
    monkeypatch.setattr(router, "STATE_DIR", tmp_path)


class TestLoopTurnDefersThroughRealPredicateInvariant9:
    """Invariant 9, exercised through the REAL ``_is_live_user_turn``.

    An autonomous / loop-driven turn carries no recent owner prompt in its transcript, so
    the real predicate returns ``False`` and the question is denied in favour of the
    durable internal row.
    """

    def test_loop_turn_with_no_heartbeat_defers(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_mirror_question_to_slack(_ask_payload("Approve A or B?", session_id="s-loop"))
        assert result is True
        assert _stdout(capsys)["permissionDecision"] == "deny"

    def test_empty_question_fails_open(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_mirror_question_to_slack(_ask_payload("", session_id="s-loop"))
        assert result is False
        assert _stdout(capsys) == {}

    def test_non_askuserquestion_tool_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_mirror_question_to_slack({"tool_name": "Bash", "tool_input": {}})
        assert result is False
        assert _stdout(capsys) == {}


class TestSelfPumpTurnWithFreshUserPromptRendersLive:
    """#2155: a fresh user prompt during a self-pump loop renders the question live.

    The end-to-end reproduction of the reported high-irritation bug, driven through the
    REAL ``_is_live_user_turn`` predicate over the session transcript. The loop owner is
    self-pumping; the user types a genuine fresh prompt the harness delivers prefixed by
    the loop continuation text. The invariant-9 anchor (a
    PURE loop tick, no user text → still denies) lives in the second test so the
    must-render escape is proven an escape, not a defanged gate.
    """

    def test_fresh_user_prompt_prefixed_by_loop_text_renders_live(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _transcript(tmp_path, _owner_typed(f"{_LOOP_PROMPT}\n\nactually, ask me which option you prefer"))
        result = handle_mirror_question_to_slack(
            _ask_payload("Approve A or B?", session_id="owner", transcript_path=transcript)
        )
        assert result is False, "a fresh same-session user prompt this turn must render the question live"
        assert _stdout(capsys) == {}

    def test_pure_loop_tick_still_defers_invariant_9(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _transcript(tmp_path, _owner_typed(_LOOP_PROMPT))
        result = handle_mirror_question_to_slack(
            _ask_payload("Approve A or B?", session_id="owner", transcript_path=transcript)
        )
        assert result is True
        assert _stdout(capsys)["permissionDecision"] == "deny"


class TestWalkThroughSecondQuestionStaysLive:
    """#2058: a multi-question walk-through keeps EVERY question live.

    A user-invoked ``/checking`` walk-through renders its FIRST question live (fresh
    prompt); by the SECOND the prompt is past the window, but the owner's in-client
    answer to the first is fresh evidence they are still here.
    """

    def test_second_question_after_an_in_client_answer_still_renders_live(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first = _transcript(tmp_path, _owner_typed("/checking", 20))
        assert (
            handle_mirror_question_to_slack(_ask_payload("Approve item 1?", session_id="s", transcript_path=first))
            is False
        )
        assert _stdout(capsys) == {}

        second = _transcript(tmp_path, _owner_typed("/checking", 600), _owner_answered(10))
        assert (
            handle_mirror_question_to_slack(_ask_payload("Approve item 2?", session_id="s", transcript_path=second))
            is False
        )
        assert _stdout(capsys) == {}


class TestAttendedTurnNeverReachesSlack:
    """#4673: a question asked in Claude Code is not asked again in Slack.

    The attended arms render in-client, so nothing leaves the box — and crucially
    they record NO row: an un-mirrored row is exactly what the tick drain picks up,
    which would reinstate the duplication one cadence later.
    """

    def test_live_turn_posts_nothing_and_records_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        live = {"session_id": "s-2", "transcript_path": _transcript(tmp_path, _owner_typed("ask me something"))}
        first = handle_mirror_question_to_slack(_ask_payload("Ship it?", tool_use_id="t-1", **live))
        capsys.readouterr()
        second = handle_mirror_question_to_slack(_ask_payload("Ship it?", tool_use_id="t-2", **live))
        capsys.readouterr()

        assert first is False, "a live turn must render in-client, not deny"
        assert second is False, "a live turn must render in-client, not deny"
        assert DeferredQuestion.objects.count() == 0, "an attended row would be drained to Slack next tick"

    def test_attended_non_owner_turn_posts_nothing_and_records_nothing(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router, "_session_drives_loop", lambda _session: False)
        verdict = handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-7", tool_use_id="t-20"))
        capsys.readouterr()

        assert verdict is False
        assert DeferredQuestion.objects.count() == 0

    def test_an_attended_ask_supersedes_the_loop_row_it_replaces(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise the Slack re-ask nag outlives the answer the owner just gave in-client.

        The re-ask is a DISTINCT harness call (its own ``tool_use_id``) inside the same
        run, which is the shape supersession is scoped to.
        """
        stranded = DeferredQuestion.record(
            "Ship it?",
            session_id="s-8",
            run_id="r-1",
            decision=OwnerDecision.PRODUCT_SCOPE,
            checked=["the ticket is silent"],
        )
        assert stranded.dismissed_at is None

        monkeypatch.setattr(router, "_session_drives_loop", lambda _session: False)
        handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-8", run_id="r-1", tool_use_id="t-22"))
        capsys.readouterr()

        stranded.refresh_from_db()
        assert stranded.dismissed_at is not None, "the superseded loop row still nags on Slack"
        assert DeferredQuestion.objects.count() == 1, "the attended re-ask must not record its own row"


class TestALoopDrivenQuestionNeverReachesTheOwner:
    """A loop-driven ``AskUserQuestion`` is recorded internal and never DM'd (#5096).

    Driven through the real first-post drain, so the property proven is "the owner never
    sees it", with an owner-decision row in the same drain as the control that IS posted.
    """

    def test_the_captured_question_is_not_posted_while_an_owner_decision_is(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backend = _slack_backend()
        handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-1", tool_use_id="t-9"))
        capsys.readouterr()
        captured = DeferredQuestion.objects.get()
        owner = DeferredQuestion.record(
            "Rotate the deploy token?", decision=OwnerDecision.CREDENTIALS, checked=["the ticket is silent"]
        )

        _drain_to(backend)

        assert backend.post_message.call_count == 1
        assert "Rotate the deploy token?" in backend.post_message.call_args.kwargs["text"]
        captured.refresh_from_db()
        owner.refresh_from_db()
        assert captured.slack_ts == ""
        assert owner.slack_ts == "1700.0001"

    def test_loop_driven_question_internal_and_deny_reason_names_checked(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-7", tool_use_id="t-16"))

        reason = _stdout(capsys)["permissionDecisionReason"]
        assert DeferredQuestion.objects.get().audience == DeferredQuestion.Audience.INTERNAL
        assert OWNER_QUESTION_ROUTE in reason

    def test_loop_driven_allow_rule_ask_recorded_with_credentials_is_posted(self) -> None:
        """The route the deny names is the one that reaches the owner."""
        backend = _slack_backend()
        call_command(
            "questions",
            "record",
            "Grant the allow rule Bash(gh api repos/*)?",
            "--decision",
            "credentials",
            "--checked",
            "the classifier denied gh api twice this run",
        )

        _drain_to(backend)

        assert backend.post_message.call_count == 1
        assert "Bash(gh api repos/*)" in backend.post_message.call_args.kwargs["text"]

    def test_a_retry_of_the_same_denied_question_keeps_one_row(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-3", tool_use_id="t-11"))
        capsys.readouterr()
        handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-3", tool_use_id="t-11"))
        capsys.readouterr()

        assert DeferredQuestion.objects.count() == 1, "the retry must not fork a second row"
        assert DeferredQuestion.objects.get().dismissed_at is None

    @pytest.mark.parametrize(
        ("resolution", "label"),
        [({"answer": "ship it"}, "answered"), ({"dismissed_reason": "stale"}, "dismissed")],
    )
    def test_a_reask_after_the_row_resolved_gets_a_fresh_row(
        self, capsys: pytest.CaptureFixture[str], resolution: dict[str, str], label: str
    ) -> None:
        """The dedupe lookup reads ``pending()``, so a RESOLVED internal row is never reused."""
        handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-6", tool_use_id="t-14"))
        capsys.readouterr()
        resolved = DeferredQuestion.consume(DeferredQuestion.objects.get().pk, **resolution)
        assert resolved is not None
        handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-6", tool_use_id="t-15"))
        capsys.readouterr()

        assert DeferredQuestion.objects.count() == 2, f"the re-ask after a {label} row was swallowed"
        assert DeferredQuestion.pending().get().pk != resolved.pk

    def test_a_second_session_asking_the_same_question_gets_its_own_row(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Control: the guard is scoped per session, so two sessions never share a row."""
        handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-4", tool_use_id="t-12"))
        capsys.readouterr()
        handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-5", tool_use_id="t-13"))
        capsys.readouterr()
        assert sorted(DeferredQuestion.objects.values_list("session_id", flat=True)) == ["s-4", "s-5"]


class TestSection807InteropGate:
    """The load-bearing §807 interop test.

    BLUEPRINT §17.1 invariant 9 promises the deferral path is a *sanctioned destination*
    for the same ``AskUserQuestion`` tool call — converted at the ``PreToolUse`` layer —
    never an inline prose fallback. A converted call still emits a ``tool_use`` block in
    the transcript (the deny denies *execution*, not the record), so the §807 Stop gate
    reads the last assistant turn, sees the tool_use, and returns ``None``.
    """

    def _transcript(self, tmp_path: Path, *, with_tool_use: bool) -> Path:
        content: list[dict] = [{"type": "text", "text": "Should I proceed? Please choose A or B."}]
        if with_tool_use:
            content.append({"type": "tool_use", "name": "AskUserQuestion", "input": {}})
        entries = [
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "do it"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": content}},
        ]
        path = tmp_path / "transcript.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        return path

    def test_converted_question_satisfies_807_gate(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = self._transcript(tmp_path, with_tool_use=True)
        assert handle_enforce_structured_question({"transcript_path": str(transcript)}) is None
        assert capsys.readouterr().out.strip() == ""

    def test_inline_question_without_tool_use_still_blocks(self, tmp_path: Path) -> None:
        """Control: without this, the test above could pass on a §807 gate broken in general."""
        transcript = self._transcript(tmp_path, with_tool_use=False)
        assert handle_enforce_structured_question({"transcript_path": str(transcript)}) is True
