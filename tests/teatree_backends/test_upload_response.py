"""``shared_message_ts`` — parsing the ``completeUploadExternal`` body (#2054).

The resolve-from-``shares`` happy paths (private/public, missing channel)
are covered end-to-end through the real backend in
``test_slack_upload_audio.py``; these cover the malformed-shape edges the
parser must tolerate without raising.
"""

import pytest

from teatree.backends.slack.upload_response import shared_message_ts

_CHANNEL = "D_SELF"


class TestSharedMessageTs:
    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"ok": True},
            {"files": "not-a-list"},
            {"files": ["not-a-dict"]},
            {"files": [{"id": "F1"}]},
            {"files": [{"id": "F1", "shares": "not-a-dict"}]},
            {"files": [{"id": "F1", "shares": {"private": "not-a-dict"}}]},
            {"files": [{"id": "F1", "shares": {"private": {_CHANNEL: "not-a-list"}}}]},
            {"files": [{"id": "F1", "shares": {"private": {_CHANNEL: ["not-a-dict"]}}}]},
            {"files": [{"id": "F1", "shares": {"private": {_CHANNEL: [{"no_ts": "x"}]}}}]},
            {"files": [{"id": "F1", "shares": {"private": {_CHANNEL: [{"ts": 123}]}}}]},
        ],
    )
    def test_empty_on_missing_or_malformed_shape(self, body: dict[str, object]) -> None:
        assert shared_message_ts(body, channel=_CHANNEL) == ""

    def test_first_matching_entry_wins(self) -> None:
        body = {
            "files": [
                {"id": "F1", "shares": {"private": {_CHANNEL: [{"ts": "1.0"}, {"ts": "2.0"}]}}},
            ]
        }
        assert shared_message_ts(body, channel=_CHANNEL) == "1.0"

    def test_private_preferred_over_public(self) -> None:
        body = {
            "files": [
                {
                    "id": "F1",
                    "shares": {
                        "public": {_CHANNEL: [{"ts": "9.0"}]},
                        "private": {_CHANNEL: [{"ts": "1.0"}]},
                    },
                }
            ]
        }
        assert shared_message_ts(body, channel=_CHANNEL) == "1.0"
