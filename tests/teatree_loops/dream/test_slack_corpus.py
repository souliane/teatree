"""The owner's Slack DMs are in the replay corpus, and only the OWNER's (#2663).

The corpus used to be ``~/.claude/projects`` and nothing else, so every correction the
owner typed in the Slack DM reached no detector. These tests pin the two halves of the
fix that can regress independently: the source is REGISTERED (so a Slack correction is
enumerated and reaches ``looks_like_user_correction``), and the source is AUTHORSHIP-
FILTERED (so the factory's own DMs are never mined as owner instructions — without that
guard the accountant grades its own output).
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import ConfigSetting, PendingChatInjection
from teatree.loops.dream import replay, slack_corpus
from teatree.loops.dream.compliance import detect_compliance_failures
from teatree.loops.dream.replay import TranscriptMember, build_extract, enumerate_members
from teatree.loops.dream.transcript_extract import looks_like_user_correction

_OWNER = "U0OWNER00"
_FACTORY = "B0FACTORY"

#: A real-shaped owner correction: it carries a ``_CORRECTION_CUES`` token ("stop"), so
#: it is ground truth for the detector rather than a line tuned to a private helper.
_CORRECTION = "stop the spam, I told you again to never DM me per-task"

#: A projects dir that cannot exist, so a test asserting on the enumerated members sees
#: ONLY what the Slack source contributed.
_NO_PROJECTS = Path("/nonexistent-projects-dir")


def _window() -> datetime:
    return datetime.now(tz=UTC) - timedelta(hours=48)


def _dm(
    text: str,
    *,
    user_id: str = _OWNER,
    ago: timedelta = timedelta(hours=1),
    at: datetime | None = None,
    ts: str = "",
) -> None:
    PendingChatInjection.objects.create(
        overlay="t3-teatree",
        channel="D0TEST",
        slack_ts=ts or f"{datetime.now(tz=UTC).timestamp()}-{text[:8]}",
        user_id=user_id,
        text=text,
        received_at=at if at is not None else datetime.now(tz=UTC) - ago,
    )


class _OwnerRegistryTestCase(TestCase):
    """Records ``_OWNER`` as the overlay's Slack owner through the real registry row."""

    def setUp(self) -> None:
        super().setUp()
        ConfigSetting.objects.set_value("overlays", {"t3-teatree": {"slack_user_id": _OWNER}})


class OwnerCorrectionReachesTheDetectorTestCase(_OwnerRegistryTestCase):
    """THE PROOF: an owner Slack correction is a corpus member the detector sees."""

    def test_owner_correction_is_enumerated_and_detected(self) -> None:
        _dm(_CORRECTION)

        members = enumerate_members(since=_window(), projects_dir=_NO_PROJECTS, task_output_roots=[])

        assert [m.kind for m in members] == [slack_corpus.SLACK_MEMBER_KIND], (
            "the owner's Slack DM must be enumerated as a corpus member"
        )
        extract = build_extract(members)
        assert any(_CORRECTION in snippet.text for snippet in extract.snippets)
        findings = detect_compliance_failures(extract)
        assert findings, "the enumerated Slack correction must reach the compliance detector"
        assert _CORRECTION in findings[0].evidence

    def test_registration_is_what_carries_it(self) -> None:
        """The mutation guard: unregister the source and the same message vanishes."""
        _dm(_CORRECTION)

        with patch.object(replay, "_EXTRA_MEMBER_SOURCES", ()):
            members = enumerate_members(since=_window(), projects_dir=_NO_PROJECTS, task_output_roots=[])

        assert members == []


class OnlyTheOwnerIsMinedTestCase(_OwnerRegistryTestCase):
    """The guard that matters most — the accountant must never grade its own output."""

    def test_factory_authored_dm_is_not_enumerated(self) -> None:
        _dm("stop doing that, I told you never to retry", user_id=_FACTORY)

        assert slack_corpus.owner_slack_members(since=_window()) == []

    def test_factory_dm_is_dropped_even_beside_an_owner_one(self) -> None:
        _dm(_CORRECTION, ts="1.1")
        _dm("stop: I told you never to open a second PR", user_id=_FACTORY, ts="1.2")

        bodies = "\n".join(m.text for m in slack_corpus.owner_slack_members(since=_window()))

        assert _CORRECTION in bodies
        assert "second PR" not in bodies

    def test_unresolvable_owner_id_mines_nothing(self) -> None:
        """Fail CLOSED: an empty allowlist enumerates nothing, never 'everything'."""
        ConfigSetting.objects.set_value("overlays", {"t3-teatree": {"slack_scope_profile": "dm_only"}})
        _dm(_CORRECTION)

        assert slack_corpus.owner_slack_members(since=_window()) == []


