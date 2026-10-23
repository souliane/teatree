from unittest.mock import patch

from teatree.core.models import ReviewRequestPost
from teatree.loop.scanners.review_nag_pause import pause_skip
from tests.teatree_loop.test_review_nag_scanner import FakeSlack


def test_visible_root_pause_skips_without_another_message_read() -> None:
    post = ReviewRequestPost(
        mr_url="https://gitlab.example/x/-/merge_requests/1",
        slack_channel_id="CREVIEW",
        slack_thread_ts="1000",
    )
    slack = FakeSlack()
    root = {"ts": "1000", "reactions": [{"name": "pause_button"}]}

    with patch.object(slack, "fetch_message", side_effect=AssertionError("duplicate read")):
        signal = pause_skip(post, slack, visible_root=root)

    assert signal is not None
    assert signal.kind == "review_nag.mr_paused"
    assert signal.payload["pause_state"] == "paused"
