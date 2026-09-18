"""The transcript weight ladder's user-correction floor (#2663).

A transcript earns `_WEIGHT_CORRECTION` — the highest floor a non-memory member can
reach — from a fresh human correction. Relayed tool output carries every correction cue
while correcting nobody, so it must not lift a chatter-only session over the doctrine it
would then outrank. Each fixture runs through ``high_signal_lines``, the seam
``_read_member_text`` feeds the ladder from.
"""

import json

from django.test import SimpleTestCase

from teatree.loops.dream.replay import _has_user_correction
from teatree.loops.dream.transcript_extract import high_signal_lines


def _tool_result(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"tool_use_id": "toolu_1", "type": "tool_result", "content": text}],
            },
        }
    )


def _meta(text: str) -> str:
    return json.dumps({"type": "user", "isMeta": True, "message": {"role": "user", "content": text}})


def _human(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


_OUTPUT = "fatal: not a git repository — do not retry, never force it"


class HasUserCorrectionTestCase(SimpleTestCase):
    """Only a human turn lifts a transcript to the correction floor."""

    def test_a_human_correction_earns_the_floor(self) -> None:
        assert _has_user_correction(high_signal_lines(_human("why was it closed????? stop doing that")))

    def test_a_tool_result_only_transcript_does_not_earn_the_floor(self) -> None:
        assert not _has_user_correction(high_signal_lines(_tool_result(_OUTPUT)))

    def test_a_harness_only_transcript_does_not_earn_the_floor(self) -> None:
        assert not _has_user_correction(high_signal_lines(_meta("Stop hook feedback: do not stop")))

    def test_a_raw_tool_result_line_does_not_earn_the_floor(self) -> None:
        assert not _has_user_correction(_tool_result("do not retry"))
