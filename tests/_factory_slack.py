"""A live factory Slack for tests: a fake owner DM plus the heartbeat, transport and lease that make it live."""

import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from teatree.backends.slack.listener_heartbeat import write_heartbeat
from teatree.core.loop_lease_manager import T3_MASTER_SLOT
from teatree.core.models import LoopLease
from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID
from teatree.core.slack_listener_heartbeat import FILENAME
from teatree.types import RawAPIDict

SLACK_OVERLAY = "t3-slack"
OWNER_ID = "UOWNER"
OWNER_DM = "D-OWNER"
BOT_ID = "UBOT"


@dataclass
class FakeOwnerDm:
    """The owner's DM as the bot sees it: posts are remembered and read back by ``ts``."""

    user_id: str = OWNER_ID
    channel: str = OWNER_DM
    bot_user_id: str = BOT_ID
    fail_post: bool = False
    read_back: bool = True
    posts: list[RawAPIDict] = field(default_factory=list)

    def auth_test(self) -> RawAPIDict:
        return {"ok": True, "user_id": self.bot_user_id}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return self.channel

    def post_message(self, *, channel: str, text: str, thread_ts: str = "", blocks: object = None) -> RawAPIDict:
        _ = blocks
        if self.fail_post:
            return {"ok": False, "error": "channel_not_found"}
        ts = f"1700000000.{len(self.posts) + 1:06d}"
        self.posts.append({"ts": ts, "channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ok": True, "ts": ts}

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = channel
        if not self.read_back:
            return {}
        return next((post for post in self.posts if post["ts"] == ts), {})

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.test/archives/{channel}/p{ts.replace('.', '')}"


@contextmanager
def live_factory_slack(dm: FakeOwnerDm, *, overlay: str = SLACK_OVERLAY) -> Generator[Path]:
    """Make factory Slack live for *dm*: fresh heartbeat, one credentialed overlay, a held t3-master lease."""
    with tempfile.TemporaryDirectory(prefix="factory-slack-") as scratch:
        directory = Path(scratch)
        with (
            patch("teatree.core.slack_listener_heartbeat.DATA_DIR", directory),
            patch("teatree.core.backend_factory.OverlayConfigResolver.all_names", return_value=[overlay]),
            patch("teatree.core.backend_factory.configured_messaging_from_overlay", return_value=dm),
        ):
            write_heartbeat(overlay, directory / FILENAME)
            LoopLease.objects.claim_ownership(T3_MASTER_SLOT, session_id=LOOP_RUNNER_SESSION_ID, owner_pid=None)
            try:
                yield directory
            finally:
                LoopLease.objects.release_ownership(T3_MASTER_SLOT, session_id=LOOP_RUNNER_SESSION_ID)