class WindowAndGroupingTestCase(_OwnerRegistryTestCase):
    def test_messages_before_the_cutoff_are_excluded(self) -> None:
        _dm(_CORRECTION, ago=timedelta(days=9), ts="old")
        _dm("stop, do not do that again", ago=timedelta(hours=2), ts="new")

        bodies = "\n".join(m.text for m in slack_corpus.owner_slack_members(since=_window()))

        assert "do not do that again" in bodies
        assert _CORRECTION not in bodies

    def test_one_member_per_utc_day_newest_first(self) -> None:
        """Fixed instants, not offsets from now — an offset test flips at midnight UTC."""
        _dm("stop A", at=datetime(2026, 8, 19, 9, 0, tzinfo=UTC), ts="a")
        _dm("stop B", at=datetime(2026, 8, 19, 18, 0, tzinfo=UTC), ts="b")
        _dm("stop C", at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC), ts="c")

        members = slack_corpus.owner_slack_members(since=datetime(2026, 8, 1, tzinfo=UTC))

        assert [m.path.as_posix() for m in members] == ["slack-dm/2026-08-20.jsonl", "slack-dm/2026-08-19.jsonl"]
        assert "stop A" in members[1].text
        assert "stop B" in members[1].text


class TestRenderedShapeMatchesTheTranscriptContract:
    """The rendered line must survive what every other transcript member survives."""

    @staticmethod
    def _member(line: str) -> TranscriptMember:
        return TranscriptMember(
            path=Path("slack-dm/2026-08-19.jsonl"), kind=slack_corpus.SLACK_MEMBER_KIND, mtime=1.0, text=line
        )

    def test_raw_line_satisfies_the_user_turn_role_gate(self) -> None:
        line = slack_corpus.render_owner_turn(_CORRECTION, received_at=datetime.now(tz=UTC), thread_ts="")

        assert looks_like_user_correction(line)

    def test_decoded_line_still_satisfies_it(self) -> None:
        """``build_extract`` stores the DECODED form, which compliance re-tests per line."""
        line = slack_corpus.render_owner_turn(_CORRECTION, received_at=datetime.now(tz=UTC), thread_ts="")

        [snippet] = build_extract([self._member(line)]).snippets

        assert looks_like_user_correction(snippet.text)
        assert _CORRECTION in snippet.text

    def test_multiline_message_stays_one_transcript_line(self) -> None:
        line = slack_corpus.render_owner_turn("stop\nthis\nnow", received_at=datetime.now(tz=UTC), thread_ts="")

        assert "\n" not in line

    def test_thread_ts_rides_the_envelope_but_never_the_prompt(self) -> None:
        """Thread provenance is preserved without costing a byte of distiller budget."""
        line = slack_corpus.render_owner_turn(
            _CORRECTION, received_at=datetime.now(tz=UTC), thread_ts="1787230873.656179"
        )
        assert json.loads(line)["thread_ts"] == "1787230873.656179"

        [snippet] = build_extract([self._member(line)]).snippets

        assert "1787230873.656179" not in snippet.text

    def test_body_reaches_the_publish_gate_undamaged(self) -> None:
        r"""No new redaction path: the rendered body is scannable by the EXISTING gate.

        The risk the rendering could introduce is escaping — a term hidden behind
        ``\uXXXX`` would be invisible to ``banned_terms_scanner.scan_text``, which reads
        the text it is handed. Both the raw line and the decoded snippet must therefore
        carry the term verbatim, so the withholding gate every published body already
        passes through sees it exactly as it would in any other member.
        """
        secret = "Zürich-Kundendaten"
        line = slack_corpus.render_owner_turn(f"stop leaking {secret}", received_at=datetime.now(tz=UTC), thread_ts="")
        assert secret in line

        [snippet] = build_extract([self._member(line)]).snippets

        assert secret in snippet.text


class TestSourceFailureNeverTakesThePassDown:
    def test_a_raising_source_is_logged_and_skipped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        def boom(_cutoff: datetime) -> list[TranscriptMember]:
            msg = "no DB"
            raise RuntimeError(msg)

        monkeypatch.setattr(replay, "_EXTRA_MEMBER_SOURCES", (("boom", boom),))
        (tmp_path / "slug").mkdir()
        (tmp_path / "slug" / "s.jsonl").write_text('{"type":"user"}\n')

        members = enumerate_members(since=_window(), projects_dir=tmp_path, task_output_roots=[])

        assert [m.kind for m in members] == ["main"]
