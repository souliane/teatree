"""The live-turn escape in ``handle_mirror_question_to_slack`` (#189, #2058, #2155).

Integration-first: the real ``hook_router`` handler is invoked with a PreToolUse payload
synthesised in-process, driven through the REAL ``handle_record_presence`` recording seam
and the REAL ``_is_live_user_turn`` predicate. The load-bearing §807 interop test is at
the bottom: a transcript carrying a hook-converted ``AskUserQuestion`` tool_use satisfies
the structured-question Stop gate, because the call is *structurally complete* — just
converted at the PreToolUse layer.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _LOOP_PROMPT, handle_enforce_structured_question, handle_mirror_question_to_slack
from teatree import live_presence
from teatree.core import notify as notify_module
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions
from teatree.live_presence import LIVE_TURN_FRESHNESS, PresenceHeartbeat

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


@contextmanager
def _kick_drains_through(backend: MagicMock) -> Iterator[None]:
    """Run the detached kick INLINE against *backend* — the real drain, no subprocess."""

    def _drain(ref: str) -> None:
        drain_unmirrored_deferred_questions(user_id="U1", only_ref=ref, backend=backend)

    with (
        patch.object(router, "_kick_question_drain", _drain),
        patch.object(notify_module, "messaging_from_overlay", return_value=backend),
    ):
        yield


def _stdout(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


@pytest.fixture(autouse=True)
def _loop_driven_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A loop-owning session, so only the live-turn predicate decides the verdict.

    #22: ``handle_record_presence`` writes via ``ups_fastpath.record_presence`` to
    ``primary_data_dir()`` — the DATA dir, not the control DB's parent — so
    ``XDG_DATA_HOME`` is what pins it, and the PRESENCE read coincides with that write
    exactly as it does in production.
    """
    monkeypatch.setattr(router, "_session_drives_loop", lambda _session: True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    target = tmp_path / "teatree" / "presence_heartbeat"
    monkeypatch.setattr(live_presence, "PRESENCE", PresenceHeartbeat(locate=lambda: target))
    monkeypatch.setattr(router, "STATE_DIR", tmp_path)


class TestLoopTurnDefersThroughRealPredicateInvariant9:
    """Invariant 9, exercised through the REAL ``_is_live_user_turn``.

    An autonomous / loop-driven turn has no prior same-session ``UserPromptSubmit``
    heartbeat, so the real predicate returns ``False`` and the question is denied in
    favour of the durable row plus its Slack mirror.
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
    REAL ``handle_record_presence`` recording seam and the REAL ``_is_live_user_turn``
    predicate. The loop owner is self-pumping; the user types a genuine fresh prompt the
    harness delivers prefixed by the loop continuation text. The invariant-9 anchor (a
    PURE loop tick, no user text → still denies) lives in the second test so the
    must-render escape is proven an escape, not a defanged gate.
    """

    def test_fresh_user_prompt_prefixed_by_loop_text_renders_live(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "owner"
        router.handle_record_presence(
            {"prompt": f"{_LOOP_PROMPT}\n\nactually, ask me which option you prefer", "session_id": session_id}
        )
        result = handle_mirror_question_to_slack(_ask_payload("Approve A or B?", session_id=session_id))
        assert result is False, "a fresh same-session user prompt this turn must render the question live"
        assert _stdout(capsys) == {}

    def test_pure_loop_tick_still_defers_invariant_9(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "owner"
        router.handle_record_presence({"prompt": _LOOP_PROMPT, "session_id": session_id})
        result = handle_mirror_question_to_slack(_ask_payload("Approve A or B?", session_id=session_id))
        assert result is True
        assert _stdout(capsys)["permissionDecision"] == "deny"


class TestWalkThroughSecondQuestionStaysLive:
    """#2058: a multi-question walk-through keeps EVERY question live.

    A user-invoked ``/checking`` walk-through renders its FIRST question live (fresh
    same-session prompt), the user answers, an intervening background task-notification
    turn fires (which does NOT refresh the presence heartbeat), and the SECOND question
    lands past :data:`LIVE_TURN_FRESHNESS` — so the pre-fix code denied it. The fix
    slides the live window forward each time an already-live question renders.
    """

    def test_second_question_after_notification_turn_still_renders_live(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session_id = "s-checking"
        t_prompt = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
        live_presence.PRESENCE.record(session_id=session_id, now=t_prompt)

        # Drive time through the real predicate by patching the clock the hook reads, so
        # this exercises the production path end to end.
        clock = {"now": t_prompt + timedelta(seconds=20)}
        heartbeat = live_presence.PRESENCE
        real_is_live = heartbeat.is_live_user_turn
        real_refresh = heartbeat.refresh_live_turn
        monkeypatch.setattr(
            heartbeat, "is_live_user_turn", lambda **kw: real_is_live(session_id=kw["session_id"], now=clock["now"])
        )
        monkeypatch.setattr(
            heartbeat, "refresh_live_turn", lambda **kw: real_refresh(session_id=kw["session_id"], now=clock["now"])
        )

        first = handle_mirror_question_to_slack(_ask_payload("Approve item 1?", session_id=session_id))
        assert first is False, "first question must render live, not deny"
        assert _stdout(capsys) == {}

        clock["now"] = t_prompt + timedelta(seconds=20) + LIVE_TURN_FRESHNESS - timedelta(seconds=10)
        assert clock["now"] - t_prompt > LIVE_TURN_FRESHNESS

        second = handle_mirror_question_to_slack(_ask_payload("Approve item 2?", session_id=session_id))
        assert second is False, "second question must still render live, not deny (#2058)"
        assert _stdout(capsys) == {}


class TestAttendedTurnNeverReachesSlack:
    """#4673: a question asked in Claude Code is not asked again in Slack.

    The attended arms render in-client, so nothing leaves the box — and crucially
    they record NO row: an un-mirrored row is exactly what the tick drain picks up,
    which would reinstate the duplication one cadence later.
    """

    def test_live_turn_posts_nothing_and_records_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "s-2"
        router.handle_record_presence({"prompt": "ask me something", "session_id": session_id})
        with patch.object(router, "_kick_question_drain") as kick:
            first = handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id=session_id, tool_use_id="t-1"))
            capsys.readouterr()
            second = handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id=session_id, tool_use_id="t-2"))
            capsys.readouterr()

        assert first is False, "a live turn must render in-client, not deny"
        assert second is False, "a live turn must render in-client, not deny"
        assert kick.call_count == 0
        assert DeferredQuestion.objects.count() == 0, "an attended row would be drained to Slack next tick"

    def test_attended_non_owner_turn_posts_nothing_and_records_nothing(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router, "_session_drives_loop", lambda _session: False)
        with patch.object(router, "_kick_question_drain") as kick:
            verdict = handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-7", tool_use_id="t-20"))
            capsys.readouterr()

        assert verdict is False
        assert kick.call_count == 0
        assert DeferredQuestion.objects.count() == 0

    def test_an_attended_ask_supersedes_the_loop_row_it_replaces(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise the Slack re-ask nag outlives the answer the owner just gave in-client.

        The re-ask is a DISTINCT harness call (its own ``tool_use_id``) inside the same
        run, which is the shape supersession is scoped to.
        """
        with patch.object(router, "_kick_question_drain"):
            handle_mirror_question_to_slack(
                _ask_payload("Ship it?", session_id="s-8", run_id="r-1", tool_use_id="t-21")
            )
            capsys.readouterr()
        stranded = DeferredQuestion.objects.get()
        assert stranded.dismissed_at is None

        monkeypatch.setattr(router, "_session_drives_loop", lambda _session: False)
        handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-8", run_id="r-1", tool_use_id="t-22"))
        capsys.readouterr()

        stranded.refresh_from_db()
        assert stranded.dismissed_at is not None, "the superseded loop row still nags on Slack"
        assert DeferredQuestion.objects.count() == 1, "the attended re-ask must not record its own row"


class TestLoopDeniedRetryDoesNotDoubleDeliver:
    """A harness retry of the SAME denied ``AskUserQuestion`` reaches Slack once.

    A denied tool call is the one the harness can plausibly retry with the identical
    payload (there is no other way for the agent to "try again"). Driven end to end
    through the real drain, so the property proven is "the owner sees it once", not
    "the hook called something once".
    """

    def test_retry_with_identical_question_delivers_once(self, capsys: pytest.CaptureFixture[str]) -> None:
        backend = _slack_backend()
        with _kick_drains_through(backend):
            handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-1", tool_use_id="t-9"))
            capsys.readouterr()
            handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-1", tool_use_id="t-9"))
            capsys.readouterr()
        assert backend.post_message.call_count == 1

    def test_a_genuinely_different_question_still_delivers(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Control: the guard keys on the question, not on blanket session suppression."""
        backend = _slack_backend()
        with _kick_drains_through(backend):
            handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-1", tool_use_id="t-9"))
            capsys.readouterr()
            handle_mirror_question_to_slack(_ask_payload("Merge it?", session_id="s-1", tool_use_id="t-10"))
            capsys.readouterr()
        assert backend.post_message.call_count == 2


class TestLoopDeniedRetryKeepsOneBindableRow:
    """A harness retry of the SAME denied question leaves ONE row the reply can bind.

    Suppressing only the delivery is not enough: ``live_for_reply`` needs a row that is
    both mirrored (``slack_ts``) and undismissed, so superseding the mirrored row and
    recording an unmirrored twin satisfies neither and drops the operator's answer as
    stale — while ``unmirrored_pending`` re-drains the twin and double-posts anyway.
    """

    def test_retry_leaves_the_mirrored_row_live_and_bindable(self, capsys: pytest.CaptureFixture[str]) -> None:
        backend = _slack_backend()
        with _kick_drains_through(backend):
            first_id = handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-3", tool_use_id="t-11"))
            capsys.readouterr()
            handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-3", tool_use_id="t-11"))
            capsys.readouterr()

        assert first_id is True, "a loop-driven question must deny, not render in-client"
        assert DeferredQuestion.objects.count() == 1, "the retry must not fork a second row"
        row = DeferredQuestion.objects.get()
        assert row.slack_ts == "1700.0001", "the drain did not stamp the mirror coordinates"
        assert row.dismissed_at is None, "the retry superseded the only mirrored row"
        bound = DeferredQuestion.live_for_reply(channel="D1", after_ts="1700.0002")
        assert bound is not None, "the operator's Slack answer has no live row to bind"
        assert bound.pk == row.pk

    @pytest.mark.parametrize(
        ("resolution", "label"),
        [({"answer": "ship it"}, "answered"), ({"dismissed_reason": "stale"}, "dismissed")],
    )
    def test_a_reask_after_the_operator_resolved_gets_a_fresh_row(
        self, capsys: pytest.CaptureFixture[str], resolution: dict[str, str], label: str
    ) -> None:
        """The dedupe lookup reads ``pending()``, so a RESOLVED row can never be reused.

        Reused, the hook returns the resolved row's pk, delivers nothing, and the re-ask
        is swallowed forever — the operator is asked once and never hears about it again.
        """
        backend = _slack_backend()
        with _kick_drains_through(backend):
            handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-6", tool_use_id="t-14"))
            capsys.readouterr()
            resolved = DeferredQuestion.consume(DeferredQuestion.objects.get().pk, **resolution)
            assert resolved is not None
            backend.post_message.return_value = {"ok": True, "ts": "1700.0003"}
            handle_mirror_question_to_slack(_ask_payload("Ship it?", session_id="s-6", tool_use_id="t-15"))
            capsys.readouterr()

            assert backend.post_message.call_count == 2, f"the re-ask after a {label} row was swallowed"

        assert DeferredQuestion.objects.count() == 2
        fresh = DeferredQuestion.pending().get()
        assert fresh.pk != resolved.pk
        assert fresh.slack_ts == "1700.0003"
        bound = DeferredQuestion.live_for_reply(channel="D1", after_ts="1700.0004")
        assert bound is not None
        assert bound.pk == fresh.pk

    def test_a_second_session_asking_the_same_question_gets_its_own_row(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Control: the guard is scoped per session, so two sessions never share a row."""
        with patch.object(router, "_kick_question_drain"):
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
