"""Tests for the dream transcript line-keeping concern (#1933)."""

from django.test import TestCase

from teatree.loops.dream.transcript_extract import high_signal_lines, looks_like_user_correction


class LooksLikeUserCorrectionTestCase(TestCase):
    """The pure heuristic that keeps a user-correction turn carrying no keyword (#1933)."""

    def test_imperative_negation_is_flagged(self) -> None:
        assert looks_like_user_correction('{"type":"user","text":"do not build a new banner"}')

    def test_told_you_is_flagged(self) -> None:
        assert looks_like_user_correction('{"type":"user","text":"I told you this already"}')

    def test_again_is_flagged(self) -> None:
        assert looks_like_user_correction('{"type":"user","text":"you did it again"}')

    def test_stop_is_flagged(self) -> None:
        assert looks_like_user_correction('{"type":"user","text":"stop — that is wrong"}')

    def test_never_is_flagged(self) -> None:
        assert looks_like_user_correction('{"type":"user","text":"never do that"}')

    def test_why_question_is_flagged(self) -> None:
        assert looks_like_user_correction('{"type":"user","text":"why did you do that?"}')

    def test_repeated_bang_is_flagged(self) -> None:
        assert looks_like_user_correction('{"type":"user","text":"no!! that is broken"}')

    def test_role_user_shape_is_flagged(self) -> None:
        assert looks_like_user_correction('{"role":"user","content":"stop doing that"}')

    def test_neutral_assistant_line_is_not_flagged(self) -> None:
        assert not looks_like_user_correction('{"type":"assistant","text":"computed result row 7"}')

    def test_neutral_user_line_without_cue_is_not_flagged(self) -> None:
        assert not looks_like_user_correction('{"type":"user","text":"please add a new endpoint here"}')

    def test_only_user_turns_are_considered(self) -> None:
        # An assistant line with a frustration cue is the agent's own text, not a correction.
        assert not looks_like_user_correction('{"type":"assistant","text":"do not worry, again it works"}')

    def test_why_without_question_mark_is_not_flagged(self) -> None:
        assert not looks_like_user_correction('{"type":"user","text":"the reason why it works is clear"}')


class HighSignalLinesTestCase(TestCase):
    def test_keeps_keyword_signal_line(self) -> None:
        raw = '{"type":"assistant","text":"noise"}\n{"type":"user","text":"TEATREE GATE BLOCK fired"}'
        assert "TEATREE GATE BLOCK" in high_signal_lines(raw)

    def test_keeps_correction_prose_with_no_keyword(self) -> None:
        raw = '{"type":"assistant","text":"noise"}\n{"type":"user","text":"do not build a new banner, stop"}'
        assert "do not build a new banner" in high_signal_lines(raw)

    def test_drops_neutral_chatter(self) -> None:
        raw = "\n".join(f'{{"type":"assistant","text":"row {i}"}}' for i in range(20))
        assert high_signal_lines(raw) == ""

    def test_keeps_repeated_near_identical_user_turn(self) -> None:
        repeated = '{"type":"user","text":"the authoring UI is still missing from the deliverable"}'
        raw = f'{repeated}\n{repeated}\n{repeated}\n{{"type":"assistant","text":"neutral"}}'
        assert "the authoring UI is still missing" in high_signal_lines(raw)

    def test_user_turn_seen_twice_is_not_yet_repeated(self) -> None:
        once = '{"type":"user","text":"a neutral request with no cue at all here"}'
        raw = f"{once}\n{once}"
        assert high_signal_lines(raw) == ""
