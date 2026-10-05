# test-path: cross-cutting
# Exercises hooks/scripts/owner_prompts.py (no src/teatree mirror).
"""What the owner typed and answered, read from the session transcript (#189, #2058, #2155, #4195).

The live-turn predicate is FAIL-SAFE toward deferral: an absent, stale, foreign or
unparsable signal is not a live turn, so an autonomous turn keeps deferring its
questions (BLUEPRINT §17.1 invariant 9).
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from hooks.scripts import owner_prompts
from hooks.scripts.hook_router import _LOOP_PROMPT
from hooks.scripts.owner_prompts import (
    LIVE_TURN_FRESHNESS,
    is_live_user_turn,
    owner_messages,
    owner_prompted_since,
    owner_prompts_since,
)

_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _stamp(seconds_ago: float) -> str:
    return (_NOW - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def _prompt(text: str, seconds_ago: float, *, kind: str = "human", **extra: object) -> dict:
    return {
        "type": "user",
        "origin": {"kind": kind},
        "timestamp": _stamp(seconds_ago),
        "message": {"role": "user", "content": text},
        **extra,
    }


def _scheduled_fire(text: str, seconds_ago: float) -> dict:
    """A cron or ``/loop`` fire, in the shape Claude Code writes it (sampled from a live transcript, redacted)."""
    return {
        "parentUuid": "<parentUuid>",
        "isSidechain": False,
        "promptId": "<promptId>",
        "type": "user",
        "message": {"role": "user", "content": text},
        "uuid": "<uuid>",
        "timestamp": _stamp(seconds_ago),
        "scheduledTaskId": "0fbfbf4a",
        "scheduledFireId": "<scheduledFireId>",
        "turnOrigin": "scheduled",
        "queuePriority": "later",
        "queueSkipAttachments": True,
        "sessionKind": "bg",
        "userType": "external",
        "entrypoint": "cli",
    }


def _answer(seconds_ago: float) -> dict:
    return {
        "type": "user",
        "timestamp": _stamp(seconds_ago),
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        "toolUseResult": {"questions": [], "answers": {"A or B?": "A"}},
    }


def _write(tmp_path: Path, *entries: dict) -> str:
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return str(path)


class TestIsLiveUserTurn:
    def test_a_prompt_the_owner_typed_moments_ago_is_live(self, tmp_path: Path) -> None:
        assert is_live_user_turn(_write(tmp_path, _prompt("which one?", 2)), now=_NOW) is True

    def test_a_prompt_just_past_the_window_is_not_live(self, tmp_path: Path) -> None:
        stale = LIVE_TURN_FRESHNESS.total_seconds() + 1

        assert is_live_user_turn(_write(tmp_path, _prompt("which one?", stale)), now=_NOW) is False

    def test_an_in_client_answer_keeps_a_walk_through_live(self, tmp_path: Path) -> None:
        transcript = _write(tmp_path, _prompt("/checking", 600), _answer(5))

        assert is_live_user_turn(transcript, now=_NOW) is True

    @pytest.mark.parametrize(
        "entry",
        [
            _prompt("<task-notification>done</task-notification>", 1, kind="task-notification"),
            _prompt("Stop hook feedback", 1, isMeta=True),
            _prompt(_LOOP_PROMPT, 1),
            _scheduled_fire("Drive the PR board.", 1),
            {**_prompt("fired", 1), "scheduledFireId": "a1b2"},
            {**_prompt("peer said", 1), "turnOrigin": "peer"},
        ],
        ids=[
            "task-notification",
            "meta",
            "bare-loop-tick",
            "scheduled-fire",
            "scheduled-fire-id-only",
            "non-human-turn-origin",
        ],
    )
    def test_machine_turns_are_not_the_owner_acting(self, tmp_path: Path, entry: dict) -> None:
        transcript = _write(tmp_path, _prompt("earlier", 600), entry)

        assert is_live_user_turn(transcript, now=_NOW) is False

    def test_a_genuine_prompt_prefixed_by_the_loop_text_is_live(self, tmp_path: Path) -> None:
        transcript = _write(tmp_path, _prompt(f"{_LOOP_PROMPT}\n\nactually, ask me which option", 1))

        assert is_live_user_turn(transcript, now=_NOW) is True

    @pytest.mark.parametrize("timestamp", [None, "not-a-time", "2026-09-29T11:59:58"])
    def test_an_unreadable_timestamp_is_not_live(self, tmp_path: Path, timestamp: str | None) -> None:
        entry = {**_prompt("hi", 1), "timestamp": timestamp}

        assert is_live_user_turn(_write(tmp_path, entry), now=_NOW) is False

    def test_a_typed_prompt_tagged_human_is_live(self, tmp_path: Path) -> None:
        assert is_live_user_turn(_write(tmp_path, {**_prompt("go", 1), "turnOrigin": "human"}), now=_NOW) is True

    def test_an_absent_transcript_is_not_live(self, tmp_path: Path) -> None:
        assert is_live_user_turn(str(tmp_path / "missing.jsonl"), now=_NOW) is False
        assert is_live_user_turn("", now=_NOW) is False


class TestOwnerMessages:
    def test_keeps_the_owners_words_and_drops_everything_else(self, tmp_path: Path) -> None:
        transcript = _write(
            tmp_path,
            _prompt("<system-reminder>CLAUDE.md body</system-reminder>please ship it", 60),
            _prompt(_LOOP_PROMPT, 50),
            _prompt("relayed", 40, kind="peer", isMeta=True),
            _answer(30),
            {"type": "assistant", "timestamp": _stamp(20), "message": {"role": "assistant", "content": "done"}},
        )

        assert owner_messages(transcript, limit=25) == ["please ship it"]

    def test_a_scheduled_fire_is_not_the_owners_words(self, tmp_path: Path) -> None:
        transcript = _write(tmp_path, _prompt("mine", 60), _scheduled_fire("Drive the PR board.", 5))

        assert owner_messages(transcript, limit=25) == ["mine"]

    def test_the_newest_messages_come_first_up_to_the_limit(self, tmp_path: Path) -> None:
        transcript = _write(tmp_path, *(_prompt(f"message {index}", 100 - index) for index in range(5)))

        assert owner_messages(transcript, limit=2) == ["message 4", "message 3"]

    def test_an_unreadable_transcript_is_none_not_empty(self, tmp_path: Path) -> None:
        assert owner_messages(str(tmp_path / "missing.jsonl"), limit=25) is None
        assert owner_messages("", limit=25) is None


class TestOwnerPromptedSince:
    """An interrupted turn fires no Stop, so the next tool call learns of the new prompt from the transcript."""

    @staticmethod
    def _append(transcript: Path, *entries: dict) -> None:
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(entry) + "\n" for entry in entries))

    def test_a_first_look_marks_the_end_and_reports_nothing(self, tmp_path: Path) -> None:
        transcript = Path(_write(tmp_path, _prompt("earlier", 60)))
        cursor = tmp_path / "cursor"

        assert owner_prompted_since(cursor, str(transcript)) is False
        assert cursor.read_text(encoding="utf-8") == str(transcript.stat().st_size)

    def test_a_prompt_appended_since_the_last_look_is_seen_once(self, tmp_path: Path) -> None:
        transcript = Path(_write(tmp_path, _prompt("earlier", 60)))
        cursor = tmp_path / "cursor"
        owner_prompted_since(cursor, str(transcript))
        self._append(transcript, _prompt("carry on", 1))

        assert owner_prompted_since(cursor, str(transcript)) is True
        assert owner_prompted_since(cursor, str(transcript)) is False

    def test_machine_turns_appended_since_are_not_a_prompt(self, tmp_path: Path) -> None:
        transcript = Path(_write(tmp_path, _prompt("earlier", 60)))
        cursor = tmp_path / "cursor"
        owner_prompted_since(cursor, str(transcript))
        self._append(transcript, _scheduled_fire("Drive the PR board.", 2), _answer(1))

        assert owner_prompted_since(cursor, str(transcript)) is False

    def test_a_line_still_being_written_waits_for_its_newline(self, tmp_path: Path) -> None:
        transcript = Path(_write(tmp_path, _prompt("earlier", 60)))
        cursor = tmp_path / "cursor"
        owner_prompted_since(cursor, str(transcript))
        line = json.dumps(_prompt("carry on", 1))
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(line[:20])

        assert owner_prompted_since(cursor, str(transcript)) is False
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(line[20:] + "\n")
        assert owner_prompted_since(cursor, str(transcript)) is True

    def test_an_absent_transcript_reports_nothing(self, tmp_path: Path) -> None:
        assert owner_prompted_since(tmp_path / "cursor", str(tmp_path / "missing.jsonl")) is False


def _slash_command(name: str, args: str, seconds_ago: float) -> dict:
    """A slash command as Claude Code records it: the typed text survives only inside the harness wrappers."""
    wrapped = (
        f"<command-message>{name.lstrip('/')} is running…</command-message>\n"
        f"<command-name>{name}</command-name>\n<command-args>{args}</command-args>"
    )
    return _prompt(wrapped, seconds_ago)


class TestOwnerPromptsSince:
    """Each owner prompt handed out once, oldest first — the trigger of the turn context the first tool call carries."""

    _append = staticmethod(TestOwnerPromptedSince._append)

    @staticmethod
    def _read(cursor: Path, transcript: str) -> list[str]:
        unread = owner_prompts_since(cursor, transcript)
        unread.mark_read()
        return list(unread.prompts)

    def test_a_first_look_hands_out_the_newest_prompt_already_there(self, tmp_path: Path) -> None:
        transcript = _write(tmp_path, _prompt("older", 90), _prompt("newest ask", 60), _scheduled_fire("Drive.", 30))
        cursor = tmp_path / "cursor"

        assert self._read(cursor, transcript) == ["newest ask"]
        assert self._read(cursor, transcript) == []

    def test_an_empty_cursor_is_a_first_look(self, tmp_path: Path) -> None:
        transcript = _write(tmp_path, _prompt("newest ask", 60))
        cursor = tmp_path / "cursor"
        cursor.touch()

        assert self._read(cursor, transcript) == ["newest ask"]

    def test_a_cursor_past_the_end_is_a_first_look(self, tmp_path: Path) -> None:
        # A rewritten or replaced transcript is shorter than the offset recorded against the old one.
        transcript = _write(tmp_path, _prompt("the only ask", 60))
        cursor = tmp_path / "cursor"
        cursor.write_text("999999", encoding="utf-8")

        assert self._read(cursor, transcript) == ["the only ask"]
        assert cursor.read_text(encoding="utf-8") == str(Path(transcript).stat().st_size)

    def test_prompts_appended_since_are_handed_out_once_oldest_first(self, tmp_path: Path) -> None:
        transcript = Path(_write(tmp_path, _prompt("earlier", 60)))
        cursor = tmp_path / "cursor"
        self._read(cursor, str(transcript))
        self._append(transcript, _prompt("one", 3), _scheduled_fire("Drive.", 2), _prompt("two", 1))

        assert self._read(cursor, str(transcript)) == ["one", "two"]
        assert self._read(cursor, str(transcript)) == []

    @pytest.mark.parametrize("first_look", [True, False], ids=["first-look", "appended"])
    def test_a_slash_command_is_handed_out_as_typed(self, tmp_path: Path, *, first_look: bool) -> None:
        typed = _slash_command("/t3:code", "how do I create a worktree?", 1)
        transcript = Path(_write(tmp_path, *([typed] if first_look else [_prompt("earlier", 60)])))
        cursor = tmp_path / "cursor"
        if not first_look:
            self._read(cursor, str(transcript))
            self._append(transcript, typed)

        assert self._read(cursor, str(transcript)) == ["/t3:code how do I create a worktree?"]

    def test_a_slash_command_with_no_arguments_is_its_name(self, tmp_path: Path) -> None:
        transcript = _write(tmp_path, _slash_command("/t3:retro", "", 1))

        assert self._read(tmp_path / "cursor", transcript) == ["/t3:retro"]

    def test_machine_turns_and_loop_ticks_are_not_handed_out(self, tmp_path: Path) -> None:
        transcript = Path(_write(tmp_path, _prompt("earlier", 60)))
        cursor = tmp_path / "cursor"
        self._read(cursor, str(transcript))
        self._append(transcript, _scheduled_fire("Drive.", 3), _answer(2), _prompt(_LOOP_PROMPT, 1))

        assert self._read(cursor, str(transcript)) == []

    def test_nothing_is_marked_read_until_the_caller_says_so(self, tmp_path: Path) -> None:
        transcript = Path(_write(tmp_path, _prompt("earlier", 60)))
        cursor = tmp_path / "cursor"
        self._read(cursor, str(transcript))
        self._append(transcript, _prompt("one", 1))
        recorded = cursor.read_text(encoding="utf-8")

        assert list(owner_prompts_since(cursor, str(transcript)).prompts) == ["one"]
        assert cursor.read_text(encoding="utf-8") == recorded
        assert self._read(cursor, str(transcript)) == ["one"]

    def test_a_first_look_at_a_session_with_no_prompt_hands_out_nothing(self, tmp_path: Path) -> None:
        transcript = _write(tmp_path, _scheduled_fire("Drive.", 3), _answer(2))

        assert self._read(tmp_path / "cursor", transcript) == []

    def test_an_absent_transcript_hands_out_nothing(self, tmp_path: Path) -> None:
        assert self._read(tmp_path / "cursor", str(tmp_path / "missing.jsonl")) == []


class TestTheReadIsBoundedByTheTurn:
    """A long session costs the readers its latest turn, not its whole history."""

    _HISTORY = 50_000

    @pytest.fixture
    def long_session(self, tmp_path: Path) -> str:
        path = tmp_path / "transcript.jsonl"
        old = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "x" * 40}})
        recent = [_prompt(f"message {index}", 30 - index) for index in range(30)]
        path.write_text(
            (old + "\n") * self._HISTORY + "".join(json.dumps(entry) + "\n" for entry in recent), encoding="utf-8"
        )
        return str(path)

    @pytest.mark.parametrize(
        "read",
        [
            lambda path: is_live_user_turn(path, now=_NOW),
            lambda path: owner_messages(path, limit=25),
            lambda path: owner_prompts_since(Path(f"{path}.cursor"), path).prompts,
        ],
        ids=["live-turn", "paste-history", "first-look"],
    )
    def test_decodes_only_the_tail(self, long_session: str, read: Callable[[str], object]) -> None:
        with mock.patch.object(json, "loads", wraps=json.loads) as loads:
            read(long_session)

        assert loads.call_count < 200


class TestALongRangeIsReadFromItsNewestEnd:
    """A range past the cursor is read from its newest end only, so every look costs the same and moves on."""

    _RANGE = 200_000
    _DECODE_CEILING = 5_000

    @pytest.fixture
    def cursor(self, tmp_path: Path) -> Path:
        return tmp_path / "cursor"

    @pytest.fixture
    def transcript(self, tmp_path: Path, cursor: Path) -> Path:
        transcript = Path(_write(tmp_path, _prompt("earlier", 60)))
        TestOwnerPromptsSince._read(cursor, str(transcript))
        return transcript

    def _append_range(self, transcript: Path, *newest: dict) -> None:
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write('{"type":"progress"}\n' * self._RANGE)
            stream.write("".join(json.dumps(entry) + "\n" for entry in newest))

    def test_the_newest_prompt_is_handed_out_from_a_bounded_read(self, transcript: Path, cursor: Path) -> None:
        self._append_range(transcript, _prompt("the newest ask", 1))

        with mock.patch.object(json, "loads", wraps=json.loads) as loads:
            unread = owner_prompts_since(cursor, str(transcript))

        assert unread.prompts == ("the newest ask",)
        assert loads.call_count < self._DECODE_CEILING
        unread.mark_read()
        assert cursor.read_text(encoding="utf-8") == str(transcript.stat().st_size)

    def test_a_range_with_no_prompt_still_moves_the_cursor_to_its_end(self, transcript: Path, cursor: Path) -> None:
        self._append_range(transcript)

        with mock.patch.object(json, "loads", wraps=json.loads) as loads:
            unread = owner_prompts_since(cursor, str(transcript))

        assert unread.prompts == ()
        assert loads.call_count < self._DECODE_CEILING
        unread.mark_read()
        assert cursor.read_text(encoding="utf-8") == str(transcript.stat().st_size)

    def test_a_first_look_at_a_long_session_with_no_prompt_is_bounded_too(self, tmp_path: Path) -> None:
        transcript = tmp_path / "transcript.jsonl"
        self._append_range(transcript)
        cursor = tmp_path / "cursor"

        with mock.patch.object(json, "loads", wraps=json.loads) as loads:
            unread = owner_prompts_since(cursor, str(transcript))

        assert unread.prompts == ()
        assert loads.call_count < self._DECODE_CEILING
        unread.mark_read()
        assert cursor.read_text(encoding="utf-8") == str(transcript.stat().st_size)

    def test_the_turn_reset_reads_the_same_bounded_range(self, transcript: Path, cursor: Path) -> None:
        self._append_range(transcript, _prompt("the newest ask", 1))

        with mock.patch.object(json, "loads", wraps=json.loads) as loads:
            assert owner_prompted_since(cursor, str(transcript)) is True

        assert loads.call_count < self._DECODE_CEILING
        assert cursor.read_text(encoding="utf-8") == str(transcript.stat().st_size)

    @staticmethod
    def _look(cursor: Path, transcript: Path, *, marking: bool) -> tuple[str, ...]:
        if not marking:
            return ("<prompted>",) if owner_prompted_since(cursor, str(transcript)) else ()
        unread = owner_prompts_since(cursor, str(transcript))
        unread.mark_read()
        return unread.prompts

    @pytest.mark.parametrize("marking", [True, False], ids=["prompts-since", "prompted-since"])
    @pytest.mark.parametrize("past_the_window", [0, 10], ids=["exactly-the-window", "wider-than-the-window"])
    def test_an_unfinished_line_as_wide_as_the_window_is_skipped(
        self, transcript: Path, cursor: Path, past_the_window: int, *, marking: bool
    ) -> None:
        giant = json.dumps({"data": "x" * (owner_prompts.LOOK_BYTES + past_the_window)})
        cut = owner_prompts.LOOK_BYTES + past_the_window
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(giant[:cut])

        assert self._look(cursor, transcript, marking=marking) == ()
        assert cursor.read_text(encoding="utf-8") == str(transcript.stat().st_size)

        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(giant[cut:] + "\n" + json.dumps(_prompt("the next ask", 1)) + "\n")
        expected = ("the next ask",) if marking else ("<prompted>",)
        assert self._look(cursor, transcript, marking=marking) == expected
        assert cursor.read_text(encoding="utf-8") == str(transcript.stat().st_size)

    @pytest.mark.parametrize("marking", [True, False], ids=["prompts-since", "prompted-since"])
    def test_a_short_partial_tail_keeps_the_cursor(self, transcript: Path, cursor: Path, *, marking: bool) -> None:
        # The widest line a look can still read, caught while its writer is one newline short of finishing it.
        text = "x" * (owner_prompts.LOOK_BYTES - 1 - len(json.dumps(_prompt("", 1))))
        line = json.dumps(_prompt(text, 1))
        recorded = cursor.read_text(encoding="utf-8")
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(line)

        assert self._look(cursor, transcript, marking=marking) == ()
        assert cursor.read_text(encoding="utf-8") == recorded

        with transcript.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        assert self._look(cursor, transcript, marking=marking) == ((text,) if marking else ("<prompted>",))

    @pytest.mark.parametrize("marking", [True, False], ids=["prompts-since", "prompted-since"])
    @pytest.mark.parametrize("caught_unfinished", [True, False], ids=["caught-unfinished", "found-finished"])
    def test_a_line_as_wide_as_the_window_is_read_by_no_look(
        self, transcript: Path, cursor: Path, *, caught_unfinished: bool, marking: bool
    ) -> None:
        # Its newline lands one byte past the window, so no look reads it: one that finds it
        # finished skips it exactly like one that caught it unfinished and moved past it.
        line = json.dumps(_prompt("x" * (owner_prompts.LOOK_BYTES - len(json.dumps(_prompt("", 1)))), 1))
        assert len(line) == owner_prompts.LOOK_BYTES
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(line)
        if caught_unfinished:
            assert self._look(cursor, transcript, marking=marking) == ()

        with transcript.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        assert self._look(cursor, transcript, marking=marking) == ()
        assert cursor.read_text(encoding="utf-8") == str(transcript.stat().st_size)

    @pytest.mark.parametrize(
        ("starts_before_the_bound", "handed_out"),
        [(0, ("the newest ask",)), (1, ())],
        ids=["starts-at-the-bound", "starts-one-byte-before-it"],
    )
    def test_only_a_line_wholly_inside_the_bound_is_read(
        self, tmp_path: Path, starts_before_the_bound: int, handed_out: tuple[str, ...]
    ) -> None:
        ask = json.dumps(_prompt("the newest ask", 1)) + "\n"
        padding = owner_prompts.LOOK_BYTES - len(ask) + starts_before_the_bound - len('{"data": ""}\n')
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps(_prompt("older", 60)) + "\n" + ask + json.dumps({"data": "x" * padding}) + "\n", encoding="utf-8"
        )
        cursor = tmp_path / "cursor"
        cursor.write_text("0", encoding="utf-8")

        assert owner_prompts_since(cursor, str(transcript)).prompts == handed_out
